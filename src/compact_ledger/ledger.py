"""Test attempt ledger, trajectory classifier, and edit-thrash detector.

All algorithms are 100% deterministic, zero-LLM, stdlib-only, and designed to stay
fast on large transcripts with bounded summaries and linear-time pattern checks.
"""

from __future__ import annotations

import fnmatch
import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from compact_ledger.models import (
    EditThrashEntry,
    FileEdit,
    ParsedTranscript,
    TestAttempt,
    TestTrajectory,
    ToolExecution,
    TrajectoryStatus,
    sanitize_unicode,
)
from compact_ledger.parsers.runners import (
    RunnerPassSignal,
    _bounded_generic_test_id,
    detect_runner,
    extract_go_failed_packages,
    extract_pass_signal,
    parse_runner_invocation,
    parse_runner_output,
    strip_ansi,
)

# Priority order for trajectories: urgent unresolved states first, resolved last.
_STATUS_PRIORITY: dict[TrajectoryStatus, int] = {
    "OSCILLATING": 0,
    "STALLED": 1,
    "PROGRESSING": 2,
    "SINGLE_FAILURE": 3,
    "RESOLVED": 4,
}

# Volatile noise normalizers for failure-message signature comparison (all strictly linear).
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s)\b")
_TMP_PYTEST_RE = re.compile(r"/(?:private/)?tmp/pytest-of-[^\s/]+/pytest-\d+")
_VAR_FOLDERS_RE = re.compile(r"/var/folders/[^\s/]+/[^\s/]+/T/[^\s/]+")
_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")

# Runner options that consume the immediately following argument token.
_PYTEST_VALUE_FLAGS = frozenset(
    {
        "-k",
        "-m",
        "-c",
        "-o",
        "-n",
        "-W",
        "-r",
        "-p",
        "--rootdir",
        "--confcutdir",
        "--junitxml",
        "--junit-prefix",
        "--cov",
        "--cov-report",
        "--maxfail",
        "--tb",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--basetemp",
        "--log-level",
        "--durations",
        "--timeout",
        "--dist",
        "--color",
        "--reruns",
        "--html",
    }
)
_JS_VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "-t",
        "--testNamePattern",
        "--testPathPattern",
        "--testPathIgnorePatterns",
        "--reporter",
        "--reporters",
        "--project",
        "--dir",
        "--root",
        "--maxWorkers",
        "-w",
        "--environment",
        "--outputFile",
        "--testTimeout",
        "--pool",
        "--color",
    }
)
_GO_VALUE_FLAGS = frozenset(
    {
        "-run",
        "-skip",
        "-count",
        "-timeout",
        "-tags",
        "-bench",
        "-benchtime",
        "-cpu",
        "-parallel",
        "-coverprofile",
        "-covermode",
        "-coverpkg",
        "-fuzz",
        "-fuzztime",
        "-o",
        "-p",
        "-exec",
        "-ldflags",
        "-gcflags",
    }
)

# Complex regex metacharacters that could cause backtracking if passed to `re`.
_UNSAFE_PATTERN_CHARS = frozenset("*+?{[(")


def _normalize_path(path: str) -> str:
    cleaned = sanitize_unicode(path).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _loc_file_only(assertion_loc: str | None) -> str:
    """Return normalized file path without line number so line shifts do not mask stalls."""
    if not assertion_loc:
        return ""
    norm = _normalize_path(assertion_loc)
    if ":" in norm:
        return norm.rsplit(":", 1)[0]
    return norm


def _normalize_message_for_sig(message: str | None) -> str:
    if not message:
        return ""
    cleaned = " ".join(strip_ansi(message).split())
    cleaned = _TMP_PYTEST_RE.sub("<tmpdir>", cleaned)
    cleaned = _VAR_FOLDERS_RE.sub("<tmpdir>", cleaned)
    cleaned = _HEX_ADDR_RE.sub("0xADDR", cleaned)
    cleaned = _DURATION_RE.sub("<dur>", cleaned)
    return cleaned


def _failure_signature(attempt: TestAttempt) -> tuple[str, ...]:
    """Compute a deterministic state signature for a failing/error TestAttempt.

    Uses file path (without line number) so minor line shifts while editing do not
    misclassify a stalled failure as progressing.
    """
    loc_file = _loc_file_only(attempt.assertion_loc)
    if attempt.actual is not None:
        return (loc_file, "val", attempt.actual.strip(), (attempt.expected or "").strip())
    return (loc_file, "msg", _normalize_message_for_sig(attempt.message or attempt.outcome))


def _state_label(attempt: TestAttempt) -> str:
    """Short display token for one attempt inside a trajectory summary."""
    if attempt.outcome == "passed":
        return "PASS"
    if attempt.actual is not None:
        return attempt.actual.strip()
    if attempt.message:
        msg = " ".join(attempt.message.split())
        if len(msg) > 60:
            return msg[:57].rstrip() + "..."
        return msg
    return attempt.outcome


