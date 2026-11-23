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
                        "content": [{"type": "text", "text": "Exit code 1\nE   assert 400 == 100"}],
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
            "output": "Exit code: 1\nOutput:\n--- FAIL: TestAuth\n",
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


def test_exit_code_scanning_avoids_false_positives_and_redos() -> None:
    import time

    records = [
        {"type": "user", "message": {"role": "user", "content": "Inspect logs and run tests."}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t_read",
                        "name": "Read",
                        "input": {"file_path": "ci.log"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t_ok_bash",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t_hdr",
                        "name": "Bash",
                        "input": {"command": "run"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t_nl",
                        "name": "Bash",
                        "input": {"command": "spam"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_read",
                        "is_error": False,
                        "content": "Process exited with code 137\nExit code: 2\n",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_ok_bash",
                        "is_error": False,
                        "content": "Exit code 1\nchild exited with code 1 as expected",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_hdr",
                        "is_error": True,
                        "content": "Exit code 2\nlog line\nProcess exited with code 0",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_nl",
                        "is_error": True,
                        "content": "\n" * 50_000 + "done",
                    },
                ],
            },
        },
    ]

    t0 = time.monotonic()
    parsed = parse_transcript_lines(_jsonl(records))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5

    by_id = {t.call_id: t for t in parsed.tool_executions}
    assert by_id["t_read"].exit_code is None
    assert by_id["t_read"].is_error is False
    assert by_id["t_ok_bash"].exit_code is None
    assert by_id["t_ok_bash"].is_error is False
    assert by_id["t_hdr"].exit_code == 2
    assert by_id["t_hdr"].is_error is True
    assert by_id["t_nl"].exit_code is None


def test_seq_ordering_interleaves_edits_and_tests_and_excludes_failed_edits() -> None:
    records = [
        {"type": "user", "message": {"role": "user", "content": "Fix calc bug"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "Edit",
                        "input": {"file_path": "a.py", "old_string": "1", "new_string": "2"},
                    },
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                    {
                        "type": "tool_use",
                        "id": "e_fail",
                        "name": "Edit",
                        "input": {"file_path": "nope.py", "old_string": "x", "new_string": "y"},
                    },
                    {
                        "type": "tool_use",
                        "id": "e2",
                        "name": "Edit",
                        "input": {"file_path": "a.py", "old_string": "2", "new_string": "3"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "e1",
                        "is_error": False,
                        "content": "ok",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "b1",
                        "is_error": True,
                        "content": "Exit code 1\nFAIL",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "e_fail",
                        "is_error": True,
                        "content": "String to replace not found",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "e2",
                        "is_error": False,
                        "content": "ok",
                    },
                ],
            },
        },
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.modified_files == ["a.py"]
    assert [e.call_id for e in parsed.file_edits] == ["e1", "e2"]
    b1 = next(t for t in parsed.tool_executions if t.call_id == "b1")
    e1, e2 = parsed.file_edits
    # Same turn_index, but seq strictly orders e1 < b1 < e2
    assert e1.turn_index == b1.turn_index == e2.turn_index == 1
    assert parsed.user_turns[0].seq < e1.seq < b1.seq < e2.seq


def test_codex_apply_patch_preserves_plus_minus_lines_and_shell_patch_and_output_unwrapping() -> (
    None
):
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: query.sql",
            "@@ -1,3 +1,3 @@",
            "---",
            "--- old comment",
            "+++i;",
            "@@ -10,1 +10,1 @@",
            "-old2",
            "+new2",
            "*** End Patch",
        ]
    )
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>cwd=/repo</environment_context>",
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Apply SQL fix"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c_patch",
                "arguments": json.dumps({"command": ["apply_patch", patch]}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_patch",
                "output": "Exit code: 0\nOutput:\nProcess exited with code 3\n",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c_plain",
                "arguments": json.dumps({"cmd": "echo hi"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_plain",
                "output": "header line\nOutput:\nbody line",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c_json_stdout",
                "arguments": json.dumps({"cmd": "cat report.json"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_json_stdout",
                "output": json.dumps({"output": {"count": 3}, "status": "ok"}),
            },
        },
    ]

    parsed = parse_transcript_lines(_jsonl(records))
    assert [t.text for t in parsed.user_turns] == ["Apply SQL fix"]
    # Two hunks in query.sql produce two contiguous FileEdits without dropping '--'/'++'
    assert len(parsed.file_edits) == 2
    assert parsed.file_edits[0].old_text == "--\n-- old comment"
    assert parsed.file_edits[0].new_text == "++i;"
    assert parsed.file_edits[1].old_text == "old2"
    assert parsed.file_edits[1].new_text == "new2"

    by_id = {t.call_id: t for t in parsed.tool_executions}
    # Header Exit code: 0 must NOT be overridden by body 'Process exited with code 3'
    assert by_id["c_patch"].exit_code == 0
    assert by_id["c_patch"].is_error is False
    # Non-Codex-header plain output containing 'Output:\n' is preserved intact
    assert by_id["c_plain"].output == "header line\nOutput:\nbody line"
    # Raw JSON stdout with non-string 'output' key is preserved instead of returning ""
    assert '"count": 3' in by_id["c_json_stdout"].output


def test_fail_open_on_recursion_huge_int_superscript_bom_and_surrogates() -> None:
    deep_nested = "[" * 2000 + "]" * 2000
    huge_int = '{"x": ' + ("9" * 5000) + "}"
    bom_first = "\ufeff" + json.dumps({"role": "user", "content": "First turn \ud83d"})
    superscript_exit = json.dumps(
        {"role": "tool", "command": "pytest", "output": "ok", "exit_code": "\u00b2"}
    )
    parsed = parse_transcript_lines([bom_first, deep_nested, huge_int, superscript_exit])
    assert parsed.format == "generic"
    assert len(parsed.user_turns) == 1
    assert parsed.user_turns[0].text == "First turn \ufffd"
    assert len(parsed.tool_executions) == 1
    assert parsed.tool_executions[0].exit_code is None


def test_claude_noise_entries_excluded_from_user_turns() -> None:
    records = [
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": "Subagent prompt"},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Keep zero runtime deps."},
        },
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert [t.text for t in parsed.user_turns] == ["Keep zero runtime deps."]
