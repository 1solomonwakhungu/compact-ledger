"""Unit and adversarial tests for compact_ledger.parsers.runners."""

from __future__ import annotations

import time

from compact_ledger.models import ToolExecution
from compact_ledger.parsers.runners import (
    detect_runner,
    extract_go_failed_packages,
    extract_pass_signal,
    normalize_command,
    parse_runner_invocation,
    parse_runner_output,
    parse_tool_executions,
    strip_ansi,
)


def test_strip_ansi_including_charset_and_decsc_and_normalize_command() -> None:
    raw = (
        "\x1b[31mFAILED\x1b[0m \x1b(B\x1b7ok\x1b8 "
        "\x1b]8;;https://example.com\x07link\x1b]8;;\x07\r\nline2\rline3\ud83d"
    )
    cleaned = strip_ansi(raw)
    assert "\x1b" not in cleaned
    assert cleaned == "FAILED ok link\nline2\nline3\ufffd"
    assert normalize_command("  uv   run\t\x1b[1mpytest\x1b[0m  -q  ") == "uv run pytest -q"


def test_detect_runner_commands_and_avoid_false_positives() -> None:
    assert detect_runner("uv run pytest -q tests/test_a.py", "") == "pytest"
    assert detect_runner("PYTHONPATH=src python3 -X dev -m pytest", "") == "pytest"
    assert detect_runner("cd repo && poetry run py.test -v", "") == "pytest"
    assert detect_runner("timeout -s KILL 60 pytest tests/test_b.py", "") == "pytest"
    assert detect_runner("nice -n 10 pytest tests/test_b.py", "") == "pytest"
    assert detect_runner("pnpm vitest run src/a.test.ts", "") == "vitest"
    assert detect_runner("npx jest --runInBand", "") == "jest"
    assert detect_runner("go test -v ./...", "") == "go"

    # Non-test / read-only / system commands must NEVER misclassify as test runners.
    assert detect_runner('git commit -m "fix pytest and vitest parsing"', "") == "generic"
    assert detect_runner("rg pytest src/", "") == "generic"
    assert detect_runner("echo 'go test ./...'", "") == "generic"
    assert (
        detect_runner("cat docs/example.txt", "=== FAILURES ===\nFAILED a.py::test_x\n")
        == "generic"
    )
    assert detect_runner("systemctl status nginx", "● nginx.service - web server\n") == "generic"
    assert detect_runner("cargo run", "ERROR app::db: connection refused\n") == "generic"
    assert detect_runner("./scripts/lint.sh", "ok  done\n") == "generic"
    assert detect_runner("node --test", "ok 1 - adds\n") == "generic"
    assert detect_runner("./scripts/check.sh", "FAIL check_schema\n") == "generic"

    # Wrapper scripts fall back cleanly to output heuristics.
    assert detect_runner("npm test", "Test Suites: 1 failed, 1 total\n  ● Math › adds\n") == "jest"
    assert detect_runner("npm test", " RUN  v2.1.0\n Test Files  1 passed (1)\n") == "vitest"
    assert (
        detect_runner("make test", "=== test session starts ===\n1 failed in 0.12s\n") == "pytest"
    )
    assert (
        detect_runner("make test", "--- FAIL: TestAuth (0.00s)\nFAIL\texample.com/auth\t0.01s\n")
        == "go"
    )

    # Long wrapper output (>400 lines) with summary at the very end is still detected.
    long_log = ("build line\n" * 600) + "=== 12 passed in 0.25s ===\n"
    assert detect_runner("make test", long_log) == "pytest"


def test_parse_runner_invocation_strips_redirections_and_flags_chdir() -> None:
    inv1 = parse_runner_invocation("pytest 2>&1 | tail -30")
    assert inv1.runner == "pytest"
    assert inv1.args == []
    assert inv1.scope_known is True

    inv2 = parse_runner_invocation("pytest > /tmp/o.log 2>&1; tail -20 /tmp/o.log")
    assert inv2.runner == "pytest"
    assert inv2.args == []
    assert inv2.scope_known is True

    inv3 = parse_runner_invocation("cd services/other && pytest")
    assert inv3.runner == "pytest"
    assert inv3.has_chdir is True
    assert inv3.scope_known is False

    inv4 = parse_runner_invocation("coverage run -m pytest tests/test_b.py")
    assert inv4.runner is None
    assert inv4.scope_known is False


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


