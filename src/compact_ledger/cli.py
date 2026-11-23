"""Command-line interface for compact-ledger."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from compact_ledger import __version__
from compact_ledger.models import (
    CompactionSnapshot,
    PinEntry,
    make_pin_id,
    normalize_pin_text,
)
from compact_ledger.parsers.transcript import SUPPORTED_FORMATS, parse_transcript
from compact_ledger.store import CorruptSnapshotError, SessionStore

DEFAULT_MAX_CHARS = 6000


def _build_baseline_pins(snapshot_turns: Sequence[Any]) -> list[PinEntry]:
    """Convert parsed user turns into baseline auto PinEntry objects."""
    pins: list[PinEntry] = []
    seen_texts: set[str] = set()
    for turn in snapshot_turns:
        norm = normalize_pin_text(turn.text)
        if not norm or norm in seen_texts:
            continue
        seen_texts.add(norm)
        pins.append(
            PinEntry(
                pin_id=make_pin_id("auto", norm, turn.turn_index),
                text=turn.text.strip(),
                source="auto",
                turn_index=turn.turn_index,
                timestamp=turn.timestamp,
            )
        )
    return pins


def _truncate_line(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _render_markdown(snapshot: CompactionSnapshot, max_chars: int) -> str:
    """Render a bounded Markdown summary prioritizing manual pins over auto pins and files."""
    if max_chars <= 0:
        return ""

    header = f"## Compact Ledger (Session: {snapshot.session_id})"
    if len(header) > max_chars:
        return _truncate_line(header, max_chars)

    lines: list[str] = [header]
    used = len(header)

    def _try_add_section(title: str, items: list[str]) -> None:
        nonlocal used
        if not items:
            return
        section_header = f"\n\n### {title}"
        added_header = False
        for item in items:
            entry = f"\n- {item}"
            needed = len(entry) + (len(section_header) if not added_header else 0)
            if used + needed <= max_chars:
                if not added_header:
                    lines.append(section_header)
                    used += len(section_header)
                    added_header = True
                lines.append(entry)
                used += len(entry)
            else:
                remaining = max_chars - used - (len(section_header) if not added_header else 0) - 3
                if remaining >= 24:
                    if not added_header:
                        lines.append(section_header)
                        used += len(section_header)
                        added_header = True
                    truncated = _truncate_line(item, remaining)
                    entry = f"\n- {truncated}"
                    lines.append(entry)
                    used += len(entry)
                break

    manual_pins = [p for p in snapshot.pins if p.source == "manual"]
    override_pins = [p for p in snapshot.pins if p.source == "override"]
    auto_pins = list(reversed([p for p in snapshot.pins if p.source == "auto"]))

    manual_items = [
        f"{'[tags: ' + ', '.join(p.tags) + '] ' if p.tags else ''}{p.text}" for p in manual_pins
    ]
    _try_add_section("Manual Pins", manual_items)

    override_items = [p.text for p in override_pins]
    _try_add_section("Active Overrides", override_items)

    auto_items = [
        f"[turn {p.turn_index}] {p.text}" if p.turn_index is not None else p.text for p in auto_pins
    ]
    _try_add_section("User Constraints & Turns", auto_items)

    if snapshot.modified_files:
        _try_add_section("Modified Files", list(snapshot.modified_files))

    result = "".join(lines)
    if len(result) > max_chars:
        return _truncate_line(result, max_chars)
    return result


def _render_json(snapshot: CompactionSnapshot, max_chars: int) -> str:
    """Render a valid JSON representation bounded to ``max_chars`` when possible."""
    payload = snapshot.to_dict()
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered

    # Compact JSON first before dropping lower-priority fields.
    rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered

    # Progressively shed lower-priority sections while preserving manual pins.
    manual_pins = [p.to_dict() for p in snapshot.pins if p.source == "manual"]
    other_pins = [p.to_dict() for p in snapshot.pins if p.source != "manual"]
    trimmed = dict(payload)
    trimmed["metadata"] = {}
    trimmed["attempts"] = []
    trimmed["trajectories"] = []
    trimmed["thrash_entries"] = []
    trimmed["modified_files"] = list(snapshot.modified_files)
    trimmed["pins"] = manual_pins + other_pins

    rendered = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered

    trimmed["modified_files"] = []
    rendered = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered

    trimmed["transcript_path"] = None
    rendered = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered

    while other_pins and len(rendered) > max_chars:
        other_pins.pop(0)
        trimmed["pins"] = manual_pins + other_pins
        rendered = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)

    while manual_pins and len(rendered) > max_chars:
        manual_pins.pop()
        trimmed["pins"] = manual_pins
        rendered = json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False)

    return rendered


def _build_parser() -> argparse.ArgumentParser:
    store_help = (
        "Override the compact-ledger store directory "
        "(default: .compact-ledger or COMPACT_LEDGER_DIR)."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--store-dir",
        default=argparse.SUPPRESS,
        help=store_help,
    )

    parser = argparse.ArgumentParser(
        prog="compact-ledger",
        description="Deterministic, zero-LLM compaction memory injector for AI coding agents.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"compact-ledger {__version__}",
    )
    parser.add_argument(
        "--store-dir",
        default=None,
        help=store_help,
    )

    subparsers = parser.add_subparsers(dest="command")

    snap_parser = subparsers.add_parser(
        "snapshot",
        parents=[common],
        help="Parse a transcript and save a compaction snapshot.",
    )
    snap_parser.add_argument("--transcript", required=True, help="Path to JSONL transcript file.")
    snap_parser.add_argument(
        "--session-id",
        default=None,
        help="Session identifier (defaults to session_id parsed from transcript).",
    )
    snap_parser.add_argument(
        "--format",
        dest="transcript_format",
        choices=sorted(SUPPORTED_FORMATS),
        default="auto",
        help="Transcript format (default: auto).",
    )
    snap_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output saved snapshot summary as JSON.",
    )

    inject_parser = subparsers.add_parser(
        "inject",
        parents=[common],
        help="Render the stored compaction ledger for a session.",
    )
    inject_parser.add_argument("--session-id", required=True, help="Session identifier.")
    inject_parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"Maximum output character budget (default: {DEFAULT_MAX_CHARS}).",
    )
    inject_parser.add_argument(
        "--format",
        dest="output_format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown).",
    )

    pin_parser = subparsers.add_parser(
        "pin",
        parents=[common],
        help="Add a manual high-priority pin for a session.",
    )
    pin_parser.add_argument("--session-id", required=True, help="Session identifier.")
    pin_parser.add_argument("text", help="Verbatim constraint or instruction to pin.")
    pin_parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=[],
        help="Optional tag to attach to the pin (repeatable).",
    )

    status_parser = subparsers.add_parser(
        "status",
        parents=[common],
        help="Show status for one session or list all stored sessions.",
    )
    status_parser.add_argument(
        "--session-id",
        default=None,
        help="Session identifier (omit to list all sessions).",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output status as JSON.",
    )

    clear_parser = subparsers.add_parser(
        "clear",
        parents=[common],
        help="Delete a stored session snapshot.",
    )
    clear_parser.add_argument("--session-id", required=True, help="Session identifier.")

    return parser


def _cmd_snapshot(args: argparse.Namespace, store: SessionStore) -> int:
    transcript_path = Path(args.transcript)
    if not transcript_path.is_file():
        print(f"Error: transcript file not found: {transcript_path}", file=sys.stderr)
        return 1

    parsed = parse_transcript(
        transcript_path,
        fmt=args.transcript_format,
        session_id=args.session_id,
    )
    session_id = args.session_id or parsed.session_id
    if not session_id:
        print(
            "Error: --session-id is required when transcript has no embedded session_id.",
            file=sys.stderr,
        )
        return 2

    baseline_pins = _build_baseline_pins(parsed.user_turns)
    snapshot = CompactionSnapshot(
        session_id=session_id,
        transcript_path=str(transcript_path.resolve()),
        pins=baseline_pins,
        modified_files=list(parsed.modified_files),
        metadata={
            "transcript_format": parsed.format,
            "user_turn_count": len(parsed.user_turns),
            "tool_execution_count": len(parsed.tool_executions),
            "file_edit_count": len(parsed.file_edits),
        },
    )
    try:
        saved = store.save_snapshot(snapshot)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        summary = {
            "session_id": saved.session_id,
            "path": str(store.session_path(saved.session_id)),
            "format": parsed.format,
            "pins": len(saved.pins),
            "manual_pins": sum(1 for p in saved.pins if p.source == "manual"),
            "modified_files": len(saved.modified_files),
        }
        print(json.dumps(summary, indent=2))
    else:
        manual_count = sum(1 for p in saved.pins if p.source == "manual")
        print(
            f"Saved snapshot for session {saved.session_id!r} "
            f"(format={parsed.format}, pins={len(saved.pins)}, "
            f"manual_pins={manual_count}, modified_files={len(saved.modified_files)})."
        )
    return 0


def _cmd_inject(args: argparse.Namespace, store: SessionStore) -> int:
    if args.max_chars <= 0:
        print("Error: --max-chars must be a positive integer.", file=sys.stderr)
        return 2

    try:
        snapshot = store.load_snapshot(args.session_id)
    except (ValueError, CorruptSnapshotError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if snapshot is None:
        print(f"No snapshot found for session {args.session_id!r}.", file=sys.stderr)
        return 1

    if args.output_format == "json":
        print(_render_json(snapshot, args.max_chars))
    else:
        print(_render_markdown(snapshot, args.max_chars))
    return 0


def _cmd_pin(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        pin = store.add_manual_pin(args.session_id, args.text, tags=args.tags)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Pinned [{pin.pin_id}] for session {args.session_id!r}: {pin.text}")
    return 0


def _cmd_status(args: argparse.Namespace, store: SessionStore) -> int:
    if args.session_id is not None:
        try:
            snapshot = store.load_snapshot(args.session_id)
        except (ValueError, CorruptSnapshotError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if snapshot is None:
            print(f"No snapshot found for session {args.session_id!r}.", file=sys.stderr)
            return 1

        manual_pins = sum(1 for p in snapshot.pins if p.source == "manual")
        auto_pins = sum(1 for p in snapshot.pins if p.source != "manual")
        info = {
            "session_id": snapshot.session_id,
            "created_at": snapshot.created_at,
            "transcript_path": snapshot.transcript_path,
            "manual_pins": manual_pins,
            "auto_pins": auto_pins,
            "attempts": len(snapshot.attempts),
            "trajectories": len(snapshot.trajectories),
            "thrash_entries": len(snapshot.thrash_entries),
            "modified_files": len(snapshot.modified_files),
            "path": str(store.session_path(snapshot.session_id)),
        }
        if args.as_json:
            print(json.dumps(info, indent=2))
        else:
            print(f"Session: {info['session_id']}")
            print(f"Created: {info['created_at']}")
            print(f"Transcript: {info['transcript_path'] or '(none)'}")
            print(f"Pins: {manual_pins} manual, {auto_pins} auto")
            print(
                f"Ledger: {info['attempts']} attempts, "
                f"{info['trajectories']} trajectories, "
                f"{info['thrash_entries']} thrash entries"
            )
            print(f"Modified files: {info['modified_files']}")
            print(f"Store file: {info['path']}")
        return 0

    sessions = store.list_sessions()
    if args.as_json:
        print(json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2))
        return 0

    if not sessions:
        print("No sessions stored.")
        return 0

    print(f"Stored sessions ({len(sessions)}):")
    for sid in sessions:
        try:
            snap = store.load_snapshot(sid)
        except CorruptSnapshotError:
            snap = None
        if snap is None:
            print(f"- {sid}")
        else:
            manual = sum(1 for p in snap.pins if p.source == "manual")
            print(
                f"- {sid} (pins={len(snap.pins)}, manual={manual}, "
                f"files={len(snap.modified_files)}, created={snap.created_at})"
            )
    return 0


def _cmd_clear(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        removed = store.clear_session(args.session_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if removed:
        print(f"Cleared session {args.session_id!r}.")
    else:
        print(f"No stored snapshot found for session {args.session_id!r}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``compact-ledger`` CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    store = SessionStore(store_dir=args.store_dir)

    if args.command == "snapshot":
        return _cmd_snapshot(args, store)
    if args.command == "inject":
        return _cmd_inject(args, store)
    if args.command == "pin":
        return _cmd_pin(args, store)
    if args.command == "status":
        return _cmd_status(args, store)
    if args.command == "clear":
        return _cmd_clear(args, store)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
