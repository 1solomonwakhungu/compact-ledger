"""Unit and integration tests for compact_ledger.ledger."""

from __future__ import annotations

from compact_ledger.ledger import (
    build_attempt_ledger,
    build_trajectories,
    detect_edit_thrash,
)
from compact_ledger.models import (
    FileEdit,
    ParsedTranscript,
    TestAttempt,
    ToolExecution,
)


def _pytest_fail_output(node: str, actual: str, expected: str = "100", lineno: int = 42) -> str:
    file_part, func_part = node.split("::", 1)
    return (
        "=== FAILURES ===\n"
        f"___ {func_part} ___\n"
        f"E       assert {actual} == {expected}\n"
        f"{file_part}:{lineno}: AssertionError\n"
        "=== short test summary info ===\n"
        f"FAILED {node} - assert {actual} == {expected}\n"
        "=== 1 failed in 0.05s ===\n"
    )


def test_trajectory_classifications_progressing_oscillating_stalled_single() -> None:
    attempts = [
        # PROGRESSING: 400 -> 200 -> 100
        TestAttempt(
            "tests/test_calc.py::test_prog",
            "pytest",
            "tests/test_calc.py:10",
            "50",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_prog",
            "pytest",
            "tests/test_calc.py:10",
            "50",
            "200",
            "failed",
            2,
        ),
        TestAttempt(
            "tests/test_calc.py::test_prog",
            "pytest",
            "tests/test_calc.py:10",
            "50",
            "100",
            "failed",
            3,
        ),
        # OSCILLATING: 400 -> 200 -> 400
        TestAttempt(
            "tests/test_calc.py::test_osc",
            "pytest",
            "tests/test_calc.py:42",
            "100",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_osc",
            "pytest",
            "tests/test_calc.py:42",
            "100",
            "200",
            "failed",
            2,
        ),
        TestAttempt(
            "tests/test_calc.py::test_osc",
            "pytest",
            "tests/test_calc.py:42",
            "100",
            "400",
            "failed",
            3,
        ),
        # STALLED: 400 -> 400
        TestAttempt(
            "tests/test_calc.py::test_stall",
            "pytest",
            "tests/test_calc.py:60",
            "100",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_stall",
            "pytest",
            "tests/test_calc.py:60",
            "100",
            "400",
            "failed",
            2,
        ),
        # STALLED after earlier move: 400 -> 200 -> 200
        TestAttempt(
            "tests/test_calc.py::test_stall_late",
            "pytest",
            "tests/test_calc.py:70",
            "100",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_stall_late",
            "pytest",
            "tests/test_calc.py:70",
            "100",
            "200",
            "failed",
            2,
        ),
        TestAttempt(
            "tests/test_calc.py::test_stall_late",
            "pytest",
            "tests/test_calc.py:70",
            "100",
            "200",
            "failed",
            3,
        ),
        # Broke out of stall: 400 -> 400 -> 200 -> PROGRESSING
        TestAttempt(
            "tests/test_calc.py::test_unstuck",
            "pytest",
            "tests/test_calc.py:80",
            "100",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_unstuck",
            "pytest",
            "tests/test_calc.py:80",
            "100",
            "400",
            "failed",
            2,
        ),
        TestAttempt(
            "tests/test_calc.py::test_unstuck",
            "pytest",
            "tests/test_calc.py:80",
            "100",
            "200",
            "failed",
            3,
        ),
        # SINGLE_FAILURE
        TestAttempt(
            "tests/test_calc.py::test_once",
            "pytest",
            "tests/test_calc.py:90",
            "10",
            "99",
            "failed",
            1,
        ),
        # Passing-only test must NOT produce a trajectory.
        TestAttempt(
            "tests/test_calc.py::test_always_green", "pytest", outcome="passed", turn_index=1
        ),
    ]

    trajs = build_trajectories(attempts)
    by_id = {t.test_id: t for t in trajs}

    assert "tests/test_calc.py::test_always_green" not in by_id
    assert by_id["tests/test_calc.py::test_prog"].status == "PROGRESSING"
    assert by_id["tests/test_calc.py::test_osc"].status == "OSCILLATING"
    assert by_id["tests/test_calc.py::test_stall"].status == "STALLED"
    assert by_id["tests/test_calc.py::test_stall_late"].status == "STALLED"
    assert by_id["tests/test_calc.py::test_unstuck"].status == "PROGRESSING"
    assert by_id["tests/test_calc.py::test_once"].status == "SINGLE_FAILURE"

    # Verify concise summary format
    expected_osc_summary = (
        "OSCILLATING: tests/test_calc.py::test_osc "
        "[400 -> 200 -> 400; expected 100] @ tests/test_calc.py:42"
    )
    assert by_id["tests/test_calc.py::test_osc"].summary == expected_osc_summary
    assert "400 (x2)" in by_id["tests/test_calc.py::test_stall"].summary

    # Verify urgent unresolved ordering (OSCILLATING -> STALLED -> PROGRESSING -> SINGLE_FAILURE)
    statuses = [t.status for t in trajs]
    assert statuses.index("OSCILLATING") < statuses.index("STALLED")
    assert statuses.index("STALLED") < statuses.index("PROGRESSING")
    assert statuses.index("PROGRESSING") < statuses.index("SINGLE_FAILURE")