def test_parse_pytest_cross_file_same_name_and_dash_in_param_id() -> None:
    output = """\
==================================== ERRORS ====================================
_________________________ ERROR at setup of test_x _____________________________

    def setup():
>       raise RuntimeError("no db")
E       RuntimeError: no db

tests/test_b.py:5: RuntimeError
=================================== FAILURES ===================================
___________________________________ test_x _____________________________________

    def test_x():
>       assert 400 == 100
E       assert 400 == 100

tests/test_a.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_x - assert 400 == 100
ERROR tests/test_b.py::test_x - RuntimeError: no db
FAILED tests/test_p.py::test_expr[a - b] - assert 1 == 2
FAILED tests/test_p.py::test_expr[a - c] - assert 3 == 4
"""
    exe = ToolExecution(1, "Bash", "pytest", output, 1, True)
    by_id = {a.test_id: a for a in parse_runner_output(exe)}

    # Cross-file details must NOT swap between test_a.py::test_x and test_b.py::test_x.
    assert by_id["tests/test_a.py::test_x"].assertion_loc == "tests/test_a.py:12"
    assert by_id["tests/test_a.py::test_x"].actual == "400"
    assert by_id["tests/test_b.py::test_x"].assertion_loc == "tests/test_b.py:5"
    assert by_id["tests/test_b.py::test_x"].outcome == "error"
    assert "no db" in (by_id["tests/test_b.py::test_x"].message or "")

    # Parametrized IDs containing ` - ` must remain intact and distinct.
    assert "tests/test_p.py::test_expr[a - b]" in by_id
    assert "tests/test_p.py::test_expr[a - c]" in by_id
    assert by_id["tests/test_p.py::test_expr[a - b]"].actual == "1"
    assert by_id["tests/test_p.py::test_expr[a - c]"].actual == "3"


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


def test_parse_jest_with_ansi_stack_frames_and_no_phantom_deprecation_warning() -> None:
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

    # Passing Jest run with `● Deprecation Warning:` must emit zero failure attempts.
    warn_pass = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="npx jest",
        output=(
            "● Deprecation Warning:\n"
            "  Option setupTestFrameworkScriptFile was replaced.\n"
            "PASS src/math.test.ts\n"
            "Test Suites: 1 passed, 1 total\n"
            "Tests:       3 passed, 3 total\n"
        ),
        exit_code=0,
        is_error=False,
    )
    assert [a for a in parse_runner_output(warn_pass) if a.outcome != "passed"] == []