def _order_tool_executions(executions: Sequence[ToolExecution]) -> list[ToolExecution]:
    if not executions:
        return []
    use_seq = all(exe.seq > 0 for exe in executions)
    indexed = list(enumerate(executions))
    if use_seq:
        indexed.sort(key=lambda pair: (pair[1].seq, pair[0]))
    else:
        indexed.sort(key=lambda pair: (pair[1].turn_index, pair[0]))
    return [exe for _, exe in indexed]


def _unescape_simple_regex(raw: str) -> str:
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _match_simple_filter(pattern: str, text: str, *, default_on_complex: bool) -> bool:
    """Strictly linear-time matcher for runner filter patterns (``-t``, ``-run``, ``-skip``).

    Supports plain substrings, ``^`` / ``$`` anchors, ``|`` alternations, and escaped
    literals without invoking backtracking regex engines. Falls back safely to
    ``default_on_complex`` for complex regex syntax.
    """
    if not pattern or len(pattern) > 200:
        return default_on_complex

    # Inspect unescaped characters for unsafe regex constructs.
    i = 0
    n = len(pattern)
    branches: list[str] = []
    start = 0
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            # Character class shorthand like \d, \w, \s -> treat as complex
            if pattern[i + 1].isalpha():
                return default_on_complex
            i += 2
            continue
        if ch in _UNSAFE_PATTERN_CHARS or ch == ".":
            return default_on_complex
        if ch == "|":
            branches.append(pattern[start:i])
            start = i + 1
        i += 1
    branches.append(pattern[start:])

    for branch in branches:
        if not branch:
            continue
        anchored_start = branch.startswith("^")
        anchored_end = branch.endswith("$") and not branch.endswith("\\$")
        core = branch[1 if anchored_start else 0 : len(branch) - (1 if anchored_end else 0)]
        literal = _unescape_simple_regex(core)
        if anchored_start and anchored_end:
            if text == literal:
                return True
        elif anchored_start:
            if text.startswith(literal):
                return True
        elif anchored_end:
            if text.endswith(literal):
                return True
        elif literal in text:
            return True
    return False


@dataclass(slots=True)
class _ParsedPassScope:
    """Pre-parsed pass signal with cached CLI scope and O(1) lookup sets."""

    exec_idx: int
    signal: RunnerPassSignal
    scope_known: bool
    explicit_ids: frozenset[str]
    passed_files: tuple[str, ...]
    passed_packages: frozenset[str]
    # Pytest fields
    pytest_targets: tuple[str, ...] = ()
    pytest_k_expr: str | None = None
    pytest_has_marker: bool = False
    pytest_ignored: tuple[str, ...] = ()
    pytest_ignore_globs: tuple[str, ...] = ()
    # JS fields
    js_targets: tuple[str, ...] = ()
    js_name_pattern: str | None = None
    js_path_pattern: str | None = None
    # Go fields
    go_packages: tuple[str, ...] = ()
    go_run_pattern: str | None = None
    go_skip_pattern: str | None = None
    # Generic command test_id
    generic_cmd_id: str = ""


def _parse_pytest_scope(
    args: list[str],
) -> tuple[list[str], str | None, bool, list[str], list[str]]:
    targets: list[str] = []
    ignored: list[str] = []
    ignore_globs: list[str] = []
    k_expr: str | None = None
    has_marker = False

    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "--":
            targets.extend(_normalize_path(t) for t in args[idx + 1 :])
            break
        if tok == "-k" and idx + 1 < n:
            k_expr = args[idx + 1].strip()
            idx += 2
            continue
        if tok.startswith("-k="):
            k_expr = tok.split("=", 1)[1].strip()
            idx += 1
            continue
        if tok.startswith("-k") and len(tok) > 2:
            # Attached short option: `pytest -kb_only`
            k_expr = tok[2:].strip()
            idx += 1
            continue
        if tok == "-m" or tok.startswith("-m="):
            has_marker = True
            idx += 2 if tok == "-m" and idx + 1 < n else 1
            continue
        if tok.startswith("-m") and len(tok) > 2:
            has_marker = True
            idx += 1
            continue
        if tok in ("--ignore", "--deselect") and idx + 1 < n:
            ignored.append(_normalize_path(args[idx + 1]))
            idx += 2
            continue
        if tok.startswith(("--ignore=", "--deselect=")):
            ignored.append(_normalize_path(tok.split("=", 1)[1]))
            idx += 1
            continue
        if tok == "--ignore-glob" and idx + 1 < n:
            ignore_globs.append(_normalize_path(args[idx + 1]))
            idx += 2
            continue
        if tok.startswith("--ignore-glob="):
            ignore_globs.append(_normalize_path(tok.split("=", 1)[1]))
            idx += 1
            continue
        if tok in _PYTEST_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        targets.append(_normalize_path(tok))
        idx += 1

    return targets, k_expr, has_marker, ignored, ignore_globs


def _parse_js_scope(runner: str, args: list[str]) -> tuple[list[str], str | None, str | None]:
    targets: list[str] = []
    name_pattern: str | None = None
    path_pattern: str | None = None

    idx = 0
    n = len(args)
    if runner == "vitest" and idx < n and args[idx] in ("run", "watch", "dev", "related", "bench"):
        idx += 1

    while idx < n:
        tok = args[idx]
        if tok == "--":
            idx += 1
            continue
        if tok in ("-t", "--testNamePattern") and idx + 1 < n:
            name_pattern = args[idx + 1]
            idx += 2
            continue
        if tok.startswith(("-t=", "--testNamePattern=")):
            name_pattern = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok == "--testPathPattern" and idx + 1 < n:
            path_pattern = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("--testPathPattern="):
            path_pattern = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in _JS_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        targets.append(_normalize_path(tok))
        idx += 1

    return targets, name_pattern, path_pattern


