"""Unit and integration tests for compact_ledger.ledger."""

from __future__ import annotations

import time

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
        # STALLED even when assertion line number shifts (7 -> 9 -> 12)
        TestAttempt(
            "tests/test_calc.py::test_line_drift",
            "pytest",
            "tests/test_calc.py:7",
            "100",
            "400",
            "failed",
            1,
        ),
        TestAttempt(
            "tests/test_calc.py::test_line_drift",
            "pytest",
            "tests/test_calc.py:9",
            "100",
            "400",
            "failed",
            2,
        ),
        TestAttempt(
            "tests/test_calc.py::test_line_drift",
            "pytest",
            "tests/test_calc.py:12",
            "100",
            "400",
            "failed",
            3,
        ),
        # STALLED with ephemeral pytest tmpdir paths normalized
        TestAttempt(
            "tests/test_calc.py::test_tmp",
            "pytest",
            "tests/test_calc.py:20",
            outcome="failed",
            turn_index=1,
            message="FileNotFoundError: /tmp/pytest-of-u/pytest-11/data.json",
        ),
        TestAttempt(
            "tests/test_calc.py::test_tmp",
            "pytest",
            "tests/test_calc.py:21",
            outcome="failed",
            turn_index=2,
            message="FileNotFoundError: /tmp/pytest-of-u/pytest-12/data.json",
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
    assert by_id["tests/test_calc.py::test_line_drift"].status == "STALLED"
    assert by_id["tests/test_calc.py::test_tmp"].status == "STALLED"
    assert by_id["tests/test_calc.py::test_stall_late"].status == "STALLED"
    assert by_id["tests/test_calc.py::test_unstuck"].status == "PROGRESSING"
    assert by_id["tests/test_calc.py::test_once"].status == "SINGLE_FAILURE"

    expected_osc_summary = (
        "OSCILLATING: tests/test_calc.py::test_osc "
        "[400 -> 200 -> 400; expected 100] @ tests/test_calc.py:42"
    )
    assert by_id["tests/test_calc.py::test_osc"].summary == expected_osc_summary
    assert "400 (x2)" in by_id["tests/test_calc.py::test_stall"].summary

    statuses = [t.status for t in trajs]
    assert statuses.index("OSCILLATING") < statuses.index("STALLED")
    assert statuses.index("STALLED") < statuses.index("PROGRESSING")
    assert statuses.index("PROGRESSING") < statuses.index("SINGLE_FAILURE")


def test_trajectory_regression_after_explicit_and_implicit_pass() -> None:
    # Explicit pass in between -> OSCILLATING
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
    assert trajs[0].status == "OSCILLATING"
    assert "400 -> PASS -> 400" in trajs[0].summary

    # Implicit full passing suite in between -> also visible as 400 -> PASS -> 400!
    transcript = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(
                1,
                "Bash",
                "pytest tests/test_a.py",
                _pytest_fail_output("tests/test_a.py::test_1", "400", "100"),
                1,
                True,
                seq=1,
            ),
            ToolExecution(2, "Bash", "pytest", "=== 10 passed in 0.10s ===\n", 0, False, seq=2),
            ToolExecution(
                3,
                "Bash",
                "pytest tests/test_a.py",
                _pytest_fail_output("tests/test_a.py::test_1", "400", "100"),
                1,
                True,
                seq=3,
            ),
        ],
    )
    _, trajs2, _ = build_attempt_ledger(transcript)
    assert trajs2[0].status == "OSCILLATING"
    assert "400 -> PASS -> 400" in trajs2[0].summary


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


