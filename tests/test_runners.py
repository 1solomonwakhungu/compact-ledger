"""Unit and adversarial tests for compact_ledger.parsers.runners."""

from __future__ import annotations

import time

from compact_ledger.models import ToolExecution
from compact_ledger.parsers.runners import (
    detect_runner,
    extract_go_failed_packages,
    extract_pass_signal,
    normalize_command,
    parse_runner_output,
    parse_tool_executions,
    strip_ansi,
)


def test_strip_ansi_and_normalize_command() -> None:
    raw = "\x1b[31mFAILED\x1b[0m \x1b]8;;https://example.com\x07link\x1b]8;;\x07\r\nline2\rline3\ud83d"
    cleaned = strip_ansi(raw)
    assert "\x1b" not in cleaned
    assert cleaned == "FAILED link\nline2\nline3\ufffd"
    assert normalize_command("  uv   run\t\x1b[1mpytest\x1b[0m  -q  ") == "uv run pytest -q"


def test_detect_runner_commands_and_avoid_false_positives() -> None:
    assert detect_runner("uv run pytest -q tests/test_a.py", "") == "pytest"
    assert detect_runner("PYTHONPATH=src python3 -m pytest", "") == "pytest"
    assert detect_runner("cd repo && poetry run py.test -v", "") == "pytest"
    assert detect_runner("pnpm vitest run src/a.test.ts", "") == "vitest"
    assert detect_runner("npx jest --runInBand", "") == "jest"
    assert detect_runner("go test -v ./...", "") == "go"

    # Non-test commands mentioning runner names must NOT misclassify as test runners.
    assert detect_runner('git commit -m "fix pytest and vitest parsing"', "") == "generic"
    assert detect_runner("rg pytest src/", "") == "generic"
    assert detect_runner("echo 'go test ./...'", "") == "generic"

    # Wrapper scripts fall back cleanly to output heuristics.
    assert detect_runner("npm test", "Test Suites: 1 failed, 1 total\n  ● Math › adds\n") == "jest"
    assert detect_runner("npm test", " RUN  v2.1.0\n Test Files  1 passed (1)\n") == "vitest"
    assert (
        detect_runner("make test", "=== test session starts ===\n1 failed in 0.12s\n") == "pytest"
    )
    assert (
        detect_runner("make test", "--- FAIL: TestAuth (0.00s)\nFAIL\texample.com/auth\n") == "go"
    )


def test_parse_pytest_assert_parametrized_assertion_error_and_raises() -> None:
    output = """\
============================= test session starts ==============================
collected 4 items

tests/test_pricing.py::test_ok PASSED                                    [ 25%]
tests/test_pricing.py::test_discount[tier1] FAILED                       [ 50%]
tests/test_pricing.py::test_custom_msg FAILED                            [ 75%]
tests/test_pricing.py::test_missing_exc FAILED                           [100%]

=================================== FAILURES ===================================
___________________________ test_discount[tier1] _______________________________

    @pytest.mark.parametrize("tier", ["tier1"])
    def test_discount(tier):
>       assert compute_discount(tier) == 100
E       assert 400 == 100
E        +  where 400 = compute_discount('tier1')

tests/test_pricing.py:42: AssertionError
_______________________________ test_custom_msg ________________________________

    def test_custom_msg():
>       raise AssertionError("expected 100, got 400")
E       AssertionError: expected 100, got 400

tests/test_pricing.py:55: AssertionError
_______________________________ test_missing_exc _______________________________

    def test_missing_exc():
        with pytest.raises(ValueError):
>           parse_age("25")
E           Failed: DID NOT RAISE <class 'ValueError'>

tests/test_pricing.py:68: Failed
=========================== short test summary info ============================
FAILED tests/test_pricing.py::test_discount[tier1] - assert 400 == 100
FAILED tests/test_pricing.py::test_custom_msg - AssertionError: expected 100, got 400
FAILED tests/test_pricing.py::test_missing_exc - Failed: DID NOT RAISE <class 'ValueError'>
========================= 3 failed, 1 passed in 0.14s ==========================
"""
    exe = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="uv run pytest -v tests/test_pricing.py",
        output=output,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe)
    by_id = {a.test_id: a for a in attempts}

    assert set(by_id) == {
        "tests/test_pricing.py::test_discount[tier1]",
        "tests/test_pricing.py::test_custom_msg",
        "tests/test_pricing.py::test_missing_exc",
        "tests/test_pricing.py::test_ok",
    }

    disc = by_id["tests/test_pricing.py::test_discount[tier1]"]
    assert disc.runner == "pytest"
    assert disc.outcome == "failed"
    assert disc.assertion_loc == "tests/test_pricing.py:42"
    assert disc.actual == "400"
    assert disc.expected == "100"
    assert disc.message == "assert 400 == 100"
    assert disc.turn_index == 2

    custom = by_id["tests/test_pricing.py::test_custom_msg"]
    assert custom.assertion_loc == "tests/test_pricing.py:55"
    assert custom.expected == "100"
    assert custom.actual == "400"

    raises = by_id["tests/test_pricing.py::test_missing_exc"]
    assert raises.assertion_loc == "tests/test_pricing.py:68"
    assert raises.actual == "DID NOT RAISE"
    assert raises.expected == "raises <class 'ValueError'>"

    ok = by_id["tests/test_pricing.py::test_ok"]
    assert ok.outcome == "passed"