def _parse_go_scope(args: list[str]) -> tuple[list[str], str | None, str | None]:
    packages: list[str] = []
    run_pattern: str | None = None
    skip_pattern: str | None = None

    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "-run" and idx + 1 < n:
            run_pattern = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("-run="):
            run_pattern = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok == "-skip" and idx + 1 < n:
            skip_pattern = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("-skip="):
            skip_pattern = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in _GO_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        packages.append(tok)
        idx += 1

    return packages, run_pattern, skip_pattern


def _compile_pass_scope(exec_idx: int, signal: RunnerPassSignal) -> _ParsedPassScope:
    """Parse CLI scope once per passing execution so trajectory building stays fast."""
    inv = parse_runner_invocation(signal.command)
    scope_known = inv.scope_known and inv.runner == signal.runner
    explicit_ids = frozenset(signal.explicit_test_ids)
    passed_files = tuple(_normalize_path(p) for p in signal.passed_files)
    passed_packages = frozenset(_normalize_path(p).rstrip("/") for p in signal.passed_packages)

    parsed = _ParsedPassScope(
        exec_idx=exec_idx,
        signal=signal,
        scope_known=scope_known,
        explicit_ids=explicit_ids,
        passed_files=passed_files,
        passed_packages=passed_packages,
        generic_cmd_id=_bounded_generic_test_id(signal.command)
        if signal.runner == "generic"
        else "",
    )

    if scope_known and signal.runner == "pytest":
        targets, k_expr, has_marker, ignored, globs = _parse_pytest_scope(inv.args)
        parsed.pytest_targets = tuple(targets)
        parsed.pytest_k_expr = k_expr
        parsed.pytest_has_marker = has_marker
        parsed.pytest_ignored = tuple(ignored)
        parsed.pytest_ignore_globs = tuple(globs)
    elif scope_known and signal.runner in ("vitest", "jest"):
        targets, name_pat, path_pat = _parse_js_scope(signal.runner, inv.args)
        parsed.js_targets = tuple(targets)
        parsed.js_name_pattern = name_pat
        parsed.js_path_pattern = path_pat
    elif scope_known and signal.runner == "go":
        pkgs, run_pat, skip_pat = _parse_go_scope(inv.args)
        parsed.go_packages = tuple(pkgs)
        parsed.go_run_pattern = run_pat
        parsed.go_skip_pattern = skip_pat

    return parsed


def _pytest_target_covers(target: str, test_id: str, test_file: str) -> bool:
    if target in ("", "."):
        return True
    norm_target = target.rstrip("/")
    if not norm_target:
        return True
    if test_id == norm_target:
        return True
    if test_id.startswith(norm_target + "[") or test_id.startswith(norm_target + "::"):
        return True
    return test_file == norm_target or test_file.startswith(norm_target + "/")


def _js_test_file_and_name(test_id: str, assertion_loc: str | None) -> tuple[str | None, str]:
    if " > " in test_id:
        prefix, rest = test_id.split(" > ", 1)
        norm_prefix = _normalize_path(prefix)
        if "." in norm_prefix:
            return norm_prefix, rest
    if assertion_loc and ":" in assertion_loc:
        return _normalize_path(assertion_loc.split(":", 1)[0]), test_id
    return None, test_id


def _split_go_test_id(test_id: str) -> tuple[str | None, str]:
    if "::" in test_id:
        pkg, name = test_id.split("::", 1)
        return _normalize_path(pkg).rstrip("/"), name
    return None, test_id