def test_unknown_scope_wrappers_chdir_and_filters_do_not_falsely_resolve_pytest() -> None:
    # Every single one of these runs does NOT prove `tests/test_a.py::test_x` passed!
    suspicious_passes = [
        "pytest tests/test_b.py",
        "pytest -m fast",
        "pytest --ignore tests/test_a.py",
        "pytest --ignore-glob 'tests/test_a*'",
        "pytest -kb_only",
        "coverage run -m pytest tests/test_b.py",
        "bash -lc 'pytest tests/test_b.py'",
        "python -X dev -m pytest tests/test_b.py",
        "tox -e py312 -- tests/test_b.py",
        "timeout -s KILL 60 pytest tests/test_b.py",
        "nice -n 10 pytest tests/test_b.py",
        "docker compose exec app pytest tests/test_b.py",
        "cd services/other && pytest",
    ]
    execs = [
        ToolExecution(
            1,
            "Bash",
            "pytest tests/test_a.py",
            _pytest_fail_output("tests/test_a.py::test_x", "400", "100"),
            1,
            True,
            seq=1,
        )
    ]
    for idx, cmd in enumerate(suspicious_passes, start=2):
        execs.append(
            ToolExecution(idx, "Bash", cmd, "=== 5 passed in 0.08s ===\n", 0, False, seq=idx)
        )
    # Skipping a test (`1 passed, 1 skipped`) must also NOT implicitly resolve it.
    execs.append(
        ToolExecution(
            99, "Bash", "pytest", "=== 1 passed, 1 skipped in 0.05s ===\n", 0, False, seq=99
        )
    )

    _, trajs, _ = build_attempt_ledger(
        ParsedTranscript(format="claude-code", tool_executions=execs)
    )
    assert len(trajs) == 1
    assert trajs[0].test_id == "tests/test_a.py::test_x"
    assert trajs[0].status == "SINGLE_FAILURE"


def test_pytest_and_vitest_redirects_and_scoped_passes_resolve() -> None:
    # Pytest with shell redirects (`2>&1 | tail -30` and `> /tmp/o.log 2>&1`) resolves cleanly!
    for pass_cmd in (
        "pytest 2>&1 | tail -30",
        "pytest > /tmp/o.log 2>&1; tail -20 /tmp/o.log",
        "uv run pytest -q",
        "pytest tests/test_a.py::test_disc",
    ):
        transcript = ParsedTranscript(
            format="claude-code",
            tool_executions=[
                ToolExecution(
                    1,
                    "Bash",
                    "pytest tests/test_a.py",
                    _pytest_fail_output("tests/test_a.py::test_disc[tier1]", "400", "100"),
                    1,
                    True,
                    seq=1,
                ),
                ToolExecution(2, "Bash", pass_cmd, "=== 12 passed in 0.20s ===\n", 0, False, seq=2),
            ],
        )
        _, trajs, _ = build_attempt_ledger(transcript)
        assert trajs[0].status == "RESOLVED", f"Expected RESOLVED for {pass_cmd!r}"

    # Vitest with `2>&1 | tail -4` resolves cleanly; `npm test -- src/b.test.ts` & chdir do NOT.
    vitest_fail = (
        " RUN  v2.1.0\n"
        " FAIL  src/a.test.ts > Suite > works\n"
        "AssertionError: expected 400 to be 100\n"
        " ❯ src/a.test.ts:12:5\n"
        " Test Files  1 failed (1)\n"
    )
    vitest_dot_pass = " RUN  v2.1.0\n Test Files  1 passed (1)\n      Tests  2 passed (2)\n"

    t_unverified = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "vitest run src/a.test.ts", vitest_fail, 1, True, seq=1),
            ToolExecution(
                2,
                "Bash",
                "npm test -- src/b.test.ts 2>&1 | tail -4",
                vitest_dot_pass,
                0,
                False,
                seq=2,
            ),
            ToolExecution(
                3,
                "Bash",
                "cd packages/other && npx vitest run --reporter=dot",
                vitest_dot_pass,
                0,
                False,
                seq=3,
            ),
        ],
    )
    _, trajs_u, _ = build_attempt_ledger(t_unverified)
    assert trajs_u[0].status == "SINGLE_FAILURE"

    t_redirect_pass = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "vitest run src/a.test.ts", vitest_fail, 1, True, seq=1),
            ToolExecution(
                2, "Bash", "npx vitest run 2>&1 | tail -4", vitest_dot_pass, 0, False, seq=2
            ),
        ],
    )
    _, trajs_r, _ = build_attempt_ledger(t_redirect_pass)
    assert trajs_r[0].status == "RESOLVED"


