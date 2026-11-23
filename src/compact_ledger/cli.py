"""Command-line interface for compact-ledger."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from compact_ledger import __version__
from compact_ledger.evals import (
    format_eval_report_json,
    format_eval_report_text,
    run_eval_suite,
)
from compact_ledger.hooks import (
    DEFAULT_CLAUDE_COMMAND,
    create_and_save_snapshot,
    handle_pre_compact,
    handle_session_start,
    init_claude_code_settings,
)
from compact_ledger.parsers.transcript import SUPPORTED_FORMATS
from compact_ledger.renderer import DEFAULT_MAX_CHARS, render_snapshot
from compact_ledger.store import CorruptSnapshotError, SessionStore


def _configure_io_streams() -> None:
    """Ensure stdout/stderr tolerate non-ASCII text on narrow-encoding pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="replace")


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

    # snapshot
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

    # inject
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

    # pin
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

    # unpin
    unpin_parser = subparsers.add_parser(
        "unpin",
        parents=[common],
        help="Remove a manual pin by pin ID or verbatim text.",
    )
    unpin_parser.add_argument("--session-id", required=True, help="Session identifier.")
    unpin_parser.add_argument("target", help="Pin ID or exact pin text to remove.")

    # status
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

    # clear
    clear_parser = subparsers.add_parser(
        "clear",
        parents=[common],
        help="Delete a stored session snapshot.",
    )
    clear_parser.add_argument("--session-id", required=True, help="Session identifier.")

    # hook pre-compact / session-start
    hook_parser = subparsers.add_parser(
        "hook",
        parents=[common],
        help="Run Claude Code lifecycle hooks (pre-compact / session-start).",
    )
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command")

    pre_compact_parser = hook_subparsers.add_parser(
        "pre-compact",
        parents=[common],
        help="Read Claude Code PreCompact JSON from stdin and save a snapshot.",
    )
    pre_compact_parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Exit non-zero on malformed stdin or errors (default: fail-open exit 0).",
    )

    session_start_parser = hook_subparsers.add_parser(
        "session-start",
        parents=[common],
        help="Read Claude Code SessionStart JSON from stdin and inject the bounded ledger.",
    )
    session_start_parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"Maximum rendered ledger character budget (default: {DEFAULT_MAX_CHARS}).",
    )
    session_start_parser.add_argument(
        "--always",
        action="store_true",
        default=False,
        help="Inject even when SessionStart source is not 'compact' or 'resume'.",
    )
    session_start_parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Exit non-zero on malformed stdin or errors (default: fail-open exit 0).",
    )

    # init
    init_parser = subparsers.add_parser(
        "init",
        help="Generate or safely merge agent hook settings.",
    )
    init_parser.add_argument(
        "--claude-code",
        action="store_true",
        default=False,
        help="Generate or merge Claude Code .claude/settings.json hook entries.",
    )
    init_parser.add_argument(
        "--write",
        action="store_true",
        default=False,
        help="Atomically write merged settings to disk instead of only printing to stdout.",
    )
    init_parser.add_argument(
        "--settings-path",
        default=None,
        help="Path to settings.json (default: .claude/settings.json in current directory).",
    )
    init_parser.add_argument(
        "--command",
        dest="init_command",
        default=DEFAULT_CLAUDE_COMMAND,
        help=f"Command prefix for hook invocation (default: {DEFAULT_CLAUDE_COMMAND!r}).",
    )

    # eval
    eval_parser = subparsers.add_parser(
        "eval",
        help="Run the seeded 20-case deterministic compaction evaluation suite.",
    )
    eval_parser.add_argument(
        "--format",
        dest="eval_format",
        choices=("text", "json"),
        default="text",
        help="Evaluation report format (default: text).",
    )
    eval_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Include case descriptions in text output.",
    )

    return parser


def _cmd_snapshot(args: argparse.Namespace, store: SessionStore) -> int:
    transcript_path = Path(args.transcript)
    if not transcript_path.is_file():
        print(f"Error: transcript file not found: {transcript_path}", file=sys.stderr)
        return 1

    try:
        saved, parsed = create_and_save_snapshot(
            transcript_path,
            store,
            session_id=args.session_id,
            transcript_format=args.transcript_format,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    manual_count = sum(1 for p in saved.pins if p.source == "manual")
    override_count = sum(
        1 for p in saved.pins if p.source == "override" and p.superseded_by is None
    )

    if args.as_json:
        summary = {
            "session_id": saved.session_id,
            "path": str(store.session_path(saved.session_id)),
            "format": parsed.format,
            "pins": len(saved.pins),
            "manual_pins": manual_count,
            "override_pins": override_count,
            "attempts": len(saved.attempts),
            "trajectories": len(saved.trajectories),
            "thrash_entries": len(saved.thrash_entries),
            "modified_files": len(saved.modified_files),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"Saved snapshot for session {saved.session_id!r} "
            f"(format={parsed.format}, pins={len(saved.pins)}, "
            f"manual_pins={manual_count}, overrides={override_count}, "
            f"trajectories={len(saved.trajectories)}, thrash={len(saved.thrash_entries)}, "
            f"modified_files={len(saved.modified_files)})."
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
    else:
        budget = args.max_chars - 1 if args.max_chars >= 2 else args.max_chars

    rendered = render_snapshot(
        snapshot,
        max_chars=budget,
        output_format=args.output_format,
    )
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
        override_pins = sum(
            1 for p in snapshot.pins if p.source == "override" and p.superseded_by is None
        )
        auto_pins = sum(1 for p in snapshot.pins if p.source != "manual")
        info = {
            "session_id": snapshot.session_id,
            "created_at": snapshot.created_at,
            "transcript_path": snapshot.transcript_path,
            "manual_pins": manual_pins,
            "auto_pins": auto_pins,
            "override_pins": override_pins,
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
            print(
                f"Pins: {manual_pins} manual, {auto_pins} auto ({override_pins} active overrides)"
            )
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
                f"trajectories={len(snap.trajectories)}, "
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


def _cmd_hook(args: argparse.Namespace) -> int:
    hook_cmd = getattr(args, "hook_command", None)
    if hook_cmd == "pre-compact":
        result = handle_pre_compact(
            sys.stdin,
            store_dir=args.store_dir,
            strict=args.strict,
        )
    elif hook_cmd == "session-start":
        result = handle_session_start(
            sys.stdin,
            store_dir=args.store_dir,
            max_chars=args.max_chars,
            always=args.always,
            strict=args.strict,
        )
    else:
        print(
            "Error: specify a hook subcommand: 'pre-compact' or 'session-start'.",
            file=sys.stderr,
        )
        return 2

    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result.exit_code


def _cmd_init(args: argparse.Namespace) -> int:
    if not args.claude_code:
        print(
            "Error: specify a target integration (currently supported: --claude-code).",
            file=sys.stderr,
        )
        return 2

    try:
        merged, target_path, wrote = init_claude_code_settings(
            settings_path=args.settings_path,
            write=args.write,
            command=args.init_command,
        )
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if wrote:
        print(f"Updated Claude Code hook settings in {target_path}")
    else:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    report = run_eval_suite()
    if args.eval_format == "json":
        print(format_eval_report_json(report))
    else:
        print(format_eval_report_text(report, verbose=args.verbose))
    return 0 if report.failed == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``compact-ledger`` CLI."""
    _configure_io_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "hook":
            return _cmd_hook(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "eval":
            return _cmd_eval(args)

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
