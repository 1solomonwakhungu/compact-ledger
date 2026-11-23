"""Transcript readers for Claude Code, Codex, and generic JSONL transcripts."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from compact_ledger.models import (
    VALID_FILE_OPERATIONS,
    FileEdit,
    FileOperation,
    ParsedTranscript,
    ToolExecution,
    UserTurn,
)

SUPPORTED_FORMATS: frozenset[str] = frozenset({"auto", "claude-code", "codex", "generic"})

_CODEX_WRAPPER_TYPES = frozenset(
    {
        "session_meta",
        "response_item",
        "event_msg",
        "turn_context",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "local_shell_call_output",
    }
)
_CLAUDE_ENTRY_TYPES = frozenset(
    {"user", "assistant", "system", "summary", "progress", "file-history-snapshot"}
)
_SHELL_TOOL_NAMES = frozenset(
    {"bash", "shell", "shell_command", "exec_command", "local_shell", "container.exec"}
)

_EXIT_CODE_PATTERNS = (
    re.compile(r"(?:^|\n)\s*Process exited with code\s+(-?\d+)\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*Exit code[:\s]+(-?\d+)\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*Command exited with (?:non-zero )?status\s+(-?\d+)\b", re.IGNORECASE),
    re.compile(r"\bexited with (?:code|status)\s+(-?\d+)\b", re.IGNORECASE),
)
_CODEX_OUTPUT_MARKER_RE = re.compile(r"(?:^|\n)Output:\n", re.MULTILINE)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        is_neg_digits = stripped.startswith("-") and stripped[1:].isdigit()
        if stripped and (stripped.isdigit() or is_neg_digits):
            return int(stripped)
    return None


def _add_modified_file(modified_files: list[str], seen: set[str], path: str | None) -> None:
    if not path:
        return
    cleaned = path.strip()
    if cleaned and cleaned not in seen:
        seen.add(cleaned)
        modified_files.append(cleaned)


def _extract_text_from_content(content: Any) -> str:
    """Extract joined text from a string or list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("text", "input_text", "output_text", None):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(parts)
    return ""


def _extract_exit_code_from_text(text: str) -> int | None:
    if not text:
        return None
    for pattern in _EXIT_CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _format_command_arg(cmd: Any) -> str | None:
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and all(isinstance(part, str) for part in cmd) and cmd:
        if (
            len(cmd) == 3
            and cmd[0] in ("bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh")
            and cmd[1] in ("-c", "-lc")
        ):
            return cmd[2]
        if (
            len(cmd) == 4
            and cmd[0] in ("env", "/usr/bin/env")
            and cmd[1] in ("bash", "sh", "zsh")
            and cmd[2] in ("-c", "-lc")
        ):
            return cmd[3]
        return shlex.join(cmd)
    return None


def _parse_apply_patch(
    patch_text: str,
    *,
    turn_index: int,
    timestamp: str | None,
) -> tuple[list[FileEdit], list[str]]:
    """Parse Codex ``apply_patch`` format into ``FileEdit`` records and modified paths."""
    edits: list[FileEdit] = []
    paths: list[str] = []
    seen: set[str] = set()

    current_path: str | None = None
    current_op: FileOperation | None = None
    old_lines: list[str] = []
    new_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_path, current_op, old_lines, new_lines
        if current_path is not None and current_op is not None:
            old_text = "\n".join(old_lines) if old_lines else None
            new_text = "\n".join(new_lines) if new_lines else None
            edits.append(
                FileEdit(
                    turn_index=turn_index,
                    path=current_path,
                    operation=current_op,
                    old_text=old_text,
                    new_text=new_text,
                    timestamp=timestamp,
                )
            )
            _add_modified_file(paths, seen, current_path)
        current_path = None
        current_op = None
        old_lines = []
        new_lines = []

    for line in patch_text.splitlines():
        if line.startswith("*** Update File:"):
            _flush()
            current_path = line.split(":", 1)[1].strip()
            current_op = "edit"
        elif line.startswith("*** Add File:"):
            _flush()
            current_path = line.split(":", 1)[1].strip()
            current_op = "write"
        elif line.startswith("*** Delete File:"):
            _flush()
            current_path = line.split(":", 1)[1].strip()
            current_op = "delete"
        elif line.startswith("*** Move to:"):
            if current_path is not None:
                _add_modified_file(paths, seen, current_path)
                moved = line.split(":", 1)[1].strip()
                if moved:
                    current_path = moved
        elif line.startswith("*** End Patch") or line.startswith("*** End of File"):
            _flush()
        elif current_op is not None:
            if line.startswith("+") and not line.startswith("+++"):
                new_lines.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                old_lines.append(line[1:])

    _flush()
    return edits, paths