def test_trajectory_regression_after_pass_is_not_resolved() -> None:
    attempts = [
        TestAttempt(
            "tests/test_a.py::test_1", "pytest", "tests/test_a.py:10", "100", "400", "failed", 1
        ),
        TestAttempt("tests/test_a.py::test_1", "pytest", outcome="passed", turn_index=2),
        TestAttempt(
            "tests/test_a.py::test_1", "pytest", "tests/test_a.py:10", "100", "400", "failed", 3
        ),
    ]
    trajs = build_trajectories(attempts)
    assert len(trajs) == 1
    assert trajs[0].status == "OSCILLATING"
    assert "400 -> PASS -> 400" in trajs[0].summary


def test_trajectory_max_states_per_test_bounds_summary() -> None:
    attempts = [
        TestAttempt(
            "tests/test_a.py::test_many", "pytest", "tests/test_a.py:5", "0", str(v), "failed", i
        )
        for i, v in enumerate([10, 9, 8, 7, 6, 5, 4, 3], start=1)
    ]
    trajs = build_trajectories(attempts, max_states_per_test=3)
    assert len(trajs) == 1
    assert trajs[0].status == "PROGRESSING"
    assert "[... -> 5 -> 4 -> 3; expected 0]" in trajs[0].summary


def test_pytest_scoped_pass_does_not_falsely_resolve_unrelated_test() -> None:
    transcript = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(
                turn_index=1,
                tool_name="Bash",
                command="pytest tests/test_a.py",
                output=_pytest_fail_output("tests/test_a.py::test_1", "400", "100"),
                exit_code=1,
                is_error=True,
                seq=1,
            ),
            # Unrelated file passing must NOT resolve tests/test_a.py::test_1!
            ToolExecution(
                turn_index=2,
                tool_name="Bash",
                command="pytest tests/test_b.py",
                output="=== 5 passed in 0.12s ===\n",
                exit_code=0,
                seq=2,
            ),
            # Marker-filtered run must NOT resolve tests/test_a.py::test_1!
            ToolExecution(
                turn_index=3,
                tool_name="Bash",
                command="pytest -m fast",
                output="=== 3 passed in 0.08s ===\n",
                exit_code=0,
                seq=3,
            ),
            # Ignored file run must NOT resolve tests/test_a.py::test_1!
            ToolExecution(
                turn_index=4,
                tool_name="Bash",
                command="pytest --ignore tests/test_a.py",
                output="=== 8 passed in 0.15s ===\n",
                exit_code=0,
                seq=4,
            ),
        ],
    )
    _, trajs, _ = build_attempt_ledger(transcript)
    assert len(trajs) == 1
    assert trajs[0].test_id == "tests/test_a.py::test_1"
    assert trajs[0].status == "SINGLE_FAILURE"