def _pass_scope_covers_test(
    parsed: _ParsedPassScope,
    test_id: str,
    runner: str,
    assertion_loc: str | None,
    *,
    go_package: str | None = None,
) -> bool:
    """Return True iff ``parsed`` is a passing run whose scope provably covers ``test_id``."""
    signal = parsed.signal
    if signal.runner != runner:
        return False

    # Generic fallback: exact bounded command match.
    if runner == "generic":
        return parsed.generic_cmd_id == test_id

    # Explicit per-test pass covers an exact test_id match (Go IDs are package-qualified).
    if test_id in parsed.explicit_ids:
        return True

    # If the command was a fallback/wrapper/chdir whose scope cannot be verified, NEVER assume
    # count-only coverage; only explicit file/package evidence may resolve.
    if runner == "pytest":
        if not parsed.scope_known:
            return False
        if signal.skipped_count > 0 or signal.deselected_count > 0:
            return False
        norm_tid = _normalize_path(test_id)
        test_file = norm_tid.split("::", 1)[0]
        if parsed.pytest_has_marker:
            return False
        for ign in parsed.pytest_ignored:
            if _pytest_target_covers(ign, norm_tid, test_file):
                return False
        for glob in parsed.pytest_ignore_globs:
            if fnmatch.fnmatch(test_file, glob) or fnmatch.fnmatch(norm_tid, glob):
                return False
        if parsed.pytest_k_expr is not None:
            if not _SIMPLE_IDENT_RE.fullmatch(parsed.pytest_k_expr):
                return False
            if parsed.pytest_k_expr.lower() not in norm_tid.lower():
                return False
        if not parsed.pytest_targets:
            return True
        return any(_pytest_target_covers(t, norm_tid, test_file) for t in parsed.pytest_targets)

    if runner in ("vitest", "jest"):
        test_file, test_name_only = _js_test_file_and_name(test_id, assertion_loc)

        # If a name filter (-t / --testNamePattern) was given, match ONLY against the test name,
        # never against the file path.
        if parsed.js_name_pattern is not None and not _match_simple_filter(
            parsed.js_name_pattern, test_name_only, default_on_complex=False
        ):
            return False
        if parsed.js_path_pattern is not None and (
            not test_file
            or not _match_simple_filter(parsed.js_path_pattern, test_file, default_on_complex=False)
        ):
            return False

        # Skipped tests inside a file mean we cannot trust a file-level PASS or summary count.
        if signal.skipped_count > 0:
            return False

        if parsed.passed_files and test_file is not None:
            return any(
                pf == test_file or pf.endswith("/" + test_file) or test_file.endswith("/" + pf)
                for pf in parsed.passed_files
            )

        if not parsed.scope_known:
            return False

        if not parsed.js_targets:
            return True

        if test_file is None:
            return False

        for target in parsed.js_targets:
            norm_t = target.rstrip("/")
            if norm_t in ("", "."):
                return True
            if (
                test_file == norm_t
                or test_file.startswith(norm_t + "/")
                or test_file.endswith("/" + norm_t)
            ):
                return True
        return False

    if runner == "go":
        pkg_from_id, bare_name = _split_go_test_id(test_id)
        norm_go_pkg = pkg_from_id or (
            _normalize_path(go_package).rstrip("/") if go_package else None
        )

        if parsed.go_skip_pattern is not None and _match_simple_filter(
            parsed.go_skip_pattern, bare_name, default_on_complex=True
        ):
            return False
        if parsed.go_run_pattern is not None:
            top_name = bare_name.split("/", 1)[0]
            if not (
                _match_simple_filter(parsed.go_run_pattern, bare_name, default_on_complex=False)
                or _match_simple_filter(parsed.go_run_pattern, top_name, default_on_complex=False)
            ):
                return False

        if signal.skipped_count > 0:
            return False

        # Explicit package evidence from `ok <full_import_path>`: require exact package equality!
        if parsed.passed_packages:
            if norm_go_pkg is not None:
                return norm_go_pkg in parsed.passed_packages
            # Only resolve an unqualified Go test from `ok <pkg>` if the run was a repo-wide `./...`
            return parsed.scope_known and (
                "./..." in parsed.go_packages or "..." in parsed.go_packages
            )

        if not parsed.scope_known:
            return False

        # Only explicit `./...` or `...` is repo-wide; bare `go test` tests only `.` (current pkg)!
        if "./..." in parsed.go_packages or "..." in parsed.go_packages:
            return True

        # Bare `go test` or `go test .` with no package metadata on either side.
        if (not parsed.go_packages or parsed.go_packages == (".",)) and norm_go_pkg is None:
            loc_file = _loc_file_only(assertion_loc)
            if "/" not in loc_file:
                return True

        return False

    return False


def _classify_attempts(attempts: Sequence[TestAttempt]) -> TrajectoryStatus:
    """Classify a non-empty sequence of attempts (with >=1 failure) for one test."""
    if not attempts:
        return "SINGLE_FAILURE"

    if attempts[-1].outcome == "passed":
        return "RESOLVED"

    states: list[tuple[str, ...]] = []
    for att in attempts:
        if att.outcome == "passed":
            states.append(("__PASSED__",))
        else:
            states.append(_failure_signature(att))

    if len(states) == 1:
        return "SINGLE_FAILURE"

    runs: list[tuple[str, ...]] = []
    for state in states:
        if not runs or runs[-1] != state:
            runs.append(state)

    if len(set(runs)) < len(runs):
        return "OSCILLATING"

    if states[-1] == states[-2]:
        return "STALLED"

    return "PROGRESSING"


def _format_trajectory_summary(
    test_id: str,
    status: TrajectoryStatus,
    attempts: Sequence[TestAttempt],
    max_states_per_test: int,
) -> str:
    """Format a concise, bounded, high-signal single-line summary for a TestTrajectory."""
    limit = max(1, max_states_per_test)

    compressed: list[tuple[str, int]] = []
    for att in attempts:
        label = _state_label(att)
        if compressed and compressed[-1][0] == label:
            compressed[-1] = (label, compressed[-1][1] + 1)
        else:
            compressed.append((label, 1))

    truncated = len(compressed) > limit
    shown = compressed[-limit:]
    formatted_parts = [f"{lbl} (x{cnt})" if cnt > 1 else lbl for lbl, cnt in shown]
    chain = ("... -> " if truncated else "") + " -> ".join(formatted_parts)

    latest_fail: TestAttempt | None = None
    for att in reversed(attempts):
        if att.outcome in ("failed", "error"):
            latest_fail = att
            break

    detail = chain
    if latest_fail is not None and latest_fail.expected is not None:
        detail = f"{chain}; expected {latest_fail.expected}"

    loc_suffix = ""
    if latest_fail is not None and latest_fail.assertion_loc:
        loc_suffix = f" @ {latest_fail.assertion_loc}"

    return f"{status}: {test_id} [{detail}]{loc_suffix}"