def _iter_json_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def detect_transcript_format(records: Sequence[dict[str, Any]]) -> str:
    """Detect whether parsed JSONL records are Claude Code, Codex, or generic."""
    if not records:
        return "unknown"

    codex_score = 0
    claude_score = 0
    generic_score = 0

    for record in records[:100]:
        rec_type = record.get("type")
        if isinstance(rec_type, str) and rec_type in _CODEX_WRAPPER_TYPES:
            codex_score += 3
            continue

        if (
            isinstance(rec_type, str)
            and rec_type in _CLAUDE_ENTRY_TYPES
            and (
                isinstance(record.get("message"), dict)
                or "sessionId" in record
                or "parentUuid" in record
                or "toolUseResult" in record
                or rec_type in ("summary", "file-history-snapshot")
            )
        ):
            claude_score += 3
            continue

        if isinstance(record.get("message"), dict):
            content = record["message"].get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                for b in content
            ):
                claude_score += 3
                continue

        if (
            "role" in record
            or "command" in record
            or rec_type in ("file_edit", "tool", "tool_execution", "command")
        ):
            generic_score += 1

    if codex_score > 0 and codex_score >= claude_score:
        return "codex"
    if claude_score > 0:
        return "claude-code"
    if generic_score > 0:
        return "generic"
    return "generic"


def _extract_claude_file_edits(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    turn_index: int,
    timestamp: str | None,
) -> list[FileEdit]:
    edits: list[FileEdit] = []
    if tool_name == "Edit":
        path = _as_str(tool_input.get("file_path")) or _as_str(tool_input.get("path"))
        if path:
            edits.append(
                FileEdit(
                    turn_index=turn_index,
                    path=path,
                    operation="edit",
                    old_text=_as_str(tool_input.get("old_string")),
                    new_text=_as_str(tool_input.get("new_string")),
                    timestamp=timestamp,
                )
            )
    elif tool_name == "Write":
        path = _as_str(tool_input.get("file_path")) or _as_str(tool_input.get("path"))
        if path:
            edits.append(
                FileEdit(
                    turn_index=turn_index,
                    path=path,
                    operation="write",
                    old_text=None,
                    new_text=_as_str(tool_input.get("content")),
                    timestamp=timestamp,
                )
            )
    elif tool_name == "MultiEdit":
        path = _as_str(tool_input.get("file_path")) or _as_str(tool_input.get("path"))
        raw_edits = tool_input.get("edits")
        if path and isinstance(raw_edits, list) and raw_edits:
            for item in raw_edits:
                if isinstance(item, dict):
                    edits.append(
                        FileEdit(
                            turn_index=turn_index,
                            path=path,
                            operation="edit",
                            old_text=_as_str(item.get("old_string")),
                            new_text=_as_str(item.get("new_string")),
                            timestamp=timestamp,
                        )
                    )
        elif path:
            edits.append(
                FileEdit(
                    turn_index=turn_index,
                    path=path,
                    operation="edit",
                    timestamp=timestamp,
                )
            )
    elif tool_name == "NotebookEdit":
        path = (
            _as_str(tool_input.get("notebook_path"))
            or _as_str(tool_input.get("file_path"))
            or _as_str(tool_input.get("path"))
        )
        if path:
            edit_mode = _as_str(tool_input.get("edit_mode")) or "replace"
            op: FileOperation
            if edit_mode == "delete":
                op = "delete"
            elif edit_mode == "insert":
                op = "write"
            else:
                op = "edit"
            edits.append(
                FileEdit(
                    turn_index=turn_index,
                    path=path,
                    operation=op,
                    old_text=None,
                    new_text=_as_str(tool_input.get("new_source")),
                    timestamp=timestamp,
                )
            )
    return edits


