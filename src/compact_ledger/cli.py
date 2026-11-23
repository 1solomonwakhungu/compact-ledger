"""Command-line interface for compact-ledger."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

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
from compact_ledger.models import PinEntry
from compact_ledger.parsers.transcript import SUPPORTED_FORMATS
from compact_ledger.renderer import DEFAULT_MAX_CHARS, render_snapshot
from compact_ledger.store import (
    ENV_CLAUDE_SESSION_ID,
    ENV_SESSION_ID,
    CorruptSnapshotError,
    SessionStore,
    default_session_id,
)

_SESSION_HELP = (
    f"Session identifier (default: ${ENV_SESSION_ID}, else ${ENV_CLAUDE_SESSION_ID}, "
    "which Claude Code sets for every shell command it runs)."
)

_MAIN_EPILOG = """\
quick start (Claude Code):
  compact-ledger init --write          wire the PreCompact + SessionStart hooks
  compact-ledger pin "Never touch db/migrations/" --negative
  compact-ledger status                see what is stored for this project

Inside Claude Code the session id is read from $CLAUDE_CODE_SESSION_ID, so an agent can
run pin / unpin / inject / status without --session-id.

exit codes: 0 ok, 1 not found or runtime error, 2 usage error. Hook commands always
exit 0 unless --strict is passed, so a hook problem never interrupts a session.
"""

_PIN_EPILOG = """\
examples:
  compact-ledger pin "Use July 31 as the billing cutoff"
  compact-ledger pin "Never push to main" --negative --tag git
  compact-ledger pin --session-id my-session "Zero runtime dependencies" --json

Pins outrank everything else in the injected ledger and survive every re-snapshot.
"""

_INJECT_EPILOG = """\
examples:
  compact-ledger inject                      what the agent will see after compaction
  compact-ledger inject --max-chars 1200     the same ledger under a tight budget
  compact-ledger inject --format json        machine-readable ledger
"""

_INIT_EPILOG = """\
examples:
  compact-ledger init                  preview the merged .claude/settings.json
  compact-ledger init --write          merge the hooks into .claude/settings.json
  compact-ledger init --write --command "uvx compact-ledger"

Existing hooks and settings are preserved; running it twice changes nothing.
"""


def _resolve_session_id(args: argparse.Namespace) -> str | None:
    """Return the explicit ``--session-id`` or the ambient one from the environment."""
    explicit = getattr(args, "session_id", None)
    if explicit is not None:
        return explicit
    return default_session_id()


def _missing_session_error() -> int:
    print(
        "Error: no session id. Pass --session-id <id>, or run inside Claude Code where "
        f"${ENV_CLAUDE_SESSION_ID} is set. `compact-ledger status` lists stored sessions.",
        file=sys.stderr,
    )
    return 2


def _configure_io_streams() -> None:
    """Ensure stdout/stderr tolerate non-ASCII text on narrow-encoding pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="replace")


def _neutralize_broken_stdio(stream: TextIO) -> None:
    """Prevent CPython shutdown from exiting 120 when stdout/stderr pipe breaks or is closed."""
    if stream not in (sys.__stdout__, sys.__stderr__):
        return
    with contextlib.suppress(OSError, ValueError):
        fd = stream.fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, fd)
        finally:
            os.close(devnull_fd)


def _safe_write_stream(stream: TextIO | None, text: str, *, fail_open: bool) -> None:
    """Write and flush ``text`` safely without raising BrokenPipeError or EBADF in fail-open."""
    if not text:
        return
    if stream is None:
        if not fail_open:
            raise OSError("Output stream is closed")
        return
    try:
        stream.write(text)
        stream.flush()
    except BrokenPipeError:
        _neutralize_broken_stdio(stream)
    except OSError:
        _neutralize_broken_stdio(stream)
        if not fail_open:
            raise


def _write_bounded_stdout(rendered: str, max_chars: int) -> None:
    """Write ``rendered`` ensuring total stdout length never exceeds ``max_chars``."""
    payload = rendered + "\n" if len(rendered) + 1 <= max_chars else rendered
    _safe_write_stream(sys.stdout, payload, fail_open=False)


def _is_fail_open_hook_argv(argv: Sequence[str]) -> bool:
    """Return True if ``argv`` invokes ``hook`` without ``--strict`` or ``--help``."""
    tokens = list(argv)
    if any(tok in ("--strict", "-h", "--help") for tok in tokens):
        return False

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--store-dir":
            idx += 2
            continue
        if tok.startswith("--store-dir="):
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return tok == "hook"
    return False


