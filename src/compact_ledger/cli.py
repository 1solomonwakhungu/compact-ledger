"""Command-line interface for compact-ledger."""

from __future__ import annotations

import argparse
import contextlib
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
    sanitize_unicode,
)
from compact_ledger.parsers.transcript import SUPPORTED_FORMATS, parse_transcript
from compact_ledger.store import CorruptSnapshotError, SessionStore

DEFAULT_MAX_CHARS = 6000


def _configure_io_streams() -> None:
    """Ensure stdout/stderr tolerate non-ASCII text on narrow-encoding pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="replace")


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
                text=sanitize_unicode(turn.text).strip(),
                source="auto",
                turn_index=turn.turn_index,
                timestamp=turn.timestamp,
            )
        )
    return pins


def _flatten_single_line(text: str) -> str:
    """Flatten embedded newlines so user text cannot spoof Markdown headings."""
    return " ".join(sanitize_unicode(text).splitlines()).strip()


def _truncate_line(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _render_markdown(snapshot: CompactionSnapshot, max_chars: int) -> str:
    """Render a bounded Markdown summary prioritizing manual pins over auto pins and files."""
    if max_chars <= 0:
        return ""

    header = f"## Compact Ledger (Session: {_flatten_single_line(snapshot.session_id)})"
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
            flat_item = _flatten_single_line(item)
            entry = f"\n- {flat_item}"
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
                if remaining >= 12:
                    if not added_header:
                        lines.append(section_header)
                        used += len(section_header)
                        added_header = True
                    truncated = _truncate_line(flat_item, remaining)
                    entry = f"\n- {truncated}"
                    lines.append(entry)
                    used += len(entry)
                break

    manual_pins = [p for p in snapshot.pins if p.source == "manual"]
    override_pins = list(reversed([p for p in snapshot.pins if p.source == "override"]))
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


def _compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _truncate_pin_for_json_budget(pin_dict: dict[str, Any], max_item_chars: int) -> str | None:
    """Truncate a pin dict's ``text`` field so its compact JSON fits in ``max_item_chars``."""
    base = dict(pin_dict)
    base["text"] = ""
    empty_len = len(_compact_json(base))
    if empty_len + 4 > max_item_chars:
        return None

    original_text = str(pin_dict.get("text", ""))
    # Binary search on prefix length to account accurately for JSON escaping.
    lo, hi = 1, len(original_text)
    best: str | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = dict(pin_dict)
        candidate["text"] = _truncate_line(original_text, mid)
        serialized = _compact_json(candidate)
        if len(serialized) <= max_item_chars:
            best = serialized
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _render_json(snapshot: CompactionSnapshot, max_chars: int) -> str:
    """Render valid JSON strictly bounded to ``<= max_chars`` in O(N) time.

    Priority order matches PLAN.md:
    (1) Manual pins & active overrides, (2) Remaining auto pins (newest first),
    (3) Trajectories / thrash / attempts, (4) Modified files.
    """
    if max_chars < 2:
        raise ValueError("--max-chars must be at least 2 for JSON output")

    total_items = (
        len(snapshot.pins)
        + len(snapshot.attempts)
        + len(snapshot.trajectories)
        + len(snapshot.thrash_entries)
        + len(snapshot.modified_files)
    )
    if total_items <= 200:
        pretty = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False)
        if len(pretty) <= max_chars:
            return pretty

    # Strict PLAN priority: manual (in order) -> override (newest first) -> auto (newest first).
    manual_pins = [p.to_dict() for p in snapshot.pins if p.source == "manual"]
    override_pins = [p.to_dict() for p in reversed(snapshot.pins) if p.source == "override"]
    auto_pins = [p.to_dict() for p in reversed(snapshot.pins) if p.source == "auto"]
    ordered_pins: list[tuple[bool, dict[str, Any]]] = [
        *((True, p) for p in manual_pins),
        *((False, p) for p in override_pins),
        *((False, p) for p in auto_pins),
    ]

    # Pick the richest JSON envelope whose empty skeleton fits within max_chars.
    envelopes: list[dict[str, Any]] = [
        {
            "schema_version": snapshot.schema_version,
            "session_id": snapshot.session_id,
            "created_at": snapshot.created_at,
            "transcript_path": snapshot.transcript_path,
            "pins": [],
            "attempts": [],
            "trajectories": [],
            "thrash_entries": [],
            "modified_files": [],
            "metadata": {},
        },
        {
            "schema_version": snapshot.schema_version,
            "session_id": snapshot.session_id,
            "created_at": snapshot.created_at,
            "pins": [],
            "attempts": [],
            "trajectories": [],
            "thrash_entries": [],
            "modified_files": [],
        },
        {
            "session_id": snapshot.session_id,
            "pins": [],
        },
        {
            "pins": [],
        },
    ]

    def _try_envelope(template: dict[str, Any]) -> tuple[str, int] | None:
        env: dict[str, Any] = {
            k: ([] if isinstance(v, list) else ({} if isinstance(v, dict) else v))
            for k, v in template.items()
        }
        used = len(_compact_json(env))
        if used > max_chars:
            return None

        def _fill_list(key: str, items: Sequence[tuple[bool, Any]]) -> None:
            nonlocal used
            if key not in env:
                return
            bucket: list[Any] = env[key]
            for allow_truncate, item in items:
                sep = 1 if bucket else 0
                serialized = _compact_json(item)
                needed = sep + len(serialized)
                if used + needed <= max_chars:
                    bucket.append(item)
                    used += needed
                elif allow_truncate and isinstance(item, dict) and "text" in item:
                    remaining = max_chars - used - sep
                    truncated_json = _truncate_pin_for_json_budget(item, remaining)
                    if truncated_json is not None:
                        bucket.append(json.loads(truncated_json))
                        used += sep + len(truncated_json)
                    break
                else:
                    break

        _fill_list("pins", ordered_pins)
        if "trajectories" in env:
            _fill_list("trajectories", [(False, t.to_dict()) for t in snapshot.trajectories])
        if "thrash_entries" in env:
            _fill_list("thrash_entries", [(False, e.to_dict()) for e in snapshot.thrash_entries])
        if "attempts" in env:
            _fill_list("attempts", [(False, a.to_dict()) for a in snapshot.attempts])
        if "modified_files" in env:
            _fill_list("modified_files", [(False, f) for f in snapshot.modified_files])
        return _compact_json(env), len(env.get("pins", []))

    best_result: str | None = None
    best_pin_count = -1
    want_pins = len(ordered_pins) > 0
    for env_template in envelopes:
        attempt = _try_envelope(env_template)
        if attempt is None:
            continue
        rendered_candidate, pin_count = attempt
        if not want_pins:
            return rendered_candidate
        # Prefer the richest envelope that keeps all manual pins (or maximizes included pins).
        if pin_count >= len(manual_pins) and pin_count > 0:
            return rendered_candidate
        if pin_count > best_pin_count:
            best_pin_count = pin_count
            best_result = rendered_candidate

    return best_result if best_result is not None else "{}"