def test_parse_pytest_short_summary_only_and_collection_error() -> None:
    output = """\
FAILED tests/test_api.py::test_timeout - AssertionError: assert 500 == 200
ERROR tests/test_api.py::test_fixture - RuntimeError: db refused connection
ERROR collecting tests/test_broken.py
"""
    exe = ToolExecution(
        turn_index=3,
        tool_name="Bash",
        command="pytest -q --tb=no",
        output=output,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe)
    by_id = {a.test_id: a for a in attempts}

    assert by_id["tests/test_api.py::test_timeout"].actual == "500"
    assert by_id["tests/test_api.py::test_timeout"].expected == "200"
    assert by_id["tests/test_api.py::test_fixture"].outcome == "error"
    assert "db refused connection" in (by_id["tests/test_api.py::test_fixture"].message or "")
    assert by_id["tests/test_broken.py"].outcome == "error"


def test_parse_pytest_pass_signal_vs_zero_tests() -> None:
    clean_pass = ToolExecution(
        turn_index=4,
        tool_name="Bash",
        command="pytest -q",
        output="............\n12 passed in 0.25s\n",
        exit_code=0,
    )
    # Without `===` bar, pytest -q still prints `12 passed in 0.25s` -> let's test both formats.
    clean_bar = ToolExecution(
        turn_index=4,
        tool_name="Bash",
        command="pytest",
        output="======================= 12 passed in 0.25s =======================\n",
        exit_code=0,
    )
    sig = extract_pass_signal(clean_bar)
    assert sig is not None
    assert sig.runner == "pytest"
    assert sig.passed_count == 12
    assert sig.failed_count == 0

    no_tests = ToolExecution(
        turn_index=4,
        tool_name="Bash",
        command="pytest -k nonexistent",
        output="====================== no tests ran in 0.01s ======================\n",
        exit_code=5,
        is_error=True,
    )
    assert extract_pass_signal(no_tests) is None
    # Verify non-failing exit_code=0 with 0 passed also returns None
    zero_pass = ToolExecution(
        turn_index=4,
        tool_name="Bash",
        command="pytest",
        output="====================== 0 passed in 0.01s ======================\n",
        exit_code=0,
    )
    assert extract_pass_signal(zero_pass) is None
    quiet_sig = extract_pass_signal(clean_pass)
    assert quiet_sig is not None
    assert quiet_sig.passed_count == 12


def test_parse_jest_with_ansi_and_stack_frames() -> None:
    output = (
        "\x1b[41m\x1b[30m FAIL \x1b[0m src/math.test.ts\n"
        "  \x1b[31m● Calculator › computes discount\x1b[0m\n"
        "\n"
        "    expect(received).toBe(expected) // Object.is equality\n"
        "\n"
        "    Expected: \x1b[32m100\x1b[0m\n"
        "    Received: \x1b[31m400\x1b[0m\n"
        "\n"
        "      at node_modules/jest-circus/build/run.js:10:5\n"
        "      at Object.<anonymous> (src/math.test.ts:42:18)\n"
        "\n"
        "  \x1b[31m● Calculator › formats label\x1b[0m\n"
        "\n"
        '    Expected substring: "USD"\n'
        '    Received string:    "EUR 10"\n'
        "      at Object.<anonymous> (src/math.test.ts:58:12)\n"
        "\n"
        "Test Suites: 1 failed, 1 total\n"
        "Tests:       2 failed, 1 passed, 3 total\n"
    )
    exe = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="npx jest src/math.test.ts",
        output=output,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe)
    assert len(attempts) == 2
    first, second = attempts

    assert first.runner == "jest"
    assert first.test_id == "src/math.test.ts > Calculator > computes discount"
    assert first.expected == "100"
    assert first.actual == "400"
    assert first.assertion_loc == "src/math.test.ts:42"
    assert first.outcome == "failed"

    assert second.test_id == "src/math.test.ts > Calculator > formats label"
    assert second.expected == '"USD"'
    assert second.actual == '"EUR 10"'
    assert second.assertion_loc == "src/math.test.ts:58"


