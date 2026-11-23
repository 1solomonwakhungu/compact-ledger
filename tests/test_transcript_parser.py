"""Tests for compact_ledger.parsers.transcript."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compact_ledger.parsers import (
    detect_transcript_format,
    parse_transcript,
    parse_transcript_lines,
)


def _jsonl(records: list[object]) -> list[str]:
    return [json.dumps(rec) for rec in records]


def test_parse_claude_code_transcript_full(tmp_path: Path) -> None:
    records = [
        {"type": "summary", "summary": "Earlier summary"},
        {
            "type": "user",
            "sessionId": "claude-sess-42",
            "timestamp": "2026-01-15T10:00:00Z",
            "isMeta": True,
            "message": {"role": "user", "content": "System meta note"},
        },
        {
            "type": "user",
            "sessionId": "claude-sess-42",
            "timestamp": "2026-01-15T10:00:01Z",
            "message": {"role": "user", "content": "Never mock the database. Fix test_calc."},
        },
        {
            "type": "assistant",
            "sessionId": "claude-sess-42",
            "timestamp": "2026-01-15T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me run pytest."},
                    {
                        "type": "tool_use",
                        "id": "toolu_bash_1",
                        "name": "Bash",
                        "input": {"command": "pytest tests/test_calc.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_edit_1",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/calc.py",
                            "old_string": "return a - b",
                            "new_string": "return a + b",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_write_1",
                        "name": "Write",
                        "input": {
                            "file_path": "src/helper.py",
                            "content": "def ok():\n    return True\n",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_multi_1",
                        "name": "MultiEdit",
                        "input": {
                            "file_path": "src/calc.py",
                            "edits": [
                                {"old_string": "a + b", "new_string": "int(a + b)"},
                                {"old_string": "x = 1", "new_string": "x = 2"},
                            ],
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_nb_1",
                        "name": "NotebookEdit",
                        "input": {
                            "notebook_path": "notebooks/demo.ipynb",
                            "edit_mode": "delete",
                            "new_source": "",
                        },
                    },
                ],
            },
        },
        # Tool-result-only user entry should NOT create a UserTurn
        {
            "type": "user",
            "sessionId": "claude-sess-42",
            "timestamp": "2026-01-15T10:00:06Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_bash_1",
                        "is_error": True,
                        "content": [{"type": "text", "text": "E   assert 400 == 100\nExit code 1"}],
                    }
                ],
            },
        },
        # Mixed tool_result + genuine user text MUST process both
        {
            "type": "user",
            "sessionId": "claude-sess-42",
            "timestamp": "2026-01-15T10:01:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_edit_1",
                        "is_error": False,
                        "content": "File updated.",
                    },
                    {
                        "type": "text",
                        "text": "Actually use July 31 instead of June 30.",
                    },
                ],
            },
        },
    ]

    transcript_file = tmp_path / "claude.jsonl"
    transcript_file.write_text("\n".join(_jsonl(records)) + "\n", encoding="utf-8")

    parsed = parse_transcript(transcript_file)
    assert parsed.format == "claude-code"
    assert parsed.session_id == "claude-sess-42"

    assert len(parsed.user_turns) == 2
    assert parsed.user_turns[0].turn_index == 1
    assert parsed.user_turns[0].text == "Never mock the database. Fix test_calc."
    assert parsed.user_turns[1].turn_index == 2
    assert parsed.user_turns[1].text == "Actually use July 31 instead of June 30."

    bash_exec = next(t for t in parsed.tool_executions if t.call_id == "toolu_bash_1")
    assert bash_exec.tool_name == "Bash"
    assert bash_exec.command == "pytest tests/test_calc.py"
    assert bash_exec.exit_code == 1
    assert bash_exec.is_error is True
    assert "assert 400 == 100" in bash_exec.output
    assert bash_exec.turn_index == 1

    edit_exec = next(t for t in parsed.tool_executions if t.call_id == "toolu_edit_1")
    assert edit_exec.output == "File updated."
    assert edit_exec.is_error is False

    assert len(parsed.file_edits) == 5
    assert parsed.file_edits[0].operation == "edit"
    assert parsed.file_edits[0].old_text == "return a - b"
    assert parsed.file_edits[1].operation == "write"
    assert parsed.file_edits[2].path == "src/calc.py"
    assert parsed.file_edits[4].operation == "delete"
    assert parsed.modified_files == ["src/calc.py", "src/helper.py", "notebooks/demo.ipynb"]


def test_parse_claude_code_tool_use_result_metadata_and_orphan() -> None:
    lines = _jsonl(
        [
            {
                "type": "user",
                "timestamp": "2026-01-15T10:00:00Z",
                "message": {"role": "user", "content": "Run build"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-01-15T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {"command": "make build"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-01-15T10:00:02Z",
                "toolUseResult": {"stdout": "compiling", "stderr": "boom", "returnCode": 2},
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": ""},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_orphan",
                            "content": "orphan output",
                            "is_error": True,
                        },
                    ],
                },
            },
        ]
    )
    parsed = parse_transcript_lines(lines)
    assert parsed.format == "claude-code"
    assert len(parsed.tool_executions) == 2
    first = parsed.tool_executions[0]
    assert first.exit_code == 2
    assert first.is_error is True
    assert first.output == "compiling\nboom"
    orphan = parsed.tool_executions[1]
    assert orphan.call_id == "toolu_orphan"
    assert orphan.output == "orphan output"
    assert orphan.is_error is True


def test_parse_codex_transcript_with_dual_events_and_apply_patch() -> None:
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/app.py",
            "@@ -1,2 +1,2 @@",
            "-VALUE = 400",
            "+VALUE = 200",
            "*** Add File: src/new_mod.py",
            "+def fresh():",
            "+    return 1",
            "*** Delete File: src/legacy.py",
            "*** End Patch",
        ]
    )
    records = [
        {
            "timestamp": "2026-01-15T11:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-rollout-99", "cwd": "/repo"},
        },
        # Dual user message emission: response_item + event_msg for same turn
        {
            "timestamp": "2026-01-15T11:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix failing pytest test."}],
            },
        },
        {
            "timestamp": "2026-01-15T11:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Fix failing pytest test."},
        },
        {
            "timestamp": "2026-01-15T11:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_1",
                "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]}),
            },
        },
        {
            "timestamp": "2026-01-15T11:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": json.dumps({"output": "FAILED test_x\n", "metadata": {"exit_code": 1}}),
            },
        },
        {
            "timestamp": "2026-01-15T11:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call_2",
                "input": patch,
            },
        },
        {
            "timestamp": "2026-01-15T11:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_2",
                "output": "Success. Updated the following files:\nM src/app.py\n",
            },
        },
        {
            "timestamp": "2026-01-15T11:00:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_3",
                "arguments": json.dumps({"cmd": "pytest -q"}),
            },
        },
        {
            "timestamp": "2026-01-15T11:00:07Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_3",
                "output": (
                    "Chunk ID: abc\nWall time: 0.2s\nProcess exited with code 0\n"
                    "Output:\n1 passed in 0.1s\n"
                ),
            },
        },
        # Second genuine user turn with identical text after tool activity MUST be kept
        {
            "timestamp": "2026-01-15T11:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix failing pytest test."}],
            },
        },
    ]

    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.format == "codex"
    assert parsed.session_id == "codex-rollout-99"

    assert len(parsed.user_turns) == 2
    assert [t.turn_index for t in parsed.user_turns] == [1, 2]

    call1 = next(t for t in parsed.tool_executions if t.call_id == "call_1")
    assert call1.command == "pytest -q"
    assert call1.exit_code == 1
    assert call1.is_error is True
    assert "FAILED test_x" in call1.output

    call3 = next(t for t in parsed.tool_executions if t.call_id == "call_3")
    assert call3.command == "pytest -q"
    assert call3.exit_code == 0
    assert call3.is_error is False
    assert call3.output == "1 passed in 0.1s\n"

    assert len(parsed.file_edits) == 3
    assert parsed.file_edits[0].path == "src/app.py"
    assert parsed.file_edits[0].operation == "edit"
    assert parsed.file_edits[0].old_text == "VALUE = 400"
    assert parsed.file_edits[0].new_text == "VALUE = 200"
    assert parsed.file_edits[1].path == "src/new_mod.py"
    assert parsed.file_edits[1].operation == "write"
    assert parsed.file_edits[1].new_text == "def fresh():\n    return 1"
    assert parsed.file_edits[2].path == "src/legacy.py"
    assert parsed.file_edits[2].operation == "delete"
    assert parsed.modified_files == ["src/app.py", "src/new_mod.py", "src/legacy.py"]


def test_parse_codex_local_shell_and_move_patch() -> None:
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: old_name.py",
            "*** Move to: new_name.py",
            "-x = 1",
            "+x = 2",
            "*** End Patch",
        ]
    )
    records = [
        {
            "type": "local_shell_call",
            "call_id": "ls_1",
            "action": {"type": "exec", "command": ["go", "test", "./..."]},
        },
        {
            "type": "local_shell_call_output",
            "call_id": "ls_1",
            "output": "--- FAIL: TestAuth\nExit code: 1",
        },
        {
            "type": "function_call",
            "call_id": "ap_1",
            "name": "apply_patch",
            "arguments": json.dumps({"patch": patch}),
        },
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.format == "codex"
    ls = next(t for t in parsed.tool_executions if t.call_id == "ls_1")
    assert ls.command == "go test ./..."
    assert ls.exit_code == 1
    assert ls.is_error is True
    assert parsed.modified_files == ["old_name.py", "new_name.py"]


def test_parse_generic_jsonl_transcript() -> None:
    records = [
        {"session_id": "gen-1", "role": "user", "content": "Keep API backwards-compatible."},
        {"role": "assistant", "content": "Running test suite."},
        {
            "role": "tool",
            "tool_name": "pytest",
            "command": "pytest -v",
            "stdout": "1 failed",
            "stderr": "AssertionError",
            "exit_code": 1,
        },
        {
            "type": "file_edit",
            "path": "src/api.py",
            "operation": "edit",
            "old_text": "v1",
            "new_text": "v2",
        },
        {"role": "user", "content": [{"type": "text", "text": "Also support Python 3.11."}]},
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.format == "generic"
    assert parsed.session_id == "gen-1"
    assert [t.text for t in parsed.user_turns] == [
        "Keep API backwards-compatible.",
        "Also support Python 3.11.",
    ]
    assert len(parsed.tool_executions) == 1
    assert parsed.tool_executions[0].output == "1 failed\nAssertionError"
    assert parsed.tool_executions[0].exit_code == 1
    assert parsed.tool_executions[0].is_error is True
    assert parsed.modified_files == ["src/api.py"]


def test_resilience_to_malformed_lines_and_unexpected_types() -> None:
    lines = [
        "",
        "   ",
        "{not valid json",
        "[1, 2, 3]",
        '"just a string"',
        "null",
        json.dumps({"type": "user", "message": None}),
        json.dumps({"type": "user", "message": {"content": 12345}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "MultiEdit",
                            "input": {"file_path": "a.py", "edits": "not-a-list"},
                        }
                    ]
                },
            }
        ),
        json.dumps({"type": "user", "message": {"content": "Survive malformed lines!"}}),
    ]
    parsed = parse_transcript_lines(lines, fmt="claude-code")
    assert parsed.format == "claude-code"
    assert len(parsed.user_turns) == 1
    assert parsed.user_turns[0].text == "Survive malformed lines!"
    assert parsed.modified_files == ["a.py"]

    empty = parse_transcript_lines(["", "  \n", "bad json"])
    assert empty.format == "unknown"
    assert empty.user_turns == []
    assert detect_transcript_format([]) == "unknown"

    with pytest.raises(ValueError, match="Unsupported transcript format"):
        parse_transcript_lines([], fmt="xml")

    with pytest.raises(FileNotFoundError):
        parse_transcript(Path("/nonexistent/path/to/transcript.jsonl"))
