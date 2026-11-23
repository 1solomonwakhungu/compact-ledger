"""Claude Code hook handlers, end-to-end snapshot pipeline, and settings init.

All hook handlers are deterministic, zero-LLM, stdlib-only, and strictly fail-open
(exit 0 without polluting stdout on malformed input or unexpected internal errors)
unless ``strict=True`` is explicitly requested.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from compact_ledger.ledger import build_attempt_ledger
from compact_ledger.models import CompactionSnapshot, ParsedTranscript, PinEntry, sanitize_unicode
from compact_ledger.parsers.transcript import parse_transcript
from compact_ledger.pins import extract_pins
from compact_ledger.renderer import DEFAULT_MAX_CHARS, render_snapshot
from compact_ledger.store import (
    ENV_STORE_DIR,
    CorruptSnapshotError,
    SessionStore,
    find_project_root,
)

__all__ = [
    "DEFAULT_CLAUDE_COMMAND",
    "DEFAULT_CLAUDE_SETTINGS_RELPATH",
    "SESSION_START_SOURCES",
    "HookResult",
    "build_claude_code_hooks_config",
    "build_snapshot_from_transcript",
    "create_and_save_snapshot",
    "handle_pre_compact",
    "handle_session_start",
    "init_claude_code_settings",
    "merge_claude_code_settings",
    "resolve_hook_store",
]

DEFAULT_CLAUDE_COMMAND = "compact-ledger"
DEFAULT_CLAUDE_SETTINGS_RELPATH = Path(".claude") / "settings.json"
SESSION_START_SOURCES: frozenset[str] = frozenset({"compact", "resume"})
SESSION_START_MATCHER = "compact|resume"
ENV_CLAUDE_PROJECT_DIR = "CLAUDE_PROJECT_DIR"
_MAX_STDIN_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap for hook stdin payloads
_COMPACTION_MATCHER_RE = re.compile(r"\b(?:compact|resume)\b", re.IGNORECASE)
_SHELL_META_RE = re.compile(r"(?:&&|\|\||[;|><`$])")
_ENV_ASSIGN_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(slots=True)
class HookResult:
    """Structured outcome of a hook invocation."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _read_stream_bytes(stream: TextIO | BinaryIO, *, max_bytes: int = _MAX_STDIN_BYTES) -> bytes:
    """Read up to ``max_bytes + 1`` bytes from a text or binary stream safely."""
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        raw = buf.read(max_bytes + 1)
        if isinstance(raw, bytes):
            return raw
        return bytes(raw)

    data = stream.read(max_bytes + 1)
    if isinstance(data, bytes):
        return data
    return str(data).encode("utf-8", errors="surrogatepass")


def _parse_hook_stdin(raw_input: str | bytes | TextIO | BinaryIO | None) -> dict[str, Any]:
    """Parse a Claude Code hook JSON object payload from raw bytes, text, or a stream."""
    if raw_input is None:
        raise ValueError("Hook stdin stream is closed")
    if isinstance(raw_input, bytes):
        if len(raw_input) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        try:
            text = raw_input.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Hook stdin is not valid UTF-8: {exc}") from exc
    elif isinstance(raw_input, str):
        if len(raw_input.encode("utf-8", errors="surrogatepass")) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        text = raw_input.removeprefix("\ufeff")
    else:
        raw_bytes = _read_stream_bytes(raw_input)
        if len(raw_bytes) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Hook stdin is not valid UTF-8: {exc}") from exc

    stripped = text.strip()
    if not stripped:
        raise ValueError("Hook stdin payload is empty")

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Hook stdin is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Hook stdin JSON must be an object, got {type(parsed).__name__}")
    return parsed


def _is_valid_existing_dir(candidate: Any) -> Path | None:
    """Return expanded ``Path`` if ``candidate`` is a valid existing directory, else ``None``."""
    if not isinstance(candidate, str) or not candidate.strip() or "\x00" in candidate:
        return None
    try:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    except OSError:
        return None
    return None