def test_parse_vitest_failure_and_pass_signals() -> None:
    fail_out = (
        "\x1b[36m RUN \x1b[0m v2.1.1 /repo\n"
        "\n"
        " \x1b[31m×\x1b[0m src/calc.test.ts > Calculator > applies coupon 8ms\n"
        " \x1b[32m✓\x1b[0m src/calc.test.ts > Calculator > handles zero 2ms\n"
        "\n"
        "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯\n"
        "\n"
        " FAIL  src/calc.test.ts > Calculator > applies coupon\n"
        "AssertionError: expected 400 to be 100 // Object.is equality\n"
        "\n"
        "- Expected\n"
        "+ Received\n"
        "\n"
        "- 100\n"
        "+ 400\n"
        "\n"
        " ❯ src/calc.test.ts:27:23\n"
        "\n"
        " Test Files  1 failed (1)\n"
        "      Tests  1 failed | 1 passed (2)\n"
    )
    exe_fail = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="pnpm vitest run src/calc.test.ts",
        output=fail_out,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe_fail)
    by_id = {a.test_id: a for a in attempts}

    failed = by_id["src/calc.test.ts > Calculator > applies coupon"]
    assert failed.runner == "vitest"
    assert failed.outcome == "failed"
    assert failed.actual == "400"
    assert failed.expected == "100"
    assert failed.assertion_loc == "src/calc.test.ts:27"

    passed = by_id["src/calc.test.ts > Calculator > handles zero"]
    assert passed.outcome == "passed"

    pass_out = (
        " RUN  v2.1.1 /repo\n"
        " ✓ src/calc.test.ts (2 tests) 6ms\n"
        " Test Files  1 passed (1)\n"
        "      Tests  2 passed (2)\n"
    )
    exe_pass = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="pnpm vitest run src/calc.test.ts",
        output=pass_out,
        exit_code=0,
    )
    sig = extract_pass_signal(exe_pass)
    assert sig is not None
    assert sig.runner == "vitest"
    assert sig.passed_files == ["src/calc.test.ts"]
    assert sig.passed_count == 2


def test_parse_go_test_non_verbose_and_verbose_subtests() -> None:
    non_verbose = """\
--- FAIL: TestCalc (0.00s)
    --- FAIL: TestCalc/tier1 (0.00s)
        calc_test.go:42: got 400, want 100
    --- FAIL: TestCalc/tier2 (0.00s)
        calc_test.go:51: want 250, got 300
FAIL
FAIL\texample.com/pkg/calc\t0.015s
"""
    exe1 = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="go test ./...",
        output=non_verbose,
        exit_code=1,
        is_error=True,
    )
    attempts1 = parse_runner_output(exe1)
    # Parent TestCalc had no direct error lines so only the two subtests are emitted.
    assert [a.test_id for a in attempts1] == ["TestCalc/tier1", "TestCalc/tier2"]
    assert attempts1[0].assertion_loc == "calc_test.go:42"
    assert attempts1[0].actual == "400"
    assert attempts1[0].expected == "100"
    assert attempts1[1].assertion_loc == "calc_test.go:51"
    assert attempts1[1].actual == "300"
    assert attempts1[1].expected == "250"

    pkg_map = extract_go_failed_packages(non_verbose)
    assert pkg_map["TestCalc/tier1"] == "example.com/pkg/calc"

    verbose = """\
=== RUN   TestCalc
=== RUN   TestCalc/tier1
    calc_test.go:42: expected 100, got 200
=== RUN   TestCalc/tier2
--- FAIL: TestCalc (0.00s)
    --- FAIL: TestCalc/tier1 (0.00s)
    --- PASS: TestCalc/tier2 (0.00s)
FAIL
FAIL\texample.com/pkg/calc\t0.011s
"""
    exe2 = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="go test -v ./pkg/calc",
        output=verbose,
        exit_code=1,
        is_error=True,
    )
    attempts2 = parse_runner_output(exe2)
    by_id = {a.test_id: a for a in attempts2}
    assert by_id["TestCalc/tier1"].outcome == "failed"
    assert by_id["TestCalc/tier1"].actual == "200"
    assert by_id["TestCalc/tier1"].expected == "100"
    assert by_id["TestCalc/tier1"].assertion_loc == "calc_test.go:42"
    assert by_id["TestCalc/tier2"].outcome == "passed"