def _build_trajectories_internal(
    attempts: Sequence[TestAttempt],
    ordered_executions: Sequence[ToolExecution],
    *,
    attempt_exec_indices: Sequence[int] | None,
    max_states_per_test: int,
) -> list[TestTrajectory]:
    grouped: dict[tuple[str, str], list[tuple[int | None, TestAttempt]]] = defaultdict(list)
    encounter_order: list[tuple[str, str]] = []

    for idx, att in enumerate(attempts):
        key = (att.runner, att.test_id)
        if key not in grouped:
            encounter_order.append(key)
        exec_idx = attempt_exec_indices[idx] if attempt_exec_indices is not None else None
        grouped[key].append((exec_idx, att))

    # Pre-compile pass scopes ONCE per passing execution and index by runner for O(1) lookup.
    pass_scopes_by_runner: dict[str, list[_ParsedPassScope]] = defaultdict(list)
    pass_exec_idxs_by_runner: dict[str, list[int]] = defaultdict(list)
    failing_exec_indices_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    go_package_by_test: dict[str, str] = {}

    for exec_idx, exe in enumerate(ordered_executions):
        sig = extract_pass_signal(exe)
        if sig is not None:
            scope = _compile_pass_scope(exec_idx, sig)
            pass_scopes_by_runner[sig.runner].append(scope)
            pass_exec_idxs_by_runner[sig.runner].append(exec_idx)
        else:
            if detect_runner(exe.command, exe.output) == "go":
                go_package_by_test.update(extract_go_failed_packages(exe.output))
            if attempt_exec_indices is None:
                for parsed in parse_runner_output(exe):
                    if parsed.outcome in ("failed", "error"):
                        failing_exec_indices_by_key[(parsed.runner, parsed.test_id)].append(
                            exec_idx
                        )

    trajectories: list[tuple[int, int, TestTrajectory]] = []

    for order_idx, key in enumerate(encounter_order):
        runner, test_id = key
        entries = grouped[key]
        raw_attempts = [att for _, att in entries]

        has_failure = any(att.outcome in ("failed", "error") for att in raw_attempts)
        if not has_failure:
            continue

        runner_scopes = pass_scopes_by_runner.get(runner)
        runner_exec_idxs = pass_exec_idxs_by_runner.get(runner)
        go_pkg = go_package_by_test.get(test_id)

        # Synthesize implicit covering passes both between failures (catching regressions after
        # an implicit pass) and after the last failure (marking RESOLVED).
        test_attempts: list[TestAttempt] = []
        if runner_scopes and runner_exec_idxs:
            for i, (e_idx, att) in enumerate(entries):
                test_attempts.append(att)
                if att.outcome not in ("failed", "error"):
                    continue

                cur_exec_idx = e_idx
                if cur_exec_idx is None and key in failing_exec_indices_by_key:
                    f_list = failing_exec_indices_by_key[key]
                    if i < len(f_list):
                        cur_exec_idx = f_list[i]

                next_entry = entries[i + 1] if i + 1 < len(entries) else None
                if next_entry is not None and next_entry[1].outcome == "passed":
                    continue

                next_exec_idx: int | None = None
                if next_entry is not None:
                    next_exec_idx = next_entry[0]
                    if next_exec_idx is None and key in failing_exec_indices_by_key:
                        f_list = failing_exec_indices_by_key[key]
                        if i + 1 < len(f_list):
                            next_exec_idx = f_list[i + 1]

                if cur_exec_idx is not None:
                    start_pos = bisect_right(runner_exec_idxs, cur_exec_idx)
                    end_pos = (
                        bisect_right(runner_exec_idxs, next_exec_idx - 1)
                        if next_exec_idx is not None
                        else len(runner_scopes)
                    )
                    # Check newest matching pass in range (for trailing passes) or earliest
                    # (for intermediate passes) to hit unfiltered passes in O(1).
                    scan_range = (
                        range(end_pos - 1, start_pos - 1, -1)
                        if next_entry is None
                        else range(start_pos, end_pos)
                    )
                    for pos in scan_range:
                        scope = runner_scopes[pos]
                        if _pass_scope_covers_test(
                            scope,
                            test_id,
                            runner,
                            att.assertion_loc,
                            go_package=go_pkg,
                        ):
                            test_attempts.append(
                                TestAttempt(
                                    test_id=test_id,
                                    runner=runner,
                                    outcome="passed",
                                    turn_index=scope.signal.turn_index,
                                    command=scope.signal.command,
                                )
                            )
                            break
                elif next_entry is None:
                    # Synthetic attempts without matching failing executions in tool_executions:
                    # fall back to turn_index comparison.
                    for scope in reversed(runner_scopes):
                        if scope.signal.turn_index < att.turn_index:
                            break
                        if _pass_scope_covers_test(
                            scope,
                            test_id,
                            runner,
                            att.assertion_loc,
                            go_package=go_pkg,
                        ):
                            test_attempts.append(
                                TestAttempt(
                                    test_id=test_id,
                                    runner=runner,
                                    outcome="passed",
                                    turn_index=scope.signal.turn_index,
                                    command=scope.signal.command,
                                )
                            )
                            break
        else:
            test_attempts = raw_attempts

        status = _classify_attempts(test_attempts)
        summary = _format_trajectory_summary(
            test_id=test_id,
            status=status,
            attempts=test_attempts,
            max_states_per_test=max_states_per_test,
        )
        traj = TestTrajectory(
            test_id=test_id,
            runner=runner,
            status=status,
            attempts=list(test_attempts),
            summary=summary,
        )
        trajectories.append((_STATUS_PRIORITY[status], order_idx, traj))

    trajectories.sort(key=lambda item: (item[0], item[1]))
    return [traj for _, _, traj in trajectories]