def test_pytest_matching_scoped_and_unfiltered_pass_resolve() -> None:
    # 1. Targeted file pass resolves parametrized node in that file.
    t_scoped = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(
                turn_index=1,
                tool_name="Bash",
                command="pytest tests/test_a.py",
                output=_pytest_fail_output("tests/test_a.py::test_disc[tier1]", "400", "100"),
                exit_code=1,
                is_error=True,
                seq=1,
            ),
            ToolExecution(
                turn_index=1,
                tool_name="Bash",
                command="pytest tests/test_a.py::test_disc",
                output="=== 2 passed in 0.04s ===\n",
                exit_code=0,
                seq=2,
            ),
        ],
    )
    _, trajs1, _ = build_attempt_ledger(t_scoped)
    assert trajs1[0].status == "RESOLVED"

    # 2. Unfiltered suite pass resolves everything for pytest.
    t_full = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(
                turn_index=1,
                tool_name="Bash",
                command="pytest tests/test_a.py",
                output=_pytest_fail_output("tests/test_a.py::test_1", "400", "100"),
                exit_code=1,
                is_error=True,
                seq=1,
            ),
            ToolExecution(
                turn_index=2,
                tool_name="Bash",
                command="uv run pytest -q",
                output="=== 20 passed in 0.30s ===\n",
                exit_code=0,
                seq=2,
            ),
        ],
    )
    _, trajs2, _ = build_attempt_ledger(t_full)
    assert trajs2[0].status == "RESOLVED"


def test_vitest_and_go_scoped_resolution_vs_unrelated_pass() -> None:
    # Vitest: passing src/b.test.ts must NOT resolve src/a.test.ts; passing src/a.test.ts DOES.
    vitest_fail = (
        " RUN  v2.1.0\n"
        " FAIL  src/a.test.ts > Suite > works\n"
        "AssertionError: expected 400 to be 100\n"
        " ❯ src/a.test.ts:12:5\n"
        " Test Files  1 failed (1)\n"
    )
    vitest_pass_b = (
        " RUN  v2.1.0\n"
        " ✓ src/b.test.ts (2 tests) 5ms\n"
        " Test Files  1 passed (1)\n"
        "      Tests  2 passed (2)\n"
    )
    vitest_pass_a = (
        " RUN  v2.1.0\n"
        " ✓ src/a.test.ts (1 test) 4ms\n"
        " Test Files  1 passed (1)\n"
        "      Tests  1 passed (1)\n"
    )

    t_v_unrelated = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "vitest run src/a.test.ts", vitest_fail, 1, True, seq=1),
            ToolExecution(2, "Bash", "vitest run src/b.test.ts", vitest_pass_b, 0, False, seq=2),
        ],
    )
    _, v_trajs1, _ = build_attempt_ledger(t_v_unrelated)
    assert v_trajs1[0].status == "SINGLE_FAILURE"

    t_v_resolved = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "vitest run src/a.test.ts", vitest_fail, 1, True, seq=1),
            ToolExecution(2, "Bash", "vitest run src/a.test.ts", vitest_pass_a, 0, False, seq=2),
        ],
    )
    _, v_trajs2, _ = build_attempt_ledger(t_v_resolved)
    assert v_trajs2[0].status == "RESOLVED"

    # Go: passing ./pkg/b must NOT resolve failure in example.com/pkg/a; passing ./pkg/a DOES.
    go_fail = (
        "--- FAIL: TestAuth (0.00s)\n"
        "    auth_test.go:20: got 401, want 200\n"
        "FAIL\n"
        "FAIL\texample.com/pkg/a\t0.010s\n"
    )
    t_go_unrelated = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "go test ./pkg/a", go_fail, 1, True, seq=1),
            ToolExecution(
                2, "Bash", "go test ./pkg/b", "ok  \texample.com/pkg/b\t0.004s\n", 0, False, seq=2
            ),
            ToolExecution(
                3,
                "Bash",
                "go test -run TestOther ./...",
                "ok  \texample.com/pkg/a\t0.004s\n",
                0,
                False,
                seq=3,
            ),
        ],
    )
    _, go_trajs1, _ = build_attempt_ledger(t_go_unrelated)
    assert go_trajs1[0].status == "SINGLE_FAILURE"

    t_go_resolved = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "go test ./pkg/a", go_fail, 1, True, seq=1),
            ToolExecution(
                2, "Bash", "go test ./pkg/a", "ok  \texample.com/pkg/a\t0.004s\n", 0, False, seq=2
            ),
        ],
    )
    _, go_trajs2, _ = build_attempt_ledger(t_go_resolved)
    assert go_trajs2[0].status == "RESOLVED"