def resolve_hook_store(
    *,
    store_dir: str | Path | None = None,
    payload_cwd: Any = None,
    env: Mapping[str, str] | None = None,
    fallback_cwd: Path | None = None,
) -> SessionStore:
    """Resolve ``SessionStore`` for a hook invocation.

    Precedence:
    1. Explicit ``store_dir`` (``--store-dir``)
    2. ``COMPACT_LEDGER_DIR`` environment variable (when non-whitespace)
    3. The project root found from ``CLAUDE_PROJECT_DIR``, else the payload ``cwd``, else
       ``fallback_cwd`` / the current working directory. See :func:`find_project_root`:
       the session's cwd moves whenever the agent changes directory, so the store is
       anchored to the nearest existing ``.compact-ledger`` or repository root instead.
    """
    if store_dir is not None:
        return SessionStore(store_dir=store_dir)

    env_map = os.environ if env is None else env
    env_dir = env_map.get(ENV_STORE_DIR)
    if env_dir and env_dir.strip():
        return SessionStore(store_dir=env_dir.strip())

    return SessionStore(
        base_dir=find_project_root(
            resolve_project_dir(payload_cwd, env=env_map, fallback_cwd=fallback_cwd)
        )
    )


def resolve_project_dir(
    payload_cwd: Any = None,
    *,
    env: Mapping[str, str] | None = None,
    fallback_cwd: Path | None = None,
) -> Path:
    """Return the best-known project directory for a hook invocation."""
    env_map = os.environ if env is None else env
    for candidate in (env_map.get(ENV_CLAUDE_PROJECT_DIR), payload_cwd):
        valid = _is_valid_existing_dir(candidate)
        if valid is not None:
            return valid
    return fallback_cwd if fallback_cwd is not None else Path.cwd()


def build_snapshot_from_transcript(
    parsed: ParsedTranscript,
    *,
    session_id: str,
    transcript_path: str | Path | None = None,
    manual_pins: list[PinEntry] | tuple[PinEntry, ...] = (),
    extra_metadata: Mapping[str, Any] | None = None,
) -> CompactionSnapshot:
    """Run the full deterministic pin + attempt ledger pipeline on a parsed transcript."""
    pins = extract_pins(parsed.user_turns, manual_pins=manual_pins)
    attempts, trajectories, thrash_entries = build_attempt_ledger(parsed)

    metadata: dict[str, Any] = {
        "transcript_format": parsed.format,
        "user_turn_count": len(parsed.user_turns),
        "tool_execution_count": len(parsed.tool_executions),
        "file_edit_count": len(parsed.file_edits),
    }
    if extra_metadata:
        for key, value in extra_metadata.items():
            if value is not None:
                metadata[str(key)] = value

    resolved_path: str | None = None
    if transcript_path is not None:
        try:
            resolved_path = str(Path(transcript_path).expanduser().resolve())
        except OSError:
            resolved_path = str(transcript_path)

    return CompactionSnapshot(
        session_id=session_id,
        transcript_path=resolved_path,
        pins=pins,
        attempts=attempts,
        trajectories=trajectories,
        thrash_entries=thrash_entries,
        modified_files=list(parsed.modified_files),
        metadata=metadata,
    )


def create_and_save_snapshot(
    transcript_path: str | Path,
    store: SessionStore,
    *,
    session_id: str | None = None,
    transcript_format: str = "auto",
    extra_metadata: Mapping[str, Any] | None = None,
) -> tuple[CompactionSnapshot, ParsedTranscript]:
    """Parse ``transcript_path``, build the complete snapshot, and persist it atomically."""
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {path}")

    parsed = parse_transcript(path, fmt=transcript_format, session_id=session_id)
    effective_session_id = session_id or parsed.session_id
    if not effective_session_id or not effective_session_id.strip():
        if not (parsed.user_turns or parsed.tool_executions or parsed.file_edits):
            raise ValueError(
                f"No transcript records could be parsed from {path} (expected a Claude Code "
                "or Codex JSONL transcript). Pass --session-id to store an empty snapshot anyway."
            )
        raise ValueError("--session-id is required when transcript has no embedded session_id.")

    # Load existing manual pins if present; tolerate corrupt files so save_snapshot can quarantine.
    existing_manual: list[PinEntry] = []
    try:
        existing_manual = store.list_manual_pins(effective_session_id)
    except CorruptSnapshotError:
        existing_manual = []

    snapshot = build_snapshot_from_transcript(
        parsed,
        session_id=effective_session_id,
        transcript_path=path,
        manual_pins=existing_manual,
        extra_metadata=extra_metadata,
    )
    saved = store.save_snapshot(snapshot)
    return saved, parsed