def build_trajectories(
    attempts: Sequence[TestAttempt],
    tool_executions: Sequence[ToolExecution] = (),
    *,
    max_states_per_test: int = 5,
) -> list[TestTrajectory]:
    """Build classified ``TestTrajectory`` records from parsed attempts and executions.

    Unresolved trajectories are ordered ahead of resolved ones by urgency
    (``OSCILLATING`` -> ``STALLED`` -> ``PROGRESSING`` -> ``SINGLE_FAILURE`` -> ``RESOLVED``),
    preserving encounter order within each status.
    """
    ordered_executions = _order_tool_executions(tool_executions)
    return _build_trajectories_internal(
        attempts,
        ordered_executions,
        attempt_exec_indices=None,
        max_states_per_test=max_states_per_test,
    )


@dataclass(slots=True)
class _TimelineEvent:
    kind: str  # "edit" | "fail" | "pass"
    turn_index: int
    seq: int
    order: int
    edit: FileEdit | None = None
    fail_signatures: frozenset[tuple[str, tuple[str, ...]]] = frozenset()
    failed_attempts: tuple[TestAttempt, ...] = ()
    pass_scope: _ParsedPassScope | None = None


def _normalize_hunk(text: str | None) -> str | None:
    if text is None:
        return None
    norm = sanitize_unicode(text).replace("\r\n", "\n")
    return norm if norm.strip() else None


def _is_trivial_stub(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) < 8 and len(stripped.split()) <= 1


def _detect_revert_on_path(edits: Sequence[FileEdit]) -> tuple[bool, int | None]:
    """Return ``(reverted, reverted_from_turn)`` if a later edit restores an earlier state."""
    seen_old_texts: dict[str, int] = {}
    seen_pairs: dict[tuple[str, str], int] = {}
    # Store full-file writes; `None` marks an intervening edit between writes.
    write_history: list[tuple[str | None, int]] = []

    for edit in edits:
        if edit.operation == "edit":
            if write_history and write_history[-1][0] is not None:
                write_history.append((None, edit.turn_index))
            old_norm = _normalize_hunk(edit.old_text)
            new_norm = _normalize_hunk(edit.new_text)
            if old_norm is None or new_norm is None or old_norm == new_norm:
                continue
            # Direct swap back: previous (A -> B), now (B -> A)
            if (new_norm, old_norm) in seen_pairs:
                return True, seen_pairs[(new_norm, old_norm)]
            # Multi-step cycle back: ignore tiny single-token stubs like `pass` to avoid noise
            if new_norm in seen_old_texts and not _is_trivial_stub(new_norm):
                return True, seen_old_texts[new_norm]
            seen_old_texts.setdefault(old_norm, edit.turn_index)
            seen_pairs.setdefault((old_norm, new_norm), edit.turn_index)
        elif edit.operation == "write":
            new_norm = _normalize_hunk(edit.new_text)
            if new_norm is None:
                continue
            # Restores an earlier write content after an intervening different write OR edit
            if len(write_history) >= 2 and write_history[-1][0] != new_norm:
                for prev_content, prev_turn in write_history[:-1]:
                    if prev_content is not None and prev_content == new_norm:
                        return True, prev_turn
            if not write_history or write_history[-1][0] != new_norm:
                write_history.append((new_norm, edit.turn_index))

    return False, None


