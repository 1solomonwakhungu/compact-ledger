"""Claude Code hook handlers, end-to-end snapshot pipeline, and settings init.

All hook handlers are deterministic, zero-LLM, stdlib-only, and strictly fail-open
(exit 0 without polluting stdout on malformed input or unexpected internal errors)
unless ``strict=True`` is explicitly requested.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from compact_ledger.ledger import build_attempt_ledger
from compact_ledger.models import CompactionSnapshot, ParsedTranscript, PinEntry, sanitize_unicode
from compact_ledger.parsers.transcript import parse_transcript
from compact_ledger.pins import extract_pins
from compact_ledger.renderer import DEFAULT_MAX_CHARS, render_snapshot
from compact_ledger.store import ENV_STORE_DIR, CorruptSnapshotError, SessionStore

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
_MAX_STDIN_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap for hook stdin payloads


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


def _parse_hook_stdin(raw_input: str | bytes | TextIO | BinaryIO) -> dict[str, Any]:
    """Parse a Claude Code hook JSON object payload from raw bytes, text, or a stream."""
    if isinstance(raw_input, bytes):
        if len(raw_input) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        try:
            text = raw_input.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Hook stdin is not valid UTF-8: {exc}") from exc
    elif isinstance(raw_input, str):
        if len(raw_input.encode("utf-8", errors="surrogatepass")) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        text = raw_input
    else:
        raw_bytes = _read_stream_bytes(raw_input)
        if len(raw_bytes) > _MAX_STDIN_BYTES:
            raise ValueError(f"Hook stdin payload exceeds {_MAX_STDIN_BYTES} bytes")
        try:
            text = raw_bytes.decode("utf-8")
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
    2. ``COMPACT_LEDGER_DIR`` environment variable
    3. Payload ``cwd`` (when it points to an existing directory)
    4. ``fallback_cwd`` or current working directory (``Path.cwd()``)
    """
    if store_dir is not None:
        return SessionStore(store_dir=store_dir)

    env_map = os.environ if env is None else env
    env_dir = env_map.get(ENV_STORE_DIR)
    if env_dir:
        return SessionStore(store_dir=env_dir)

    valid_cwd = _is_valid_existing_dir(payload_cwd)
    if valid_cwd is not None:
        return SessionStore(base_dir=valid_cwd)

    return SessionStore(base_dir=fallback_cwd if fallback_cwd is not None else Path.cwd())


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
    raw_input: str | bytes | TextIO | BinaryIO,
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
    raw_input: str | bytes | TextIO | BinaryIO,
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
    return command.strip()


def _hook_command_for(command_prefix: str, hook_subcommand: str) -> str:
    return f"{command_prefix} hook {hook_subcommand}"


def _is_compact_ledger_hook_entry(
    hook_item: Any,
    hook_subcommand: str,
    *,
    expected_command: str | None = None,
) -> bool:
    """Return True if ``hook_item`` is a compact-ledger command hook for ``hook_subcommand``."""
    if not isinstance(hook_item, dict):
        return False
    if hook_item.get("type") != "command":
        return False
    cmd = hook_item.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return False

    if expected_command is not None and cmd.strip() == expected_command.strip():
        return True

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    if len(tokens) < 3:
        return False

    # Look for "hook <subcommand>" paired with compact-ledger / compact_ledger in preceding tokens.
    for idx in range(len(tokens) - 1):
        if tokens[idx] == "hook" and tokens[idx + 1] == hook_subcommand:
            prefix_tokens = tokens[:idx]
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
    full_command: str,
) -> None:
    """Idempotently insert or update compact-ledger's hook entry for ``event_name``."""
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

    new_hook_obj = {"type": "command", "command": full_command}
    found = False
    updated_entries: list[Any] = []

    for entry in event_list:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
            entry_hooks = list(entry["hooks"])
            new_entry_hooks: list[Any] = []
            entry_had_match = False
            for h in entry_hooks:
                if _is_compact_ledger_hook_entry(
                    h,
                    hook_subcommand,
                    expected_command=full_command,
                ):
                    entry_had_match = True
                    if not found:
                        updated_h = dict(h)
                        updated_h["type"] = "command"
                        updated_h["command"] = full_command
                        new_entry_hooks.append(updated_h)
                        found = True
                    # Drop any duplicate compact-ledger hooks for the same event.
                else:
                    new_entry_hooks.append(h)

            if entry_had_match and not new_entry_hooks:
                # Entry only held duplicate compact-ledger hook(s); drop empty wrapper.
                continue

            entry_copy = dict(entry)
            entry_copy["hooks"] = new_entry_hooks
            if entry_had_match and len(new_entry_hooks) == 1:
                entry_copy["matcher"] = matcher
            updated_entries.append(entry_copy)
        else:
            updated_entries.append(entry)

    if not found:
        updated_entries.append(
            {
                "matcher": matcher,
                "hooks": [new_hook_obj],
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
        full_command=_hook_command_for(prefix, "pre-compact"),
    )
    _upsert_event_hook(
        hooks_dict,
        event_name="SessionStart",
        matcher=SESSION_START_MATCHER,
        hook_subcommand="session-start",
        full_command=_hook_command_for(prefix, "session-start"),
    )

    merged["hooks"] = hooks_dict
    return merged


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
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
    Raises ``ValueError`` if the existing settings file contains invalid JSON or
    a non-object top-level structure (never corrupting the existing file).
    """
    prefix = _validate_command_prefix(command)
    if settings_path is not None:
        target = Path(settings_path).expanduser()
    else:
        base = cwd if cwd is not None else Path.cwd()
        target = base / DEFAULT_CLAUDE_SETTINGS_RELPATH

    existing_data: dict[str, Any] | None = None
    if target.exists():
        if not target.is_file():
            raise ValueError(f"Settings path exists but is not a regular file: {target}")
        try:
            raw_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Failed to read existing settings file {target}: {exc}") from exc

        if not raw_text.strip():
            raise ValueError(
                f"Existing settings file {target} is empty (invalid JSON); refusing to overwrite."
            )
        try:
            parsed = json.loads(raw_text)
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
    if write:
        serialized = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(target, serialized)
        return merged, target, True

    return merged, target, False


def _emit_hook_result(result: HookResult) -> int:
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result.exit_code