def _resolve_transcript_path(raw_path: str, payload_cwd: Any) -> Path:
    if "\x00" in raw_path:
        raise ValueError("transcript_path must not contain null bytes")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        valid_cwd = _is_valid_existing_dir(payload_cwd)
        if valid_cwd is not None:
            candidate = valid_cwd / path
            if candidate.is_file():
                return candidate
    return path


def _is_snapshot_non_empty(snapshot: CompactionSnapshot) -> bool:
    return bool(
        snapshot.pins
        or snapshot.trajectories
        or snapshot.thrash_entries
        or snapshot.attempts
        or snapshot.modified_files
    )


def handle_pre_compact(
    raw_input: str | bytes | TextIO | BinaryIO | None,
    *,
    store_dir: str | Path | None = None,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
    fallback_cwd: Path | None = None,
) -> HookResult:
    """Process a Claude Code ``PreCompact`` hook payload from stdin.

    Fail-open by default (exit 0, empty stdout) unless ``strict=True``.
    """
    try:
        payload = _parse_hook_stdin(raw_input)
        raw_sid = payload.get("session_id")
        raw_tpath = payload.get("transcript_path")
        if not isinstance(raw_sid, str) or not raw_sid.strip():
            raise ValueError("PreCompact payload missing non-empty 'session_id'")
        if not isinstance(raw_tpath, str) or not raw_tpath.strip():
            raise ValueError("PreCompact payload missing non-empty 'transcript_path'")

        payload_cwd = payload.get("cwd")
        transcript_path = _resolve_transcript_path(raw_tpath.strip(), payload_cwd)
        store = resolve_hook_store(
            store_dir=store_dir,
            payload_cwd=payload_cwd,
            env=env,
            fallback_cwd=fallback_cwd,
        )

        extra_meta: dict[str, Any] = {"hook_event": "PreCompact"}
        with contextlib.suppress(OSError, RuntimeError):
            project_dir = resolve_project_dir(payload_cwd, env=env, fallback_cwd=fallback_cwd)
            extra_meta["project_dir"] = sanitize_unicode(str(project_dir.resolve()))
        trigger = payload.get("trigger")
        if isinstance(trigger, str) and trigger.strip():
            extra_meta["trigger"] = sanitize_unicode(trigger.strip())

        create_and_save_snapshot(
            transcript_path,
            store,
            session_id=raw_sid.strip(),
            transcript_format="auto",
            extra_metadata=extra_meta,
        )
        return HookResult(exit_code=0)
    except Exception as exc:
        msg = f"compact-ledger pre-compact: {sanitize_unicode(str(exc))}\n"
        return HookResult(exit_code=1 if strict else 0, stdout="", stderr=msg)


def handle_session_start(
    raw_input: str | bytes | TextIO | BinaryIO | None,
    *,
    store_dir: str | Path | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    always: bool = False,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
    fallback_cwd: Path | None = None,
) -> HookResult:
    """Process a Claude Code ``SessionStart`` hook payload from stdin.

    Injects the bounded Markdown ledger via ``hookSpecificOutput.additionalContext``
    only when ``source`` is ``"compact"`` or ``"resume"`` (case-insensitive) or
    ``always=True``, and a non-empty snapshot exists for ``session_id``.
    Fail-open by default (exit 0, empty stdout) unless ``strict=True``.
    """
    try:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("--max-chars must be a positive integer")

        payload = _parse_hook_stdin(raw_input)
        raw_source = payload.get("source")
        source_norm = raw_source.strip().lower() if isinstance(raw_source, str) else ""

        if not always and source_norm not in SESSION_START_SOURCES:
            return HookResult(exit_code=0)

        raw_sid = payload.get("session_id")
        if not isinstance(raw_sid, str) or not raw_sid.strip():
            raise ValueError("SessionStart payload missing non-empty 'session_id'")

        store = resolve_hook_store(
            store_dir=store_dir,
            payload_cwd=payload.get("cwd"),
            env=env,
            fallback_cwd=fallback_cwd,
        )
        snapshot = store.load_snapshot(raw_sid.strip())
        if snapshot is None or not _is_snapshot_non_empty(snapshot):
            return HookResult(exit_code=0)

        rendered = render_snapshot(snapshot, max_chars=max_chars, output_format="markdown")
        if not rendered.strip():
            return HookResult(exit_code=0)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": rendered,
            }
        }
        return HookResult(
            exit_code=0,
            stdout=sanitize_unicode(json.dumps(output, ensure_ascii=False)) + "\n",
        )
    except Exception as exc:
        msg = f"compact-ledger session-start: {sanitize_unicode(str(exc))}\n"
        return HookResult(exit_code=1 if strict else 0, stdout="", stderr=msg)