def parse_claude_code_records(
    records: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> ParsedTranscript:
    """Parse Claude Code JSONL transcript records."""
    detected_session_id = session_id
    user_turns: list[UserTurn] = []
    tool_executions: list[ToolExecution] = []
    file_edits: list[FileEdit] = []
    modified_files: list[str] = []
    seen_files: set[str] = set()

    pending_tools: dict[str, ToolExecution] = {}
    turn_index = 0

    for record in records:
        if detected_session_id is None:
            sid = _as_str(record.get("sessionId")) or _as_str(record.get("session_id"))
            if sid:
                detected_session_id = sid

        rec_type = _as_str(record.get("type"))
        timestamp = _as_str(record.get("timestamp"))
        message = record.get("message")
        msg_dict = message if isinstance(message, dict) else {}
        content = msg_dict.get("content")
        tool_use_result = record.get("toolUseResult")
        tur_dict = tool_use_result if isinstance(tool_use_result, dict) else {}

        if rec_type == "user":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    call_id = _as_str(block.get("tool_use_id"))
                    output_text = _extract_text_from_content(block.get("content"))
                    if not output_text and tur_dict:
                        stdout = _as_str(tur_dict.get("stdout")) or ""
                        stderr = _as_str(tur_dict.get("stderr")) or ""
                        output_text = "\n".join(part for part in (stdout, stderr) if part)
                    elif not output_text and isinstance(tool_use_result, str):
                        output_text = tool_use_result

                    exit_code = (
                        _as_int(block.get("exit_code"))
                        if block.get("exit_code") is not None
                        else _as_int(block.get("exitCode"))
                    )
                    if exit_code is None and tur_dict:
                        for key in ("exit_code", "exitCode", "returnCode", "returncode"):
                            exit_code = _as_int(tur_dict.get(key))
                            if exit_code is not None:
                                break
                    if exit_code is None:
                        exit_code = _extract_exit_code_from_text(output_text)

                    is_error = bool(block.get("is_error") is True)
                    if exit_code is not None and exit_code != 0:
                        is_error = True

                    if call_id and call_id in pending_tools:
                        execution = pending_tools[call_id]
                        execution.output = output_text
                        execution.exit_code = exit_code
                        execution.is_error = is_error
                        if execution.timestamp is None and timestamp is not None:
                            execution.timestamp = timestamp
                    else:
                        execution = ToolExecution(
                            turn_index=turn_index,
                            tool_name="unknown",
                            command=None,
                            output=output_text,
                            exit_code=exit_code,
                            is_error=is_error,
                            timestamp=timestamp,
                            call_id=call_id,
                        )
                        tool_executions.append(execution)

            if record.get("isMeta") is True or record.get("isCompactSummary") is True:
                continue

            if isinstance(content, str):
                user_text = content.strip()
            elif isinstance(content, list):
                text_parts = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ]
                user_text = "\n".join(text_parts).strip()
            else:
                user_text = ""

            if user_text:
                turn_index += 1
                user_turns.append(
                    UserTurn(
                        turn_index=turn_index,
                        text=user_text,
                        timestamp=timestamp,
                        source="claude-code",
                    )
                )

        elif rec_type == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_name = _as_str(block.get("name")) or "unknown"
                call_id = _as_str(block.get("id"))
                raw_input = block.get("input")
                tool_input = raw_input if isinstance(raw_input, dict) else {}

                command: str | None = None
                if tool_name.lower() in _SHELL_TOOL_NAMES:
                    command = _format_command_arg(
                        tool_input.get("command") or tool_input.get("cmd")
                    )

                execution = ToolExecution(
                    turn_index=turn_index,
                    tool_name=tool_name,
                    command=command,
                    output="",
                    exit_code=None,
                    is_error=False,
                    timestamp=timestamp,
                    call_id=call_id,
                )
                tool_executions.append(execution)
                if call_id:
                    pending_tools[call_id] = execution

                extracted_edits = _extract_claude_file_edits(
                    tool_name,
                    tool_input,
                    turn_index=turn_index,
                    timestamp=timestamp,
                )
                for edit in extracted_edits:
                    file_edits.append(edit)
                    _add_modified_file(modified_files, seen_files, edit.path)

    return ParsedTranscript(
        format="claude-code",
        session_id=detected_session_id,
        user_turns=user_turns,
        tool_executions=tool_executions,
        file_edits=file_edits,
        modified_files=modified_files,
    )