def test_jest_t_matches_test_name_only_and_is_redos_safe() -> None:
    jest_fail = (
        "FAIL src/calc.test.js\n"
        "  ● divides by zero\n"
        "    Expected: 0\n"
        "    Received: Infinity\n"
        "      at Object.<anonymous> (src/calc.test.js:10:5)\n"
        "Test Suites: 1 failed, 1 total\n"
    )
    jest_pass_filtered = (
        "PASS src/calc.test.js\n"
        "Test Suites: 1 passed, 1 total\n"
        "Tests:       1 skipped, 1 passed, 2 total\n"
    )
    # `-t calc` matches file name `calc.test.js` but NOT test name `divides by zero`!
    t0 = time.perf_counter()
    transcript = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "npx jest src/calc.test.js", jest_fail, 1, True, seq=1),
            ToolExecution(2, "Bash", "npx jest -t calc", jest_pass_filtered, 0, False, seq=2),
            # Adversarial ReDoS pattern in `-t` must return instantly without backtracking!
            ToolExecution(
                3,
                "Bash",
                "npx jest -t '(a+)+$'",
                "PASS src/calc.test.js\nTest Suites: 1 passed, 1 total\nTests: 1 passed, 1 total\n",
                0,
                False,
                seq=3,
            ),
        ],
    )
    _, trajs, _ = build_attempt_ledger(transcript)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2
    assert trajs[0].status == "SINGLE_FAILURE"


def test_go_qualified_ids_and_all_scoping_guards() -> None:
    go_fail_pkg_a = (
        "--- FAIL: TestParse (0.00s)\n"
        "    parse_test.go:20: got 3, want 1\n"
        "FAIL\n"
        "FAIL\texample.com/m/pkg/api\t0.010s\n"
    )
    transcript = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "go test ./pkg/api", go_fail_pkg_a, 1, True, seq=1),
            # 1. Same test name passing in a DIFFERENT package must NOT resolve pkg/api::TestParse!
            ToolExecution(
                2,
                "Bash",
                "go test -v ./pkg/other",
                (
                    "=== RUN   TestParse\n"
                    "--- PASS: TestParse (0.00s)\n"
                    "PASS\n"
                    "ok  \texample.com/m/pkg/other\t0.004s\n"
                ),
                0,
                False,
                seq=2,
            ),
            # 2. Bare `go test` (current package `.`, not `./...`) must NOT resolve pkg/api!
            ToolExecution(
                3, "Bash", "go test", "PASS\nok  \texample.com/m\t0.004s\n", 0, False, seq=3
            ),
            # 3. `go test ./api` (example.com/m/api) must NOT suffix-match example.com/m/pkg/api!
            ToolExecution(
                4,
                "Bash",
                "go test ./api",
                "ok  \texample.com/m/api\t0.004s\n",
                0,
                False,
                seq=4,
            ),
            # 4. `[no tests to run]` must NOT resolve!
            ToolExecution(
                5,
                "Bash",
                "go test ./pkg/api",
                "ok  \texample.com/m/pkg/api\t0.002s [no tests to run]\n",
                0,
                False,
                seq=5,
            ),
            # 5. Unrelated script printing `ok  done` must NOT resolve!
            ToolExecution(6, "Bash", "./scripts/lint.sh", "ok  done\n", 0, False, seq=6),
            # 6. Evil ReDoS `-run` pattern must be instant and NOT falsely resolve!
            ToolExecution(
                7,
                "Bash",
                "go test -run '(a+)+$' ./...",
                "ok  \texample.com/m/pkg/api\t0.004s\n",
                0,
                False,
                seq=7,
            ),
        ],
    )
    t0 = time.perf_counter()
    _, trajs, _ = build_attempt_ledger(transcript)
    assert time.perf_counter() - t0 < 0.2
    assert len(trajs) == 1
    assert trajs[0].test_id == "example.com/m/pkg/api::TestParse"
    assert trajs[0].status == "SINGLE_FAILURE"

    # Now an exact package pass DOES resolve it!
    resolved_tx = ParsedTranscript(
        format="claude-code",
        tool_executions=[
            ToolExecution(1, "Bash", "go test ./pkg/api", go_fail_pkg_a, 1, True, seq=1),
            ToolExecution(
                2,
                "Bash",
                "go test ./pkg/api",
                "ok  \texample.com/m/pkg/api\t0.005s\n",
                0,
                False,
                seq=2,
            ),
        ],
    )
    _, r_trajs, _ = build_attempt_ledger(resolved_tx)
    assert r_trajs[0].status == "RESOLVED"


