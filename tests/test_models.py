"""Tests for compact_ledger.models."""

from __future__ import annotations

import json

import pytest

from compact_ledger.models import (
    SCHEMA_VERSION,
    CompactionSnapshot,
    EditThrashEntry,
    FileEdit,
    ParsedTranscript,
    PinEntry,
    TestAttempt,
    TestTrajectory,
    ToolExecution,
    UserTurn,
    make_pin_id,
    normalize_pin_text,
)


def test_user_turn_round_trip() -> None:
    turn = UserTurn(
        turn_index=2,
        text="Never touch the billing migrations.",
        timestamp="2026-01-15T10:00:00Z",
        source="claude-code",
    )
    payload = turn.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert UserTurn.from_dict(payload) == turn


def test_tool_execution_round_trip() -> None:
    exe = ToolExecution(
        turn_index=3,
        tool_name="Bash",
        command="pytest -q",
        output="FAILED tests/test_math.py::test_add",
        exit_code=1,
        is_error=True,
        timestamp="2026-01-15T10:01:00Z",
        call_id="toolu_123",
    )
    payload = exe.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert ToolExecution.from_dict(payload) == exe


def test_file_edit_round_trip_and_validation() -> None:
    edit = FileEdit(
        turn_index=1,
        path="src/app.py",
        operation="edit",
        old_text="x = 1",
        new_text="x = 2",
        timestamp="2026-01-15T10:02:00Z",
    )
    assert FileEdit.from_dict(edit.to_dict()) == edit

    with pytest.raises(ValueError, match="FileEdit.operation"):
        FileEdit(turn_index=1, path="src/app.py", operation="rename")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="FileEdit.operation"):
        FileEdit.from_dict({"turn_index": 1, "path": "a.py", "operation": "chmod"})


def test_parsed_transcript_round_trip() -> None:
    parsed = ParsedTranscript(
        format="claude-code",
        session_id="sess-1",
        user_turns=[UserTurn(turn_index=1, text="Fix tests")],
        tool_executions=[ToolExecution(turn_index=1, tool_name="Bash", command="pytest")],
        file_edits=[FileEdit(turn_index=1, path="a.py", operation="write", new_text="pass\n")],
        modified_files=["a.py"],
    )
    payload = parsed.to_dict()
    restored = ParsedTranscript.from_dict(payload)
    assert restored == parsed
    # Verify defensive copying
    payload["modified_files"].append("mutated.py")
    assert restored.modified_files == ["a.py"]


def test_pin_entry_round_trip_and_validation() -> None:
    pin = PinEntry(
        pin_id="pin-override-1",
        text="Actually use July 31 instead of June 30.",
        source="override",
        turn_index=4,
        timestamp="2026-01-15T10:05:00Z",
        supersedes="pin-auto-1",
        superseded_by=None,
        is_negative=False,
        tags=["deadline", "override"],
    )
    payload = pin.to_dict()
    restored = PinEntry.from_dict(payload)
    assert restored == pin
    payload["tags"].append("mutated")
    assert restored.tags == ["deadline", "override"]

    with pytest.raises(ValueError, match="PinEntry.source"):
        PinEntry(pin_id="p1", text="hi", source="unknown")  # type: ignore[arg-type]


def test_test_attempt_and_trajectory_round_trip() -> None:
    attempt = TestAttempt(
        test_id="tests/test_api.py::test_timeout",
        runner="pytest",
        assertion_loc="tests/test_api.py:42",
        expected="100",
        actual="400",
        outcome="failed",
        turn_index=2,
        message="assert 400 == 100",
        command="pytest tests/test_api.py",
    )
    traj = TestTrajectory(
        test_id=attempt.test_id,
        runner="pytest",
        status="PROGRESSING",
        attempts=[attempt],
        summary="400 -> 200 -> 100",
    )
    assert TestAttempt.from_dict(attempt.to_dict()) == attempt
    assert TestTrajectory.from_dict(traj.to_dict()) == traj

    with pytest.raises(ValueError, match="TestAttempt.outcome"):
        TestAttempt(test_id="t", runner="pytest", outcome="skipped")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="TestTrajectory.status"):
        TestTrajectory(test_id="t", runner="pytest", status="UNKNOWN")  # type: ignore[arg-type]