def _unwrap_codex_output(raw_output: Any) -> tuple[str, int | None]:
    """Extract output text and optional exit code from a Codex tool output payload."""
    if isinstance(raw_output, dict):
        output = _as_str(raw_output.get("output")) or ""
        meta = raw_output.get("metadata")
        exit_code = _as_int(meta.get("exit_code")) if isinstance(meta, dict) else None
        if exit_code is None:
            exit_code = _as_int(raw_output.get("exit_code"))
        if exit_code is None:
            exit_code = _extract_exit_code_from_text(output)
        return output, exit_code

    if not isinstance(raw_output, str):
        return "", None

    stripped = raw_output.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and ("output" in decoded or "metadata" in decoded):
            return _unwrap_codex_output(decoded)

    exit_code = _extract_exit_code_from_text(raw_output)
    marker = _CODEX_OUTPUT_MARKER_RE.search(raw_output)
    if marker:
        return raw_output[marker.end() :], exit_code
    return raw_output, exit_code


def parse_codex_records(
    records: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> ParsedTranscript:
    """Parse Codex CLI / Responses API JSONL transcript records."""
    detected_session_id = session_id
    user_turns: list[UserTurn] = []
    tool_executions: list[ToolExecution] = []
    file_edits: list[FileEdit] = []
    modified_files: list[str] = []
    seen_files: set[str] = set()

    pending_tools: dict[str, ToolExecution] = {}
    turn_index = 0
    last_user_origin: str | None = None
    activity_since_last_user = True

    def _record_user_turn(text: str, timestamp: str | None, origin: str) -> None:
        nonlocal turn_index, last_user_origin, activity_since_last_user
        cleaned = text.strip()
        if not cleaned:
            return
        # Codex frequently logs both response_item(message/user) and event_msg(user_message)
        # for the exact same user turn; deduplicate consecutive cross-origin duplicates.
        if (
            user_turns
            and not activity_since_last_user
            and last_user_origin is not None
            and last_user_origin != origin
            and user_turns[-1].text == cleaned
        ):
            return
        turn_index += 1
        user_turns.append(
            UserTurn(
                turn_index=turn_index,
                text=cleaned,
                timestamp=timestamp,
                source="codex",
            )
        )
        last_user_origin = origin
        activity_since_last_user = False

    for record in records:
        rec_type = _as_str(record.get("type"))
        timestamp = _as_str(record.get("timestamp"))
        payload = record.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}

        if rec_type == "session_meta":
            if detected_session_id is None:
                sid = _as_str(payload_dict.get("id")) or _as_str(record.get("id"))
                if sid:
                    detected_session_id = sid
            continue

        if rec_type == "event_msg":
            event_type = _as_str(payload_dict.get("type"))
            if event_type == "user_message":
                msg_text = _as_str(payload_dict.get("message")) or _as_str(payload_dict.get("text"))
                if msg_text:
                    _record_user_turn(msg_text, timestamp, origin="event_msg")
            elif event_type in ("agent_message", "exec_command_end", "patch_apply_end"):
                activity_since_last_user = True
            continue

        item = payload_dict if rec_type == "response_item" else record

        item_type = _as_str(item.get("type"))

        if item_type == "message":
            role = _as_str(item.get("role"))
            if role == "user":
                text = _extract_text_from_content(item.get("content"))
                _record_user_turn(text, timestamp, origin="response_item")
            elif role == "assistant":
                activity_since_last_user = True

        elif item_type == "function_call":
            activity_since_last_user = True
            tool_name = _as_str(item.get("name")) or "unknown"
            call_id = _as_str(item.get("call_id"))
            raw_args = item.get("arguments")
            args: dict[str, Any] = {}
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    decoded_args = json.loads(raw_args)
                    if isinstance(decoded_args, dict):
                        args = decoded_args
                except json.JSONDecodeError:
                    args = {}

            command: str | None = None
            if tool_name.lower() in _SHELL_TOOL_NAMES:
                command = _format_command_arg(args.get("command") or args.get("cmd"))

            execution = ToolExecution(
                turn_index=turn_index,
                tool_name=tool_name,
                command=command,
                output="",
                exit_code=None,
                is_error=False,
                timestamp=timestamp,
                call_id=call_id,
            )
            tool_executions.append(execution)
            if call_id:
                pending_tools[call_id] = execution

            if tool_name == "apply_patch":
                patch_text = (
                    _as_str(args.get("input"))
                    or _as_str(args.get("patch"))
                    or (_as_str(raw_args) if not args else None)
                )
                if patch_text:
                    patch_edits, patch_paths = _parse_apply_patch(
                        patch_text,
                        turn_index=turn_index,
                        timestamp=timestamp,
                    )
                    file_edits.extend(patch_edits)
                    for path in patch_paths:
                        _add_modified_file(modified_files, seen_files, path)

        elif item_type == "custom_tool_call":
            activity_since_last_user = True
            tool_name = _as_str(item.get("name")) or "unknown"
            call_id = _as_str(item.get("call_id"))
            raw_input = item.get("input")

            command = None
            if tool_name.lower() in _SHELL_TOOL_NAMES:
                if isinstance(raw_input, dict):
                    command = _format_command_arg(raw_input.get("command") or raw_input.get("cmd"))
                elif isinstance(raw_input, str):
                    command = raw_input

            execution = ToolExecution(
                turn_index=turn_index,
                tool_name=tool_name,
                command=command,
                output="",
                exit_code=None,
                is_error=False,
                timestamp=timestamp,
                call_id=call_id,
            )
            tool_executions.append(execution)
            if call_id:
                pending_tools[call_id] = execution

            if tool_name == "apply_patch" and isinstance(raw_input, str):
                patch_edits, patch_paths = _parse_apply_patch(
                    raw_input,
                    turn_index=turn_index,
                    timestamp=timestamp,
                )
                file_edits.extend(patch_edits)
                for path in patch_paths:
                    _add_modified_file(modified_files, seen_files, path)

        elif item_type == "local_shell_call":
            activity_since_last_user = True
            call_id = _as_str(item.get("call_id"))
            action = item.get("action")
            action_dict = action if isinstance(action, dict) else {}
            command = _format_command_arg(action_dict.get("command") or item.get("command"))
            execution = ToolExecution(
                turn_index=turn_index,
                tool_name="local_shell",
                command=command,
                output="",
                exit_code=None,
                is_error=False,
                timestamp=timestamp,
                call_id=call_id,
            )
            tool_executions.append(execution)
            if call_id:
                pending_tools[call_id] = execution

        elif item_type in (
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
        ):
            activity_since_last_user = True
            call_id = _as_str(item.get("call_id"))
            output_text, exit_code = _unwrap_codex_output(item.get("output"))
            is_error = bool(item.get("is_error") is True)
            if exit_code is not None and exit_code != 0:
                is_error = True

            if call_id and call_id in pending_tools:
                execution = pending_tools[call_id]
                execution.output = output_text
                execution.exit_code = exit_code
                execution.is_error = is_error
                if execution.timestamp is None and timestamp is not None:
                    execution.timestamp = timestamp
            else:
                tool_executions.append(
                    ToolExecution(
                        turn_index=turn_index,
                        tool_name="unknown",
                        command=None,
                        output=output_text,
                        exit_code=exit_code,
                        is_error=is_error,
                        timestamp=timestamp,
                        call_id=call_id,
                    )
                )

    return ParsedTranscript(
        format="codex",
        session_id=detected_session_id,
        user_turns=user_turns,
        tool_executions=tool_executions,
        file_edits=file_edits,
        modified_files=modified_files,
    )