def test_parse_vitest_failure_suite_error_and_pass_signals() -> None:
    fail_out = (
        "\x1b[36m RUN \x1b[0m v2.1.1 /repo\n"
        "\n"
        " \x1b[31m×\x1b[0m src/calc.test.ts > Calculator > applies coupon 8ms\n"
        " \x1b[32m✓\x1b[0m src/calc.test.ts > Calculator > handles zero 2ms\n"
        " \x1b[32m✓\x1b[0m parses config.ts 1ms\n"
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
        "      Tests  1 failed | 2 passed (3)\n"
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
    # Test name ending in `.ts` must NOT be mistaken for a test file header.
    assert "parses config.ts" in by_id

    # Suite-level transform error must NOT be dropped.
    suite_err = ToolExecution(
        turn_index=2,
        tool_name="Bash",
        command="pnpm vitest run src/broken.test.ts",
        output=(
            " RUN  v2.1.1 /repo\n"
            " FAIL  src/broken.test.ts [ src/broken.test.ts ]\n"
            "SyntaxError: Unexpected token\n"
            " Test Files  1 failed (1)\n"
        ),
        exit_code=1,
        is_error=True,
    )
    suite_attempts = parse_runner_output(suite_err)
    assert len(suite_attempts) == 1
    assert suite_attempts[0].test_id == "src/broken.test.ts"
    assert "Unexpected token" in (suite_attempts[0].message or "")

    pass_out = (
        " RUN  v2.1.1 /repo\n"
        " ✓ src/calc.test.ts (2 tests) 6ms\n"
        " Test Files  1 passed (1)\n"
        "      Tests  2 passed (2)\n"
    )
    exe_pass = ToolExecution(
        turn_index=3,
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


def test_parse_go_test_qualified_ids_parallel_cont_and_testify() -> None:
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
    assert [a.test_id for a in attempts1] == [
        "example.com/pkg/calc::TestCalc/tier1",
        "example.com/pkg/calc::TestCalc/tier2",
    ]
    assert attempts1[0].assertion_loc == "calc_test.go:42"
    assert attempts1[0].actual == "400"
    assert attempts1[0].expected == "100"
    assert attempts1[1].assertion_loc == "calc_test.go:51"
    assert attempts1[1].actual == "300"
    assert attempts1[1].expected == "250"

    pkg_map = extract_go_failed_packages(non_verbose)
    assert pkg_map["TestCalc/tier1"] == "example.com/pkg/calc"
    assert pkg_map["example.com/pkg/calc::TestCalc/tier1"] == "example.com/pkg/calc"

    # Parallel `=== CONT` log attribution + verbose pass.
    parallel_verbose = """\
=== RUN   TestA
=== PAUSE TestA
=== RUN   TestB
=== PAUSE TestB
=== CONT  TestA
    a_test.go:10: got 1, want 2
=== CONT  TestB
--- FAIL: TestA (0.00s)
--- PASS: TestB (0.00s)
FAIL
FAIL\texample.com/pkg/par\t0.011s
"""
    exe2 = ToolExecution(2, "Bash", "go test -v ./pkg/par", parallel_verbose, 1, True)
    by_id2 = {a.test_id: a for a in parse_runner_output(exe2)}
    assert by_id2["example.com/pkg/par::TestA"].actual == "1"
    assert by_id2["example.com/pkg/par::TestA"].expected == "2"
    assert by_id2["example.com/pkg/par::TestB"].outcome == "passed"

    # Testify multiline output + `ok ... [no tests to run]` exclusion.
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
    exe3 = ToolExecution(1, "Bash", "go test ./pkg/billing", testify_out, 1, True)
    attempts3 = parse_runner_output(exe3)
    assert len(attempts3) == 1
    assert attempts3[0].test_id == "example.com/pkg/billing::TestInvoice"
    assert attempts3[0].expected == "100"
    assert attempts3[0].actual == "400"

    no_tests_run = ToolExecution(
        2,
        "Bash",
        "go test ./pkg/billing",
        "ok  \texample.com/pkg/billing\t0.002s [no tests to run]\n",
        0,
        False,
    )
    assert extract_pass_signal(no_tests_run) is None


def test_failing_specialized_runner_without_test_lines_falls_back_cleanly() -> None:
    pytest_err = ToolExecution(
        1,
        "Bash",
        "pytest tests/nope.py",
        "ERROR: file or directory not found: tests/nope.py\n",
        4,
        True,
    )
    p_atts = parse_runner_output(pytest_err)
    assert len(p_atts) == 1
    assert p_atts[0].runner == "pytest"
    assert p_atts[0].outcome == "error"
    assert "not found" in (p_atts[0].message or "")

    go_panic = ToolExecution(
        2,
        "Bash",
        "go test ./pkg/a",
        "panic: test timed out after 10m0s\n",
        2,
        True,
    )
    g_atts = parse_runner_output(go_panic)
    assert len(g_atts) == 1
    assert g_atts[0].runner == "go"
    assert g_atts[0].outcome == "error"
    assert "timed out" in (g_atts[0].message or "")


def test_parse_generic_claude_exit_none_and_ignore_probes_and_bound_heredoc() -> None:
    heredoc_cmd = "python3 - <<'EOF'\n" + ("print('x')\n" * 400) + "EOF"
    executions = [
        ToolExecution(1, "Bash", "git status", "clean", 0),
        ToolExecution(1, "Read", None, "File not found", is_error=True),
        # Probe commands exiting non-zero must NOT emit generic test failures.
        ToolExecution(1, "Bash", "grep -rn foo src/", "", 1, True),
        ToolExecution(1, "Bash", "test -f x.lock", "", 1, True),
        ToolExecution(1, "Bash", "ls build/", "No such file", 2, True),
        ToolExecution(1, "Bash", "git diff --exit-code", "diff --git ...", 1, True),
        # Real failing generic build command.
        ToolExecution(
            2,
            "Bash",
            "make  build",
            "\x1b[31msrc/app.py:12: error: Incompatible return value type\x1b[0m\n\n---\n",
            2,
            True,
        ),
        # Claude Code successful Bash result (exit_code=None, is_error=False) resolves it!
        ToolExecution(3, "Bash", "make build", "Build succeeded", exit_code=None, is_error=False),
        # Huge heredoc failing command gets a bounded test_id.
        ToolExecution(4, "Bash", heredoc_cmd, "RuntimeError: boom", 1, True),
    ]
    attempts = parse_tool_executions(executions)
    assert len(attempts) == 3

    assert attempts[0].runner == "generic"
    assert attempts[0].test_id == "make build"
    assert attempts[0].outcome == "failed"

    assert attempts[1].runner == "generic"
    assert attempts[1].test_id == "make build"
    assert attempts[1].outcome == "passed"

    assert attempts[2].runner == "generic"
    assert len(attempts[2].test_id) <= 120


def test_adversarial_redos_whitespace_and_huge_outputs_complete_in_milliseconds() -> None:
    # Directly exercise every formerly-backtracking pattern with 2KB whitespace bombs + huge inputs.
    ws_bomb = " " * 2030
    pathological_pytest = "\n".join(
        [
            "=== FAILURES ===",
            f"___{ws_bomb}x",
            "___ test_huge ___",
            f"E       expected{ws_bomb}x",
            f"E       got{ws_bomb}x",
            f"E       want{ws_bomb}x",
            "E       assert " + ("1 == " * 20_000) + "2",
            "a::" * 1000 + "b",
            ("\x1b[31;1m" + "a" * 100 + "\x1b[0m") * 2000,
            "\x1b]8;;" + ("x" * 100_000),
            "tests/test_huge.py:10: AssertionError",
            "FAILED tests/test_huge.py::test_huge - assert 1 == 2",
        ]
    )
    pathological_vitest = "\n".join(
        [
            " RUN  v2.1.0",
            " FAIL  src/a.test.ts > Suite > bomb",
            f"AssertionError: expected{ws_bomb}x",
            f" × {'a ' * 1000}x",
            " ❯ src/a.test.ts:10:5",
        ]
    )
    pathological_go = "\n".join(
        [
            "--- FAIL: TestBomb (0.00s)",
            f"    bomb_test.go:10: expected{ws_bomb}x",
            f"    bomb_test.go:11: got{ws_bomb}x",
            f"    bomb_test.go:12: want{ws_bomb}x",
            "FAIL\texample.com/bomb\t0.01s",
        ]
    )

    t0 = time.perf_counter()
    p_atts = parse_runner_output(
        ToolExecution(1, "Bash", "pytest tests/test_huge.py", pathological_pytest, 1, True)
    )
    v_atts = parse_runner_output(
        ToolExecution(1, "Bash", "vitest run src/a.test.ts", pathological_vitest, 1, True)
    )
    g_atts = parse_runner_output(
        ToolExecution(1, "Bash", "go test ./...", pathological_go, 1, True)
    )
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, f"Adversarial parsing took {elapsed:.3f}s — possible ReDoS"
    assert any(a.test_id == "tests/test_huge.py::test_huge" for a in p_atts)
    assert len(v_atts) == 1
    assert len(g_atts) == 1


def test_round2_regressions_n3_n6_n7_and_non_shell_tools() -> None:
    # N3: Probe command before real runner must NOT drop failing runs, nor `hatch run test`.
    for cmd in (
        "echo 'running' && npm test",
        "pwd; npm test",
        "ls && npm test",
        "[ -f .env ] && npm test",
        "which node && npm test",
        "hatch run test",
        "pdm run test",
        "echo start\nnpx jest",
    ):
        exe = ToolExecution(
            1,
            "Bash",
            cmd,
            (
                "FAIL src/a.test.js\n  ● adds\n    Expected: 2\n"
                "    Received: 3\nTest Suites: 1 failed, 1 total\n"
            ),
            1,
            True,
        )
        atts = parse_runner_output(exe)
        assert len(atts) == 1, f"Dropped failure for {cmd!r}"
        assert atts[0].outcome == "failed"

    # `git commit` pre-commit hook failure must NOT be dropped.
    commit_fail = ToolExecution(1, "Bash", "git commit -m 'msg'", "ruff failed\n", 1, True)
    assert len(parse_runner_output(commit_fail)) == 1

    # Non-shell Read/Agent results containing test output must NEVER produce attempts/signals.
    read_pass = ToolExecution(
        2, "Read", None, "tests/test_a.py::test_x PASSED\n=== 1 passed in 0.01s ===\n", None, False
    )
    assert parse_runner_output(read_pass) == []
    assert extract_pass_signal(read_pass) is None

    # N6: Passing Claude Code Jest run (exit_code=None) with open-handle warnings emits NO fail.
    jest_claude_pass = ToolExecution(
        2,
        "Bash",
        "npx jest",
        (
            "PASS src/a.test.js\n"
            "  ● Cannot log after tests are done. Did you forget to wait for something async?\n"
            "  ●  TCPSERVERWRAP\n"
            "Test Suites: 1 passed, 1 total\n"
            "Tests:       2 passed, 2 total\n"
        ),
        exit_code=None,
        is_error=False,
    )
    assert [a for a in parse_runner_output(jest_claude_pass) if a.outcome != "passed"] == []

    # N7: Former quadratic patterns (`❯` × 2040 and `✓ a` + 2030 spaces + `b`) run in milliseconds.
    chevron_bomb = "\n".join(
        [" RUN  v2.1.0", " FAIL  src/a.test.ts > Suite > bomb"] + ["❯" * 2040] * 100
    )
    space_bomb = "\n".join([" RUN  v2.1.0"] + [f"✓ a{' ' * 2030}b"] * 100)
    t0 = time.perf_counter()
    parse_runner_output(ToolExecution(1, "Bash", "vitest run", chevron_bomb, 1, True))
    parse_runner_output(ToolExecution(1, "Bash", "vitest run", space_bomb, 0, False))
    assert time.perf_counter() - t0 < 2.0