# ---------------------------------------------------------------------------
# Claude Code .claude/settings.json generation and safe idempotent merging
# ---------------------------------------------------------------------------


def _validate_command_prefix(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("--command must be a non-empty command string")
    if "\x00" in command or "\n" in command or "\r" in command:
        raise ValueError("--command must be a single-line command without control characters")
    trimmed = command.strip()
    if any(ch.isspace() for ch in trimmed):
        # If the user passed an unquoted filesystem path with spaces, shell-quote it safely
        # while leaving multi-token commands like "uvx compact-ledger" intact.
        with contextlib.suppress(OSError):
            if Path(trimmed).expanduser().is_file():
                return shlex.quote(trimmed)
    return trimmed


def _hook_command_for(command_prefix: str, hook_subcommand: str) -> str:
    return f"{command_prefix} hook {hook_subcommand}"


def _parse_tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _is_compact_ledger_prefix_tokens(
    prefix_tokens: list[str],
    expected_prefix_tokens: list[str],
) -> bool:
    if not prefix_tokens:
        return False
    if prefix_tokens == expected_prefix_tokens:
        return True
    for tok in prefix_tokens:
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if (
            base
            in (
                "compact-ledger",
                "compact-ledger.exe",
                "compact_ledger",
            )
            or "compact-ledger" in base
        ):
            return True
    return False


def _match_and_rewrite_compact_ledger_hook(
    hook_item: Any,
    hook_subcommand: str,
    *,
    command_prefix: str,
    expected_prefix_tokens: list[str],
) -> tuple[bool, str | None]:
    """Check if ``hook_item`` is a compact-ledger hook for ``hook_subcommand``.

    Returns ``(is_match, updated_command)``:
    - If not a compact-ledger hook, returns ``(False, None)``.
    - If the prefix already matches, or the prefix contains shell operators
      (e.g. ``cd /repo && ...``) or env assignments (e.g. ``COMPACT_LEDGER_DIR=...``),
      returns the original command string **byte-for-byte untouched**.
    - If the prefix is a plain compact-ledger prefix that differs from
      ``command_prefix``, splices ONLY the prefix in the raw command string so
      any trailing flags, redirections, pipes, ``|| true``, or ``"$VAR"``
      expansions are preserved verbatim.
    """
    if not isinstance(hook_item, dict):
        return False, None
    if hook_item.get("type") != "command":
        return False, None
    cmd = hook_item.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return False, None

    raw_pattern = re.compile(
        rf"^(?P<prefix>.*?)(?P<hook>\bhook\s+{re.escape(hook_subcommand)})(?P<suffix>(?:\s+.*)?)$"
    )
    match = raw_pattern.match(cmd.strip())
    if match is None:
        return False, None

    raw_prefix = match.group("prefix").strip()
    raw_suffix = match.group("suffix")
    prefix_tokens = _parse_tokens(raw_prefix)
    if not _is_compact_ledger_prefix_tokens(prefix_tokens, expected_prefix_tokens):
        return False, None

    # Preserve byte-for-byte when prefix already matches or uses shell wrappers/env assignments.
    if (
        prefix_tokens == expected_prefix_tokens
        or _SHELL_META_RE.search(raw_prefix)
        or any(_ENV_ASSIGN_PREFIX_RE.match(tok) for tok in prefix_tokens)
    ):
        return True, cmd

    return True, f"{command_prefix} hook {hook_subcommand}{raw_suffix}"


def _matcher_covers_event(event_name: str, matcher_value: Any) -> bool:
    """Return True if ``matcher_value`` fires for compact-ledger's target lifecycle events."""
    if event_name != "SessionStart":
        return True
    if matcher_value is None:
        return True
    if not isinstance(matcher_value, str):
        return False
    stripped = matcher_value.strip()
    if stripped in ("", "*", ".*"):
        return True
    return bool(_COMPACTION_MATCHER_RE.search(stripped))


def build_claude_code_hooks_config(
    *,
    command: str = DEFAULT_CLAUDE_COMMAND,
) -> dict[str, Any]:
    """Build a fresh Claude Code ``.claude/settings.json`` hooks configuration."""
    prefix = _validate_command_prefix(command)
    return {
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command_for(prefix, "pre-compact"),
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": SESSION_START_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command_for(prefix, "session-start"),
                        }
                    ],
                }
            ],
        }
    }