def _write_bounded_stdout(rendered: str, max_chars: int) -> None:
    """Write ``rendered`` ensuring total stdout length never exceeds ``max_chars``."""
    if len(rendered) + 1 <= max_chars:
        sys.stdout.write(rendered + "\n")
    else:
        sys.stdout.write(rendered)


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
    pin_parser.add_argument(
        "--negative",
        action="store_true",
        default=False,
        help="Mark this pin as a negative constraint (do-not / never rule).",
    )

    unpin_parser = subparsers.add_parser(
        "unpin",
        parents=[common],
        help="Remove a manual pin by pin ID or verbatim text.",
    )
    unpin_parser.add_argument("--session-id", required=True, help="Session identifier.")
    unpin_parser.add_argument("target", help="Pin ID or exact pin text to remove.")

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
        print(json.dumps(summary, indent=2, ensure_ascii=False))
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
    if args.output_format == "json" and args.max_chars < 2:
        print("Error: --max-chars must be at least 2 for JSON output.", file=sys.stderr)
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
        budget = args.max_chars - 1 if args.max_chars >= 3 else args.max_chars
        rendered = _render_json(snapshot, budget)
    else:
        budget = args.max_chars - 1 if args.max_chars >= 2 else args.max_chars
        rendered = _render_markdown(snapshot, budget)

    _write_bounded_stdout(rendered, args.max_chars)
    return 0


def _cmd_pin(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        pin = store.add_manual_pin(
            args.session_id,
            args.text,
            tags=args.tags,
            is_negative=args.negative,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Pinned [{pin.pin_id}] for session {args.session_id!r}: {pin.text}")
    return 0


def _cmd_unpin(args: argparse.Namespace, store: SessionStore) -> int:
    try:
        removed = store.remove_manual_pin(args.session_id, args.target)
    except (ValueError, CorruptSnapshotError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if removed:
        print(f"Removed manual pin from session {args.session_id!r}.")
        return 0
    print(f"No matching manual pin found in session {args.session_id!r}.", file=sys.stderr)
    return 1


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
            print(json.dumps(info, indent=2, ensure_ascii=False))
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
        except (ValueError, CorruptSnapshotError):
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
    _configure_io_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        store = SessionStore(store_dir=args.store_dir)
        if args.command == "snapshot":
            return _cmd_snapshot(args, store)
        if args.command == "inject":
            return _cmd_inject(args, store)
        if args.command == "pin":
            return _cmd_pin(args, store)
        if args.command == "unpin":
            return _cmd_unpin(args, store)
        if args.command == "status":
            return _cmd_status(args, store)
        if args.command == "clear":
            return _cmd_clear(args, store)
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