def detect_edit_thrash(
    file_edits: Sequence[FileEdit],
    tool_executions: Sequence[ToolExecution],
    attempts: Sequence[TestAttempt] = (),
) -> list[EditThrashEntry]:
    """Detect edit-revert loops and repeated edit-failure thrashing per modified file."""
    if not file_edits:
        return []

    events: list[_TimelineEvent] = []
    all_items_have_seq = all(e.seq > 0 for e in file_edits) and all(
        t.seq > 0 for t in tool_executions
    )

    order_counter = 0
    for edit in file_edits:
        events.append(
            _TimelineEvent(
                kind="edit",
                turn_index=edit.turn_index,
                seq=edit.seq,
                order=order_counter,
                edit=edit,
            )
        )
        order_counter += 1

    ordered_execs = _order_tool_executions(tool_executions)
    failed_generic_cmds: set[str] = set()
    go_package_by_test: dict[str, str] = {}
    has_tool_fail_events = False

    for exec_idx, exe in enumerate(ordered_execs):
        clean_out = strip_ansi(exe.output)
        runner = detect_runner(exe.command, clean_out)
        generic_id = _bounded_generic_test_id(exe.command)
        parsed = parse_runner_output(
            exe,
            emit_generic_pass=(runner == "generic" and generic_id in failed_generic_cmds),
        )
        failing = [a for a in parsed if a.outcome in ("failed", "error")]
        if failing:
            has_tool_fail_events = True
            if runner == "generic":
                failed_generic_cmds.add(generic_id)
            elif runner == "go":
                go_package_by_test.update(extract_go_failed_packages(clean_out))
            sigs = frozenset((a.test_id, _failure_signature(a)) for a in failing)
            events.append(
                _TimelineEvent(
                    kind="fail",
                    turn_index=exe.turn_index,
                    seq=exe.seq,
                    order=order_counter,
                    fail_signatures=sigs,
                    failed_attempts=tuple(failing),
                )
            )
            order_counter += 1
        else:
            pass_sig = extract_pass_signal(exe)
            if pass_sig is not None and (
                pass_sig.runner != "generic" or generic_id in failed_generic_cmds
            ):
                events.append(
                    _TimelineEvent(
                        kind="pass",
                        turn_index=exe.turn_index,
                        seq=exe.seq,
                        order=order_counter,
                        pass_scope=_compile_pass_scope(exec_idx, pass_sig),
                    )
                )
                order_counter += 1

    if not has_tool_fail_events and attempts:
        by_turn: dict[int, list[TestAttempt]] = defaultdict(list)
        for att in attempts:
            by_turn[att.turn_index].append(att)
        for t_idx in sorted(by_turn):
            turn_atts = by_turn[t_idx]
            failing = [a for a in turn_atts if a.outcome in ("failed", "error")]
            if failing:
                sigs = frozenset((a.test_id, _failure_signature(a)) for a in failing)
                events.append(
                    _TimelineEvent(
                        kind="fail",
                        turn_index=t_idx,
                        seq=0,
                        order=order_counter,
                        fail_signatures=sigs,
                        failed_attempts=tuple(failing),
                    )
                )
                order_counter += 1
            elif any(a.outcome == "passed" for a in turn_atts):
                events.append(
                    _TimelineEvent(
                        kind="pass",
                        turn_index=t_idx,
                        seq=0,
                        order=order_counter,
                    )
                )
                order_counter += 1

    if all_items_have_seq and all(ev.seq > 0 for ev in events):
        events.sort(key=lambda ev: (ev.seq, ev.order))
    else:
        kind_rank = {"edit": 0, "fail": 1, "pass": 2}
        events.sort(key=lambda ev: (ev.turn_index, kind_rank[ev.kind], ev.order))

    edits_by_path: dict[str, list[tuple[int, FileEdit]]] = defaultdict(list)
    display_path: dict[str, str] = {}
    path_order: list[str] = []

    for ev_idx, ev in enumerate(events):
        if ev.kind == "edit" and ev.edit is not None:
            norm = _normalize_path(ev.edit.path)
            if norm not in edits_by_path:
                display_path[norm] = ev.edit.path
                path_order.append(norm)
            edits_by_path[norm].append((ev_idx, ev.edit))

    fail_positions = [(idx, ev) for idx, ev in enumerate(events) if ev.kind == "fail"]
    pass_positions = [(idx, ev) for idx, ev in enumerate(events) if ev.kind == "pass"]

    thrash_entries: list[EditThrashEntry] = []

    for norm_path in path_order:
        path_edits_with_pos = edits_by_path[norm_path]
        edits_only = [ed for _, ed in path_edits_with_pos]
        edit_positions = [pos for pos, _ in path_edits_with_pos]
        first_edit_pos = edit_positions[0]
        last_edit_pos = edit_positions[-1]

        reverted, reverted_from_turn = _detect_revert_on_path(edits_only)

        preceding_fail: tuple[int, _TimelineEvent] | None = None
        for f_pos, f_ev in fail_positions:
            if f_pos < first_edit_pos:
                preceding_fail = (f_pos, f_ev)
            else:
                break

        window_fails: list[tuple[int, _TimelineEvent]] = []
        if preceding_fail is not None:
            window_fails.append(preceding_fail)
        window_fails.extend(
            (f_pos, f_ev) for f_pos, f_ev in fail_positions if f_pos > first_edit_pos
        )

        # Check if clean passing run(s) after the last edit cover EVERY failing test
        # observed in this file's window (each covered after its own last failure).
        latest_fail_by_test: dict[tuple[str, str], tuple[int, TestAttempt]] = {}
        for f_pos, f_ev in window_fails:
            for att in f_ev.failed_attempts:
                latest_fail_by_test[(att.runner, att.test_id)] = (f_pos, att)

        passes_after_edit = [
            (p_pos, p_ev) for p_pos, p_ev in pass_positions if p_pos > last_edit_pos
        ]
        if not passes_after_edit:
            resolved_after_last_edit = False
        elif not latest_fail_by_test:
            last_fail_pos = window_fails[-1][0] if window_fails else -1
            resolved_after_last_edit = any(p_pos > last_fail_pos for p_pos, _ in passes_after_edit)
        else:
            resolved_after_last_edit = all(
                any(
                    p_pos > fail_pos
                    and (
                        p_ev.pass_scope is None
                        or _pass_scope_covers_test(
                            p_ev.pass_scope,
                            att.test_id,
                            att.runner,
                            att.assertion_loc,
                            go_package=go_package_by_test.get(att.test_id),
                        )
                    )
                    for p_pos, p_ev in passes_after_edit
                )
                for fail_pos, att in latest_fail_by_test.values()
            )

        fails_after_edit = [item for item in window_fails if item[0] > first_edit_pos]
        edits_after_first_fail = (
            sum(1 for pos in edit_positions if pos > window_fails[0][0]) if window_fails else 0
        )

        # Detect any recurring failure signature separated by at least one edit to this file
        # (robust to unrelated noise failures occurring in between).
        has_stalled_failures = False
        first_seen_sig_pos: dict[tuple[str, tuple[str, ...]], int] = {}
        for f_pos, f_ev in window_fails:
            for sig in f_ev.fail_signatures:
                prev_pos = first_seen_sig_pos.get(sig)
                if prev_pos is not None:
                    # Check if any edit to this file occurred strictly between prev_pos and f_pos.
                    ins = bisect_right(edit_positions, prev_pos)
                    if ins < len(edit_positions) and edit_positions[ins] < f_pos:
                        has_stalled_failures = True
                        break
                else:
                    first_seen_sig_pos[sig] = f_pos
            if has_stalled_failures:
                break

        edit_count = len(edits_only)
        failure_count = len(window_fails)

        is_revert_thrash = reverted and not resolved_after_last_edit
        is_stalled_thrash = (
            not resolved_after_last_edit
            and edit_count >= 2
            and len(fails_after_edit) >= 1
            and failure_count >= 2
            and has_stalled_failures
        )
        is_repeated_fail_thrash = (
            not resolved_after_last_edit
            and edit_count >= 3
            and len(fails_after_edit) >= 2
            and edits_after_first_fail >= 1
        )

        if not (is_revert_thrash or is_stalled_thrash or is_repeated_fail_thrash):
            continue

        turn_indices = list(dict.fromkeys(ed.turn_index for ed in edits_only))
        if is_revert_thrash:
            from_note = f" from turn {reverted_from_turn}" if reverted_from_turn is not None else ""
            if failure_count > 0:
                reason = (
                    f"Reverted prior edit{from_note} on {display_path[norm_path]} "
                    f"while failures persisted ({edit_count} edits, {failure_count} failures)"
                )
            else:
                reason = (
                    f"Reverted prior edit{from_note} on {display_path[norm_path]} "
                    f"({edit_count} edits)"
                )
        elif is_stalled_thrash:
            reason = (
                f"Repeated edits ({edit_count}) on {display_path[norm_path]} "
                f"between identical/stalled failures ({failure_count} failures)"
            )
        else:
            reason = (
                f"Repeated edits ({edit_count}) on {display_path[norm_path]} "
                f"without resolving failures ({failure_count} failures)"
            )

        thrash_entries.append(
            EditThrashEntry(
                path=display_path[norm_path],
                edit_count=edit_count,
                failure_count=failure_count,
                reverted=reverted,
                turn_indices=turn_indices,
                reason=reason,
            )
        )

    return thrash_entries