def test_edit_thrash_entry_round_trip() -> None:
    entry = EditThrashEntry(
        path="src/parser.py",
        edit_count=4,
        failure_count=3,
        reverted=True,
        turn_indices=[2, 3, 4, 5],
        reason="Reverted hunk from turn 2 while test still failing",
    )
    payload = entry.to_dict()
    restored = EditThrashEntry.from_dict(payload)
    assert restored == entry
    payload["turn_indices"].append(99)
    assert restored.turn_indices == [2, 3, 4, 5]


def test_compaction_snapshot_full_json_round_trip() -> None:
    attempt = TestAttempt(
        test_id="tests/test_calc.py::test_sum",
        runner="pytest",
        assertion_loc="tests/test_calc.py:10",
        expected="10",
        actual="12",
        outcome="failed",
        turn_index=2,
        message="assert 12 == 10",
        command="pytest",
    )
    snapshot = CompactionSnapshot(
        session_id="sess-full-roundtrip",
        created_at="2026-01-15T12:00:00Z",
        transcript_path="/tmp/transcript.jsonl",
        pins=[
            PinEntry(
                pin_id="pin-manual-1",
                text="Do not add third-party dependencies.",
                source="manual",
                is_negative=True,
                tags=["constraint"],
            ),
            PinEntry(
                pin_id="pin-auto-1",
                text="Keep Python 3.11 compatibility.",
                source="auto",
                turn_index=1,
            ),
        ],
        attempts=[attempt],
        trajectories=[
            TestTrajectory(
                test_id=attempt.test_id,
                runner="pytest",
                status="SINGLE_FAILURE",
                attempts=[attempt],
                summary="Single failure: got 12 expected 10",
            )
        ],
        thrash_entries=[
            EditThrashEntry(
                path="src/calc.py",
                edit_count=3,
                failure_count=2,
                reverted=False,
                turn_indices=[1, 2, 3],
                reason="Repeated edits without passing",
            )
        ],
        modified_files=["src/calc.py", "tests/test_calc.py"],
        schema_version=SCHEMA_VERSION,
        metadata={"transcript_format": "claude-code", "user_turn_count": 2},
    )

    raw_json = snapshot.to_json()
    restored = CompactionSnapshot.from_json(raw_json)
    assert restored == snapshot


def test_from_dict_rejects_bool_for_int_and_invalid_json() -> None:
    with pytest.raises(ValueError, match="turn_index must be an int"):
        UserTurn.from_dict({"turn_index": True, "text": "hello"})

    with pytest.raises(ValueError, match="exit_code must be an int"):
        ToolExecution.from_dict({"turn_index": 1, "tool_name": "Bash", "exit_code": False})

    with pytest.raises(ValueError, match="schema_version must be an int"):
        CompactionSnapshot.from_dict({"session_id": "s1", "schema_version": True})

    with pytest.raises(ValueError, match="Invalid snapshot JSON"):
        CompactionSnapshot.from_json("{not valid json")

    with pytest.raises(ValueError, match="expected a dict"):
        CompactionSnapshot.from_json("[1, 2, 3]")


def test_make_pin_id_is_deterministic_and_normalizes_whitespace() -> None:
    assert normalize_pin_text("  Never   use\tmocks \n here ") == "Never use mocks here"
    id1 = make_pin_id("manual", "  Use   UTC  ")
    id2 = make_pin_id("manual", "Use UTC")
    id3 = make_pin_id("manual", "use utc")
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("pin-manual-")
    # Lone UTF-16 surrogates must never crash make_pin_id or JSON serialization
    surrogate_id = make_pin_id("manual", "bad \ud83d surrogate")
    assert surrogate_id.startswith("pin-manual-")


def test_seq_and_call_id_round_trip() -> None:
    turn = UserTurn(turn_index=1, text="Run tests", seq=1)
    exe = ToolExecution(turn_index=1, tool_name="Bash", command="pytest", call_id="c1", seq=2)
    edit = FileEdit(turn_index=1, path="a.py", operation="edit", call_id="c2", seq=3)
    assert UserTurn.from_dict(turn.to_dict()) == turn
    assert ToolExecution.from_dict(exe.to_dict()) == exe
    assert FileEdit.from_dict(edit.to_dict()) == edit
    # Missing seq/call_id defaults cleanly for forward/backward compatibility
    assert FileEdit.from_dict({"turn_index": 1, "path": "a.py"}).seq == 0
    assert FileEdit.from_dict({"turn_index": 1, "path": "a.py"}).call_id is None