def _build_parser() -> argparse.ArgumentParser:
    store_help = (
        "Override the compact-ledger store directory "
        "(default: .compact-ledger or COMPACT_LEDGER_DIR)."
    )
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument(
        "--store-dir",
        default=argparse.SUPPRESS,
        help=store_help,
    )

    parser = argparse.ArgumentParser(
        prog="compact-ledger",
        description=(
            "Keep user rules, corrections and failed attempts alive across context "
            "compaction. Deterministic, local, no LLM calls."
        ),
        epilog=_MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
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
        description="Print the ledger that the SessionStart hook injects after compaction.",
        epilog=_INJECT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inject_parser.add_argument("--session-id", default=None, help=_SESSION_HELP)
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
        description=(
            "Pin a rule the agent must still follow after compaction. Use it the moment "
            "the user states a hard constraint or corrects an earlier instruction."
        ),
        epilog=_PIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pin_parser.add_argument("--session-id", default=None, help=_SESSION_HELP)
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
    pin_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output the stored pin as JSON.",
    )

    # unpin
    unpin_parser = subparsers.add_parser(
        "unpin",
        parents=[common],
        help="Remove a manual pin by pin ID or verbatim text.",
    )
    unpin_parser.add_argument("--session-id", default=None, help=_SESSION_HELP)
    unpin_parser.add_argument(
        "target",
        help="Pin ID or exact pin text to remove (`status --session-id <id>` lists both).",
    )

    # status
    status_parser = subparsers.add_parser(
        "status",
        parents=[common],
        help="Show status for one session or list all stored sessions.",
        description=(
            "List stored sessions, or describe one session (counts plus every manual pin "
            "with its ID). The current Claude Code session is marked with '*'."
        ),
    )
    status_parser.add_argument(
        "--session-id",
        default=None,
        help="Session to describe, including its pins (omit to list all sessions).",
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
    clear_parser.add_argument("--session-id", default=None, help=_SESSION_HELP)

    # hook pre-compact / session-start
    hook_parser = subparsers.add_parser(
        "hook",
        parents=[common],
        allow_abbrev=False,
        help="Run Claude Code lifecycle hooks (pre-compact / session-start).",
    )
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command")

    pre_compact_parser = hook_subparsers.add_parser(
        "pre-compact",
        parents=[common],
        allow_abbrev=False,
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
        allow_abbrev=False,
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
        description=(
            "Wire the PreCompact and SessionStart hooks into Claude Code. Prints the merged "
            "settings by default; pass --write to save them."
        ),
        epilog=_INIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init_parser.add_argument(
        "--claude-code",
        action="store_true",
        default=False,
        help="Target Claude Code (the default and currently the only integration).",
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

    session_id = _resolve_session_id(args)
    if session_id is None:
        return _missing_session_error()

    try:
        snapshot = store.load_snapshot(session_id)
    except CorruptSnapshotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if snapshot is None:
        print(_no_snapshot_message(session_id, store), file=sys.stderr)
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


def _no_snapshot_message(session_id: str, store: SessionStore) -> str:
    return (
        f"No snapshot found for session {session_id!r} in {store.root_dir}. "
        "One is written when the PreCompact hook fires, or by `compact-ledger snapshot` / `pin`."
    )


def _pin_to_dict(pin: PinEntry) -> dict[str, object]:
    return {
        "pin_id": pin.pin_id,
        "text": pin.text,
        "is_negative": pin.is_negative,
        "tags": list(pin.tags),
    }


def _cmd_pin(args: argparse.Namespace, store: SessionStore) -> int:
    session_id = _resolve_session_id(args)
    if session_id is None:
        return _missing_session_error()

    try:
        pin = store.add_manual_pin(
            session_id,
            args.text,
            tags=args.tags,
            is_negative=args.negative,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        payload = {
            "session_id": session_id,
            **_pin_to_dict(pin),
            "path": str(store.session_path(session_id)),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Pinned [{pin.pin_id}] for session {session_id!r}: {pin.text}")
    return 0


def _cmd_unpin(args: argparse.Namespace, store: SessionStore) -> int:
    session_id = _resolve_session_id(args)
    if session_id is None:
        return _missing_session_error()

    try:
        removed = store.remove_manual_pin(session_id, args.target)
    except (ValueError, CorruptSnapshotError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if removed:
        print(f"Removed manual pin from session {session_id!r}.")
        return 0
    print(
        f"No matching manual pin found in session {session_id!r}. "
        f"`compact-ledger status --session-id {shlex.quote(session_id)}` lists pin IDs and text.",
        file=sys.stderr,
    )
    return 1


def _cmd_status(args: argparse.Namespace, store: SessionStore) -> int:
    if args.session_id is not None:
        try:
            snapshot = store.load_snapshot(args.session_id)
        except CorruptSnapshotError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        if snapshot is None:
            print(_no_snapshot_message(args.session_id, store), file=sys.stderr)
            return 1

        manual_entries = [p for p in snapshot.pins if p.source == "manual"]
        manual_pins = len(manual_entries)
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
            "pins": [_pin_to_dict(p) for p in manual_entries],
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
            if manual_entries:
                print("Manual pins:")
                for pin in manual_entries:
                    flags = " [DO NOT]" if pin.is_negative else ""
                    tags = f" [tags: {', '.join(pin.tags)}]" if pin.tags else ""
                    print(f"- {pin.pin_id}{flags}{tags} {' '.join(pin.text.split())}")
        return 0

    sessions = store.list_sessions()
    current = default_session_id()
    if args.as_json:
        listing = {
            "sessions": sessions,
            "count": len(sessions),
            "store_dir": str(store.root_dir),
            "current_session_id": current,
        }
        print(json.dumps(listing, indent=2, ensure_ascii=False))
        return 0

    if not sessions:
        print(f"No sessions stored. Store: {store.root_dir}")
        return 0

    print(f"Store: {store.root_dir}")
    print(f"Stored sessions ({len(sessions)}):")
    for sid in sessions:
        marker = "*" if sid == current else "-"
        try:
            snap = store.load_snapshot(sid)
        except (ValueError, CorruptSnapshotError):
            snap = None
        if snap is None:
            print(f"{marker} {sid}")
        else:
            manual = sum(1 for p in snap.pins if p.source == "manual")
            print(
                f"{marker} {sid} (pins={len(snap.pins)}, manual={manual}, "
                f"trajectories={len(snap.trajectories)}, "
                f"files={len(snap.modified_files)}, created={snap.created_at})"
            )
    return 0


def _cmd_clear(args: argparse.Namespace, store: SessionStore) -> int:
    session_id = _resolve_session_id(args)
    if session_id is None:
        return _missing_session_error()

    try:
        removed = store.clear_session(session_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if removed:
        print(f"Cleared session {session_id!r}.")
    else:
        print(f"No stored snapshot found for session {session_id!r}.")
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    hook_cmd = getattr(args, "hook_command", None)
    strict = bool(getattr(args, "strict", False))

    if hook_cmd == "pre-compact":
        result = handle_pre_compact(
            sys.stdin,
            store_dir=args.store_dir,
            strict=strict,
        )
    elif hook_cmd == "session-start":
        result = handle_session_start(
            sys.stdin,
            store_dir=args.store_dir,
            max_chars=args.max_chars,
            always=args.always,
            strict=strict,
        )
    else:
        _safe_write_stream(
            sys.stderr,
            "Error: specify a hook subcommand: 'pre-compact' or 'session-start'.\n",
            fail_open=True,
        )
        return 0 if not strict else 2

    _safe_write_stream(sys.stderr, result.stderr, fail_open=True)
    _safe_write_stream(sys.stdout, result.stdout, fail_open=not strict)
    return result.exit_code


def _cmd_init(args: argparse.Namespace) -> int:
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
        print(
            "Next: restart Claude Code (or open /hooks) so it loads the hooks, run /compact "
            "once, then check `compact-ledger status`."
        )
    else:
        print(json.dumps(merged, indent=2, ensure_ascii=False, allow_nan=False))
        print("Preview only. Re-run with --write to save these settings.", file=sys.stderr)

    missing = _missing_hook_executable(args.init_command)
    if missing is not None:
        print(
            f"Warning: {missing!r} is not on PATH, so these hooks will do nothing until it "
            "is. Install it (`uv tool install "
            "git+https://github.com/1solomonwakhungu/compact-ledger.git`) or pass "
            "--command with a runnable prefix.",
            file=sys.stderr,
        )
    return 0


def _missing_hook_executable(command_prefix: str) -> str | None:
    """Return the hook executable name when it cannot be found on PATH."""
    try:
        tokens = shlex.split(command_prefix)
    except ValueError:
        return None
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", ".", "~")):
        tokens.pop(0)
    if not tokens:
        return None
    executable = os.path.expanduser(tokens[0])
    return None if shutil.which(executable) else tokens[0]


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
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    hook_fail_open = _is_fail_open_hook_argv(effective_argv)

    try:
        err_ctx = (
            contextlib.redirect_stderr(io.StringIO())
            if hook_fail_open and sys.stderr is None
            else contextlib.nullcontext()
        )
        with err_ctx:
            args = parser.parse_args(effective_argv)
    except SystemExit as exc:
        if hook_fail_open and exc.code not in (0, None):
            if sys.stderr is not None:
                try:
                    sys.stderr.flush()
                except OSError:
                    _neutralize_broken_stdio(sys.stderr)
            return 0
        raise
    except (OSError, AttributeError):
        if hook_fail_open:
            if sys.stderr is not None:
                _neutralize_broken_stdio(sys.stderr)
            return 0
        raise

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
        _neutralize_broken_stdio(sys.stdout)
        return 0
    except (OSError, ValueError) as exc:
        if hook_fail_open:
            _safe_write_stream(sys.stderr, f"compact-ledger hook: {exc}\n", fail_open=True)
            return 0
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