def test_edit_thrash_hunk_revert_and_write_revert() -> None:
    edits = [
        FileEdit(
            1,
            "src/calc.py",
            "edit",
            old_text="return price * 4",
            new_text="return price * 2",
            seq=1,
        ),
        FileEdit(
            2,
            "src/calc.py",
            "edit",
            old_text="return price * 2",
            new_text="return price * 4",
            seq=3,
        ),
        # Full-file write revert: v1 -> v2 -> v1
        FileEdit(1, "src/cfg.py", "write", new_text="MODE = 'a'\n", seq=4),
        FileEdit(2, "src/cfg.py", "write", new_text="MODE = 'b'\n", seq=5),
        FileEdit(3, "src/cfg.py", "write", new_text="MODE = 'a'\n", seq=6),
    ]
    tools = [
        ToolExecution(
            1,
            "Bash",
            "pytest tests/test_calc.py",
            _pytest_fail_output("tests/test_calc.py::test_price", "200", "100"),
            1,
            True,
            seq=2,
        ),
        ToolExecution(
            2,
            "Bash",
            "pytest tests/test_calc.py",
            _pytest_fail_output("tests/test_calc.py::test_price", "400", "100"),
            1,
            True,
            seq=7,
        ),
    ]

    thrash = detect_edit_thrash(edits, tools)
    by_path = {e.path: e for e in thrash}

    assert "src/calc.py" in by_path
    assert by_path["src/calc.py"].reverted is True
    assert by_path["src/calc.py"].edit_count == 2
    assert by_path["src/calc.py"].failure_count == 2
    assert by_path["src/calc.py"].turn_indices == [1, 2]
    assert "Reverted" in by_path["src/calc.py"].reason

    assert "src/cfg.py" in by_path
    assert by_path["src/cfg.py"].reverted is True
    assert by_path["src/cfg.py"].edit_count == 3


def test_edit_thrash_repeated_edits_between_stalled_failures_and_unrelated_pass() -> None:
    edits = [
        FileEdit(1, "src/parser.py", "edit", old_text="x = 1", new_text="x = 2", seq=1),
        FileEdit(2, "src/parser.py", "edit", old_text="x = 2", new_text="x = 3", seq=3),
    ]
    tools = [
        ToolExecution(
            1,
            "Bash",
            "pytest tests/test_parser.py",
            _pytest_fail_output("tests/test_parser.py::test_parse", "400", "100"),
            1,
            True,
            seq=2,
        ),
        ToolExecution(
            2,
            "Bash",
            "pytest tests/test_parser.py",
            _pytest_fail_output("tests/test_parser.py::test_parse", "400", "100"),
            1,
            True,
            seq=4,
        ),
        # Unrelated passing test file must NOT suppress the thrash warning!
        ToolExecution(
            3,
            "Bash",
            "pytest tests/test_other.py",
            "=== 4 passed in 0.05s ===\n",
            0,
            False,
            seq=5,
        ),
    ]

    thrash = detect_edit_thrash(edits, tools)
    assert len(thrash) == 1
    assert thrash[0].path == "src/parser.py"
    assert thrash[0].reverted is False
    assert thrash[0].edit_count == 2
    assert thrash[0].failure_count == 2
    assert "stalled" in thrash[0].reason.lower()