def build_attempt_ledger(
    transcript: ParsedTranscript,
    *,
    max_states_per_test: int = 5,
) -> tuple[list[TestAttempt], list[TestTrajectory], list[EditThrashEntry]]:
    """Build the complete attempt ledger, trajectories, and thrash report from a transcript."""
    ordered_executions = _order_tool_executions(transcript.tool_executions)

    attempts: list[TestAttempt] = []
    attempt_exec_indices: list[int] = []
    failed_generic_cmds: set[str] = set()

    for exec_idx, exe in enumerate(ordered_executions):
        clean_out = strip_ansi(exe.output)
        runner = detect_runner(exe.command, clean_out)
        generic_id = _bounded_generic_test_id(exe.command)
        parsed = parse_runner_output(
            exe,
            emit_generic_pass=(runner == "generic" and generic_id in failed_generic_cmds),
        )
        for item in parsed:
            if runner == "generic" and item.outcome in ("failed", "error"):
                failed_generic_cmds.add(item.test_id)
            attempts.append(item)
            attempt_exec_indices.append(exec_idx)

    trajectories = _build_trajectories_internal(
        attempts,
        ordered_executions,
        attempt_exec_indices=attempt_exec_indices,
        max_states_per_test=max_states_per_test,
    )
    thrash_entries = detect_edit_thrash(
        transcript.file_edits,
        ordered_executions,
        attempts,
    )
    return attempts, trajectories, thrash_entries


__all__ = [
    "build_attempt_ledger",
    "build_trajectories",
    "detect_edit_thrash",
]