def parse_generic_records(
    records: Sequence[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> ParsedTranscript:
    """Parse generic flat JSONL transcripts."""
    detected_session_id = session_id
    user_turns: list[UserTurn] = []
    tool_executions: list[ToolExecution] = []
    file_edits: list[FileEdit] = []
    modified_files: list[str] = []
    seen_files: set[str] = set()

    turn_index = 0

    for record in records:
        if detected_session_id is None:
            sid = _as_str(record.get("session_id")) or _as_str(record.get("sessionId"))
            if sid:
                detected_session_id = sid

        timestamp = _as_str(record.get("timestamp"))
        rec_type = _as_str(record.get("type"))
        role = _as_str(record.get("role"))

        if rec_type == "file_edit":
            path = _as_str(record.get("path")) or _as_str(record.get("file_path"))
            if not path:
                continue
            raw_op = _as_str(record.get("operation")) or "edit"
            op: FileOperation = raw_op if raw_op in VALID_FILE_OPERATIONS else "edit"  # type: ignore[assignment]
            edit = FileEdit(
                turn_index=turn_index,
                path=path,
                operation=op,
                old_text=_as_str(record.get("old_text")) or _as_str(record.get("old_string")),
                new_text=_as_str(record.get("new_text")) or _as_str(record.get("new_string")),
                timestamp=timestamp,
            )
            file_edits.append(edit)
            _add_modified_file(modified_files, seen_files, path)
            continue

        if role == "user" or (rec_type == "user" and role is None):
            raw_content = record.get("content")
            if raw_content is None:
                raw_content = record.get("text")
            text = _extract_text_from_content(raw_content).strip()
            if text:
                turn_index += 1
                user_turns.append(
                    UserTurn(
                        turn_index=turn_index,
                        text=text,
                        timestamp=timestamp,
                        source="generic",
                    )
                )
            continue

        is_tool_record = (
            role == "tool"
            or rec_type in ("tool", "tool_execution", "command", "bash", "shell")
            or "command" in record
        )
        if is_tool_record:
            tool_name = (
                _as_str(record.get("tool_name"))
                or _as_str(record.get("name"))
                or (rec_type if rec_type in ("bash", "shell") else "shell")
            )
            command = _format_command_arg(record.get("command") or record.get("cmd"))
            output_val = record.get("output")
            if output_val is not None:
                output_text = _extract_text_from_content(output_val)
            else:
                stdout = _as_str(record.get("stdout")) or ""
                stderr = _as_str(record.get("stderr")) or ""
                if stdout or stderr:
                    output_text = "\n".join(part for part in (stdout, stderr) if part)
                else:
                    output_text = _extract_text_from_content(record.get("content"))

            exit_code = _as_int(record.get("exit_code"))
            if exit_code is None:
                exit_code = _as_int(record.get("exitCode"))
            if exit_code is None:
                exit_code = _extract_exit_code_from_text(output_text)

            is_error = bool(record.get("is_error") is True)
            if exit_code is not None and exit_code != 0:
                is_error = True

            tool_executions.append(
                ToolExecution(
                    turn_index=turn_index,
                    tool_name=tool_name,
                    command=command,
                    output=output_text,
                    exit_code=exit_code,
                    is_error=is_error,
                    timestamp=timestamp,
                    call_id=_as_str(record.get("call_id")) or _as_str(record.get("id")),
                )
            )

    return ParsedTranscript(
        format="generic",
        session_id=detected_session_id,
        user_turns=user_turns,
        tool_executions=tool_executions,
        file_edits=file_edits,
        modified_files=modified_files,
    )


def parse_transcript_lines(
    lines: Iterable[str],
    *,
    fmt: str = "auto",
    session_id: str | None = None,
) -> ParsedTranscript:
    """Parse transcript JSONL lines using auto-detection or an explicit format."""
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported transcript format {fmt!r}; expected one of {sorted(SUPPORTED_FORMATS)}"
        )

    records = _iter_json_records(lines)
    if not records:
        return ParsedTranscript(
            format="unknown" if fmt == "auto" else fmt,
            session_id=session_id,
        )

    resolved_fmt = detect_transcript_format(records) if fmt == "auto" else fmt
    if resolved_fmt == "claude-code":
        return parse_claude_code_records(records, session_id=session_id)
    if resolved_fmt == "codex":
        return parse_codex_records(records, session_id=session_id)
    return parse_generic_records(records, session_id=session_id)


def parse_transcript(
    path: str | Path,
    *,
    fmt: str = "auto",
    session_id: str | None = None,
) -> ParsedTranscript:
    """Read and parse a JSONL transcript file from disk."""
    transcript_path = Path(path)
    with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
        return parse_transcript_lines(handle, fmt=fmt, session_id=session_id)