def _upsert_event_hook(
    hooks_dict: dict[str, Any],
    *,
    event_name: str,
    matcher: str,
    hook_subcommand: str,
    command_prefix: str,
) -> None:
    """Idempotently insert or update compact-ledger's hook entry for ``event_name``.

    - Never modifies unrelated hooks or their matchers.
    - Preserves hand-edited shell syntax (``|| true``, redirections, ``$VAR``
      expansions, env prefixes, ``cd ... &&``), CLI flags, and custom matchers.
    - Deduplicates only within a single entry or across entries sharing the same
      matcher, so separate ``compact`` and ``resume`` entries are both preserved.
    - For ``SessionStart``, leaves non-compaction custom entries (such as a
      ``startup`` matcher) untouched and ensures a compaction-covering entry exists.
    """
    existing_entries = hooks_dict.get(event_name)
    if existing_entries is None:
        event_list: list[Any] = []
    elif isinstance(existing_entries, list):
        event_list = list(existing_entries)
    else:
        raise ValueError(
            f"Existing settings.hooks.{event_name} must be a list, "
            f"got {type(existing_entries).__name__}"
        )

    base_command = _hook_command_for(command_prefix, hook_subcommand)
    expected_prefix_tokens = _parse_tokens(command_prefix)
    found_any = False
    seen_matchers: set[str] = set()
    updated_entries: list[Any] = []

    for entry in event_list:
        if not (isinstance(entry, dict) and isinstance(entry.get("hooks"), list)):
            updated_entries.append(entry)
            continue

        # Leave SessionStart entries with non-compaction matchers (e.g. "startup") untouched.
        if not _matcher_covers_event(event_name, entry.get("matcher")):
            updated_entries.append(entry)
            continue

        raw_matcher = entry.get("matcher")
        matcher_key = raw_matcher.strip() if isinstance(raw_matcher, str) else ""
        entry_hooks = list(entry["hooks"])
        new_entry_hooks: list[Any] = []
        entry_had_match = False

        for h in entry_hooks:
            is_match, rewritten_cmd = _match_and_rewrite_compact_ledger_hook(
                h,
                hook_subcommand,
                command_prefix=command_prefix,
                expected_prefix_tokens=expected_prefix_tokens,
            )
            if is_match and rewritten_cmd is not None:
                entry_had_match = True
                found_any = True
                if matcher_key not in seen_matchers:
                    seen_matchers.add(matcher_key)
                    updated_h = dict(h)
                    updated_h["type"] = "command"
                    updated_h["command"] = rewritten_cmd
                    new_entry_hooks.append(updated_h)
                # Drop duplicate compact-ledger hooks within the same matcher.
            else:
                new_entry_hooks.append(h)

        if entry_had_match and not new_entry_hooks:
            # Entry only held duplicate compact-ledger hook(s); drop empty wrapper.
            continue

        entry_copy = dict(entry)
        entry_copy["hooks"] = new_entry_hooks
        updated_entries.append(entry_copy)

    if not found_any:
        updated_entries.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": base_command}],
            }
        )

    hooks_dict[event_name] = updated_entries