def test_parse_go_testify_and_pass_signal() -> None:
    testify_out = """\
=== RUN   TestInvoice
    invoice_test.go:88:
        \tError Trace:\tinvoice_test.go:88
        \tError:      \tNot equal:
        \t            \texpected: 100
        \t            \tactual  : 400
--- FAIL: TestInvoice (0.00s)
FAIL
FAIL\texample.com/pkg/billing\t0.009s
"""
    exe = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="go test ./pkg/billing",
        output=testify_out,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe)
    assert len(attempts) == 1
    assert attempts[0].test_id == "TestInvoice"
    assert attempts[0].expected == "100"
    assert attempts[0].actual == "400"
    assert attempts[0].assertion_loc == "invoice_test.go:88"

    pass_exe = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="go test ./pkg/billing",
        output="ok  \texample.com/pkg/billing\t0.007s\n",
        exit_code=0,
    )
    sig = extract_pass_signal(pass_exe)
    assert sig is not None
    assert sig.runner == "go"
    assert sig.passed_packages == ["example.com/pkg/billing"]


def test_parse_generic_command_fail_then_pass_and_ignore_non_shell_tools() -> None:
    executions = [
        # Unrelated passing shell command should NOT emit any attempt.
        ToolExecution(
            turn_index=1, tool_name="Bash", command="git status", output="clean", exit_code=0
        ),
        # Non-shell tool error (e.g. Read/Edit) without command should NOT emit a TestAttempt.
        ToolExecution(
            turn_index=1, tool_name="Read", command=None, output="File not found", is_error=True
        ),
        # Generic failing command.
        ToolExecution(
            turn_index=2,
            tool_name="Bash",
            command="mypy  src/",
            output="\x1b[31msrc/app.py:12: error: Incompatible return value type\x1b[0m\n\n---\n",
            exit_code=1,
            is_error=True,
        ),
        # Different passing command must NOT resolve `mypy src/`.
        ToolExecution(
            turn_index=3,
            tool_name="Bash",
            command="ruff check .",
            output="All checks passed!",
            exit_code=0,
        ),
        # Exact same normalized command later passes -> emits `passed`.
        ToolExecution(
            turn_index=4,
            tool_name="Bash",
            command="mypy src/",
            output="Success: no issues found",
            exit_code=0,
        ),
    ]
    attempts = parse_tool_executions(executions)
    assert len(attempts) == 2
    assert attempts[0].runner == "generic"
    assert attempts[0].test_id == "mypy src/"
    assert attempts[0].outcome == "failed"
    assert "exit 1:" in (attempts[0].message or "")
    assert "Incompatible return value type" in (attempts[0].message or "")

    assert attempts[1].runner == "generic"
    assert attempts[1].test_id == "mypy src/"
    assert attempts[1].outcome == "passed"
    assert attempts[1].turn_index == 4


def test_adversarial_huge_and_malformed_outputs_are_redos_safe() -> None:
    # Pathological inputs designed to trigger catastrophic backtracking in naive regexes.
    huge_ansi = ("\x1b[31;1m" + "a" * 100 + "\x1b[0m") * 3000
    huge_single_line = "E       assert " + ("1 == " * 100_000) + "2"
    malformed_osc = "\x1b]8;;" + ("x" * 200_000)  # unterminated OSC sequence
    pathological = "\n".join(
        [
            "=== FAILURES ===",
            "_" * 10 + " test_huge " + "_" * 10,
            huge_single_line,
            huge_ansi,
            malformed_osc,
            "tests/test_huge.py:10: AssertionError",
            "FAILED tests/test_huge.py::test_huge - assert 1 == 2",
        ]
    )
    exe = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="pytest tests/test_huge.py",
        output=pathological,
        exit_code=1,
        is_error=True,
    )
    t0 = time.perf_counter()
    attempts = parse_runner_output(exe)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.25, f"Parsing took too long ({elapsed:.3f}s) — possible ReDoS"
    assert len(attempts) == 1
    assert attempts[0].test_id == "tests/test_huge.py::test_huge"
    assert len(attempts[0].message or "") <= 240