def test_edit_thrash_no_false_positive_on_clean_edit_to_pass_or_unrelated_edits() -> None:
    # Case 1: edit -> fail -> edit -> clean pass (normal debugging, no thrash!)
    edits_clean = [
        FileEdit(1, "src/math.py", "edit", old_text="return a - b", new_text="return a * b", seq=1),
        FileEdit(2, "src/math.py", "edit", old_text="return a * b", new_text="return a + b", seq=3),
    ]
    tools_clean = [
        ToolExecution(
            1,
            "Bash",
            "pytest tests/test_math.py",
            _pytest_fail_output("tests/test_math.py::test_add", "6", "5"),
            1,
            True,
            seq=2,
        ),
        ToolExecution(
            2,
            "Bash",
            "pytest tests/test_math.py",
            "=== 3 passed in 0.03s ===\n",
            0,
            False,
            seq=4,
        ),
    ]
    assert detect_edit_thrash(edits_clean, tools_clean) == []

    # Case 2: 3 edits to README before any failure, followed by 1 unrelated failure (no thrash!)
    edits_pre = [
        FileEdit(1, "README.md", "edit", old_text="a", new_text="b", seq=1),
        FileEdit(1, "README.md", "edit", old_text="b", new_text="c", seq=2),
        FileEdit(1, "README.md", "edit", old_text="c", new_text="d", seq=3),
    ]
    tools_pre = [
        ToolExecution(
            2,
            "Bash",
            "pytest tests/test_app.py",
            _pytest_fail_output("tests/test_app.py::test_run", "1", "2"),
            1,
            True,
            seq=4,
        ),
    ]
    assert detect_edit_thrash(edits_pre, tools_pre) == []


def test_build_attempt_ledger_end_to_end_multi_turn_session() -> None:
    transcript = ParsedTranscript(
        format="claude-code",
        session_id="sess-e2e",
        file_edits=[
            FileEdit(1, "src/pricing.py", "edit", old_text="rate = 4", new_text="rate = 2", seq=2),
            FileEdit(2, "src/pricing.py", "edit", old_text="rate = 2", new_text="rate = 4", seq=4),
        ],
        tool_executions=[
            ToolExecution(
                1,
                "Bash",
                "pytest tests/test_pricing.py",
                _pytest_fail_output("tests/test_pricing.py::test_total", "400", "100"),
                1,
                True,
                seq=1,
            ),
            ToolExecution(
                1,
                "Bash",
                "pytest tests/test_pricing.py",
                _pytest_fail_output("tests/test_pricing.py::test_total", "200", "100"),
                1,
                True,
                seq=3,
            ),
            ToolExecution(
                2,
                "Bash",
                "pytest tests/test_pricing.py",
                _pytest_fail_output("tests/test_pricing.py::test_total", "400", "100"),
                1,
                True,
                seq=5,
            ),
            ToolExecution(
                2,
                "Bash",
                "mypy src/",
                "src/pricing.py:5: error: Missing return statement\n",
                1,
                True,
                seq=6,
            ),
            ToolExecution(
                3,
                "Bash",
                "mypy src/",
                "Success: no issues found\n",
                0,
                False,
                seq=7,
            ),
        ],
    )
    attempts, trajectories, thrash = build_attempt_ledger(transcript)

    assert len(attempts) == 5
    by_id = {t.test_id: t for t in trajectories}
    assert by_id["tests/test_pricing.py::test_total"].status == "OSCILLATING"
    expected_summary = (
        "OSCILLATING: tests/test_pricing.py::test_total "
        "[400 -> 200 -> 400; expected 100] @ tests/test_pricing.py:42"
    )
    assert by_id["tests/test_pricing.py::test_total"].summary == expected_summary
    assert by_id["mypy src/"].status == "RESOLVED"

    assert len(thrash) == 1
    assert thrash[0].path == "src/pricing.py"
    assert thrash[0].reverted is True
    assert thrash[0].edit_count == 2
    assert thrash[0].failure_count == 4