def test_edit_thrash_hunk_revert_write_edit_write_revert_and_no_stub_false_positive() -> None:
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
        # Write -> Edit -> Write restoring original write content must be detected as revert!
        FileEdit(1, "src/cfg.py", "write", new_text="MODE = 'a'\n", seq=4),
        FileEdit(2, "src/cfg.py", "edit", old_text="MODE = 'a'", new_text="MODE = 'b'", seq=5),
        FileEdit(3, "src/cfg.py", "write", new_text="MODE = 'a'\n", seq=6),
        # Unrelated tiny `pass` stubs across two different functions must NOT false-positive!
        FileEdit(1, "src/stubs.py", "edit", old_text="pass", new_text="return 1", seq=8),
        FileEdit(2, "src/stubs.py", "edit", old_text="return 2", new_text="pass", seq=9),
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

    assert "src/cfg.py" in by_path
    assert by_path["src/cfg.py"].reverted is True
    assert by_path["src/cfg.py"].edit_count == 3

    assert "src/stubs.py" not in by_path


def test_edit_thrash_stalled_with_line_drift_and_intervening_noise_failures() -> None:
    edits = [
        FileEdit(1, "src/parser.py", "edit", old_text="x = 1", new_text="x = 2", seq=1),
        FileEdit(2, "src/parser.py", "edit", old_text="x = 2", new_text="x = 3", seq=4),
    ]
    tools = [
        ToolExecution(
            1,
            "Bash",
            "pytest tests/test_parser.py",
            _pytest_fail_output("tests/test_parser.py::test_parse", "400", "100", lineno=10),
            1,
            True,
            seq=2,
        ),
        # Intervening unrelated failing build command must NOT mask the stalled test!
        ToolExecution(1, "Bash", "mypy src/", "src/other.py:1: error: bad type\n", 1, True, seq=3),
        ToolExecution(
            2,
            "Bash",
            "pytest tests/test_parser.py",
            # Assertion line shifted from 10 to 14 -> still STALLED!
            _pytest_fail_output("tests/test_parser.py::test_parse", "400", "100", lineno=14),
            1,
            True,
            seq=5,
        ),
        # Unrelated passing test file must NOT suppress the thrash warning!
        ToolExecution(
            3, "Bash", "pytest tests/test_other.py", "=== 4 passed in 0.05s ===\n", 0, False, seq=6
        ),
    ]

    thrash = detect_edit_thrash(edits, tools)
    assert len(thrash) == 1
    assert thrash[0].path == "src/parser.py"
    assert thrash[0].reverted is False
    assert thrash[0].edit_count == 2
    assert "stalled" in thrash[0].reason.lower()


def test_edit_thrash_no_false_positive_with_grep_misses_and_final_pass() -> None:
    # 3 incremental edits + 2 `grep -q` misses (exit 1) + final full passing suite -> NO thrash!
    edits = [
        FileEdit(1, "src/a.py", "edit", old_text="a = 1", new_text="a = 2", seq=1),
        FileEdit(1, "src/a.py", "edit", old_text="b = 1", new_text="b = 2", seq=3),
        FileEdit(2, "src/a.py", "edit", old_text="c = 1", new_text="c = 2", seq=5),
    ]
    tools = [
        ToolExecution(1, "Bash", "grep -q TODO src/a.py", "", 1, True, seq=2),
        ToolExecution(1, "Bash", "grep -q FIXME src/a.py", "", 1, True, seq=4),
        ToolExecution(2, "Bash", "pytest", "=== 10 passed in 0.10s ===\n", 0, False, seq=6),
    ]
    assert detect_edit_thrash(edits, tools) == []


def test_ledger_scales_fast_for_1000_failing_tests_and_200_scoped_passes() -> None:
    attempts = [
        TestAttempt(
            f"tests/test_mod_{i % 50}.py::test_case_{i}",
            "pytest",
            f"tests/test_mod_{i % 50}.py:10",
            "1",
            "2",
            "failed",
            1,
        )
        for i in range(1000)
    ]
    scoped_passes = [
        ToolExecution(
            2,
            "Bash",
            f"pytest tests/test_other_{j}.py",
            "=== 2 passed in 0.01s ===\n",
            0,
            False,
            seq=j + 1,
        )
        for j in range(200)
    ]

    t0 = time.perf_counter()
    trajs = build_trajectories(attempts, scoped_passes)
    elapsed = time.perf_counter() - t0

    assert len(trajs) == 1000
    assert all(t.status == "SINGLE_FAILURE" for t in trajs)
    assert elapsed < 0.25, f"Ledger scaling regressed: {elapsed:.3f}s for 1000x200"


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
                exit_code=None,
                is_error=False,
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