def merge_claude_code_settings(
    existing: Mapping[str, Any] | None,
    *,
    command: str = DEFAULT_CLAUDE_COMMAND,
) -> dict[str, Any]:
    """Merge compact-ledger hooks into an existing Claude Code settings dict idempotently."""
    prefix = _validate_command_prefix(command)
    if existing is None:
        return build_claude_code_hooks_config(command=prefix)
    if not isinstance(existing, Mapping):
        raise ValueError(f"Existing settings JSON must be an object, got {type(existing).__name__}")

    merged: dict[str, Any] = dict(existing)
    raw_hooks = merged.get("hooks")
    if raw_hooks is None:
        hooks_dict: dict[str, Any] = {}
    elif isinstance(raw_hooks, dict):
        hooks_dict = dict(raw_hooks)
    else:
        raise ValueError(
            f"Existing settings.hooks must be an object, got {type(raw_hooks).__name__}"
        )

    _upsert_event_hook(
        hooks_dict,
        event_name="PreCompact",
        matcher="",
        hook_subcommand="pre-compact",
        command_prefix=prefix,
    )
    _upsert_event_hook(
        hooks_dict,
        event_name="SessionStart",
        matcher=SESSION_START_MATCHER,
        hook_subcommand="session-start",
        command_prefix=prefix,
    )

    merged["hooks"] = hooks_dict
    return merged


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"Non-standard JSON constant {token!r} is not allowed")


def _atomic_write_text(target: Path, content: str, *, mode: int | None = None) -> None:
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked settings file: {target}")
    if target.parent.is_symlink():
        raise ValueError(f"Refusing to use symlinked settings directory: {target.parent}")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        # Use os.open with mode 0o666 so new files respect the process umask atomically.
        fd = -1
        for _ in range(32):
            candidate = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o666 if mode is None else mode,
                )
                tmp_path = candidate
                break
            except FileExistsError:
                continue
        if fd < 0 or tmp_path is None:
            raise OSError(f"Failed to allocate temporary file in {target.parent}")

        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        tmp_path = None
        if hasattr(os, "O_RDONLY"):
            with contextlib.suppress(OSError):
                dir_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def init_claude_code_settings(
    *,
    settings_path: str | Path | None = None,
    write: bool = False,
    command: str = DEFAULT_CLAUDE_COMMAND,
    cwd: Path | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    """Build or merge Claude Code settings and optionally write them atomically.

    Returns ``(merged_config, resolved_settings_path, wrote_file)``.
    Raises ``ValueError`` if ``write=True`` on a symlink, or if an existing
    settings file contains invalid JSON or a non-object top-level structure
    (never corrupting the existing file).
    """
    prefix = _validate_command_prefix(command)
    if settings_path is not None:
        target = Path(settings_path).expanduser()
    else:
        base = cwd if cwd is not None else Path.cwd()
        target = base / DEFAULT_CLAUDE_SETTINGS_RELPATH

    if write:
        if target.is_symlink():
            raise ValueError(f"Refusing to operate on symlinked settings file: {target}")
        if target.parent.is_symlink():
            raise ValueError(
                f"Refusing to operate on symlinked settings directory: {target.parent}"
            )
    elif target.is_symlink() and not target.exists():
        raise ValueError(f"Settings path is a broken symlink: {target}")

    existing_data: dict[str, Any] | None = None
    existing_mode: int | None = None
    if target.exists():
        if not target.is_file():
            raise ValueError(f"Settings path exists but is not a regular file: {target}")
        with contextlib.suppress(OSError):
            existing_mode = stat.S_IMODE(target.stat().st_mode)
        try:
            raw_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Failed to read existing settings file {target}: {exc}") from exc

        if not raw_text.strip():
            raise ValueError(
                f"Existing settings file {target} is empty (invalid JSON); refusing to overwrite."
            )
        try:
            parsed = json.loads(raw_text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ValueError(
                f"Existing settings file {target} contains invalid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Existing settings file {target} must contain a JSON object at top level, "
                f"got {type(parsed).__name__}."
            )
        existing_data = parsed

    merged = merge_claude_code_settings(existing_data, command=prefix)
    try:
        serialized = json.dumps(merged, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Merged settings contain non-JSON values: {exc}") from exc

    if write:
        _atomic_write_text(target, serialized, mode=existing_mode)
        return merged, target, True

    return merged, target, False
