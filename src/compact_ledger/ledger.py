"""Test attempt ledger, trajectory classifier, and edit-thrash detector.

All algorithms are 100% deterministic, zero-LLM, stdlib-only, and operate in
linear time over parsed transcript events.
"""

from __future__ import annotations

import re
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
    detect_runner,
    extract_go_failed_packages,
    extract_pass_signal,
    extract_runner_invocation,
    normalize_command,
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

# Volatile noise normalizers for failure-message signature comparison.
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s)\b")
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
    }
)


def _normalize_path(path: str) -> str:
    cleaned = sanitize_unicode(path).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _normalize_message_for_sig(message: str | None) -> str:
    if not message:
        return ""
    cleaned = " ".join(strip_ansi(message).split())
    cleaned = _HEX_ADDR_RE.sub("0xADDR", cleaned)
    cleaned = _DURATION_RE.sub("<dur>", cleaned)
    return cleaned


def _failure_signature(attempt: TestAttempt) -> tuple[str, ...]:
    """Compute a deterministic state signature for a failing/error TestAttempt."""
    loc = _normalize_path(attempt.assertion_loc) if attempt.assertion_loc else ""
    if attempt.actual is not None:
        return (loc, "val", attempt.actual.strip(), (attempt.expected or "").strip())
    return (loc, "msg", _normalize_message_for_sig(attempt.message or attempt.outcome))


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


def _extract_pytest_scope(command: str | None) -> tuple[list[str], str | None, bool, list[str]]:
    """Parse pytest CLI args into ``(targets, k_expr, has_marker_filter, ignored)``."""
    runner, args = extract_runner_invocation(command)
    if runner != "pytest":
        return [], None, False, []

    targets: list[str] = []
    ignored: list[str] = []
    k_expr: str | None = None
    has_marker = False

    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "--":
            targets.extend(_normalize_path(t) for t in args[idx + 1 :])
            break
        if tok.startswith("-k="):
            k_expr = tok.split("=", 1)[1].strip()
            idx += 1
            continue
        if tok == "-k" and idx + 1 < n:
            k_expr = args[idx + 1].strip()
            idx += 2
            continue
        if tok == "-m" or tok.startswith("-m="):
            has_marker = True
            idx += 2 if tok == "-m" and idx + 1 < n else 1
            continue
        if tok in ("--ignore", "--deselect") and idx + 1 < n:
            ignored.append(_normalize_path(args[idx + 1]))
            idx += 2
            continue
        if tok.startswith(("--ignore=", "--deselect=")):
            ignored.append(_normalize_path(tok.split("=", 1)[1]))
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

    return targets, k_expr, has_marker, ignored


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


def _extract_js_scope(command: str | None) -> tuple[list[str], str | None, str | None]:
    """Parse vitest/jest CLI args into ``(targets, name_pattern, path_pattern)``."""
    runner, args = extract_runner_invocation(command)
    if runner not in ("vitest", "jest"):
        return [], None, None

    targets: list[str] = []
    name_pattern: str | None = None
    path_pattern: str | None = None

    idx = 0
    n = len(args)
    # Skip vitest subcommands like `run`, `watch`, `related`
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


def _extract_go_scope(command: str | None) -> tuple[list[str], str | None, str | None]:
    """Parse ``go test`` CLI args into ``(packages, run_pattern, skip_pattern)``."""
    runner, args = extract_runner_invocation(command)
    if runner != "go":
        return [], None, None

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


def _js_test_file(test_id: str, assertion_loc: str | None) -> str | None:
    if " > " in test_id:
        prefix = _normalize_path(test_id.split(" > ", 1)[0])
        if "." in prefix:
            return prefix
    if assertion_loc and ":" in assertion_loc:
        return _normalize_path(assertion_loc.split(":", 1)[0])
    return None


def _safe_regex_search(pattern: str, text: str) -> bool:
    if len(pattern) > 200:
        return False
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def _pass_signal_covers_test(
    signal: RunnerPassSignal,
    test_id: str,
    runner: str,
    assertion_loc: str | None,
    *,
    go_package: str | None = None,
) -> bool:
    """Return True iff ``signal`` is a passing run whose scope provably covers ``test_id``."""
    if signal.runner != runner:
        return False

    # Explicit per-test pass always covers an exact test_id match.
    if test_id in signal.explicit_test_ids:
        return True

    if runner == "generic":
        return normalize_command(signal.command) == test_id

    if runner == "pytest":
        norm_tid = _normalize_path(test_id)
        test_file = norm_tid.split("::", 1)[0]
        targets, k_expr, has_marker, ignored = _extract_pytest_scope(signal.command)
        if has_marker:
            return False
        for ign in ignored:
            if _pytest_target_covers(ign, norm_tid, test_file):
                return False
        if k_expr is not None:
            # Conservative: only trust simple positive substring identifiers in `-k`
            if not _SIMPLE_IDENT_RE.fullmatch(k_expr):
                return False
            if k_expr.lower() not in norm_tid.lower():
                return False
        if not targets:
            return True
        return any(_pytest_target_covers(t, norm_tid, test_file) for t in targets)

    if runner in ("vitest", "jest"):
        test_file = _js_test_file(test_id, assertion_loc)
        targets, name_pattern, path_pattern = _extract_js_scope(signal.command)

        if name_pattern is not None and not _safe_regex_search(name_pattern, test_id):
            return False
        if path_pattern is not None and (
            not test_file or not _safe_regex_search(path_pattern, test_file)
        ):
            return False

        # If output explicitly lists PASS files, verify our test file is among them
        # whenever a test_file is known.
        if signal.passed_files and test_file is not None:
            return any(
                pf == test_file or pf.endswith("/" + test_file) or test_file.endswith("/" + pf)
                for pf in signal.passed_files
            )

        if not targets:
            return True

        if test_file is None:
            return False

        for target in targets:
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
        packages, run_pattern, skip_pattern = _extract_go_scope(signal.command)
        if skip_pattern is not None and _safe_regex_search(skip_pattern, test_id):
            return False
        if run_pattern is not None:
            # Go's -run matches each `/`-separated subtest component; check top-level or full match.
            top_name = test_id.split("/", 1)[0]
            if not (
                _safe_regex_search(run_pattern, test_id)
                or _safe_regex_search(run_pattern, top_name)
            ):
                return False

        if not packages or "./..." in packages or "..." in packages:
            return True

        # Scoped package run (e.g. `go test ./pkg/b`): check failing Go package or dir.
        test_dir: str | None = None
        if assertion_loc and ":" in assertion_loc:
            fpath = _normalize_path(assertion_loc.split(":", 1)[0])
            if "/" in fpath:
                test_dir = fpath.rsplit("/", 1)[0]

        norm_go_pkg = _normalize_path(go_package).rstrip("/") if go_package else None

        if signal.passed_packages:
            for passed_pkg in signal.passed_packages:
                norm_passed = _normalize_path(passed_pkg).rstrip("/")
                if norm_go_pkg and (
                    norm_passed == norm_go_pkg
                    or norm_passed.endswith("/" + norm_go_pkg)
                    or norm_go_pkg.endswith("/" + norm_passed)
                ):
                    return True
                if test_dir and (norm_passed == test_dir or norm_passed.endswith("/" + test_dir)):
                    return True

        for pkg in packages:
            norm_pkg = _normalize_path(pkg).rstrip("/")
            if norm_pkg in ("", "."):
                if test_dir is None and norm_go_pkg is None:
                    return True
                continue
            if norm_pkg.endswith("/..."):
                prefix = norm_pkg[:-4].rstrip("/")
                if test_dir and (test_dir == prefix or test_dir.startswith(prefix + "/")):
                    return True
                if norm_go_pkg and (
                    norm_go_pkg == prefix
                    or norm_go_pkg.endswith("/" + prefix)
                    or ("/" + prefix + "/") in ("/" + norm_go_pkg + "/")
                ):
                    return True
                continue
            if test_dir and (test_dir == norm_pkg or test_dir.endswith("/" + norm_pkg)):
                return True
            if norm_go_pkg and (norm_go_pkg == norm_pkg or norm_go_pkg.endswith("/" + norm_pkg)):
                return True

        return False

    return False


def _classify_attempts(attempts: Sequence[TestAttempt]) -> TrajectoryStatus:
    """Classify a non-empty sequence of attempts (with >=1 failure) for one test."""
    if not attempts:
        return "SINGLE_FAILURE"

    if attempts[-1].outcome == "passed":
        return "RESOLVED"

    # Build chronological state sequence including any intermediate PASS transitions.
    states: list[tuple[str, ...]] = []
    for att in attempts:
        if att.outcome == "passed":
            states.append(("__PASSED__",))
        else:
            states.append(_failure_signature(att))

    if len(states) == 1:
        return "SINGLE_FAILURE"

    # Compress consecutive identical states into runs.
    runs: list[tuple[str, ...]] = []
    for state in states:
        if not runs or runs[-1] != state:
            runs.append(state)

    # OSCILLATING: an earlier state recurred after at least one different state intervened.
    if len(set(runs)) < len(runs):
        return "OSCILLATING"

    # STALLED: latest attempts are stuck repeating the exact same failure signature.
    if states[-1] == states[-2]:
        return "STALLED"

    # PROGRESSING: moving through distinct states without recurrence.
    return "PROGRESSING"


def _format_trajectory_summary(
    test_id: str,
    status: TrajectoryStatus,
    attempts: Sequence[TestAttempt],
    max_states_per_test: int,
) -> str:
    """Format a concise, bounded, high-signal single-line summary for a TestTrajectory."""
    limit = max(1, max_states_per_test)

    # Run-length encode consecutive identical display labels so stalled loops stay compact.
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

    # Pull expected + assertion_loc from the latest failing/error attempt.
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

    # Pre-compute passing runner signals, Go package mappings, and failing execution indices.
    pass_signals: list[tuple[int, RunnerPassSignal]] = []
    failing_exec_indices_by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    go_package_by_test: dict[str, str] = {}
    for exec_idx, exe in enumerate(ordered_executions):
        sig = extract_pass_signal(exe)
        if sig is not None:
            pass_signals.append((exec_idx, sig))
        else:
            if detect_runner(exe.command, exe.output) == "go":
                go_package_by_test.update(extract_go_failed_packages(exe.output))
            if attempt_exec_indices is None:
                # Map failing executions to keys so direct build_trajectories() callers
                # still get accurate intra-turn ordering.
                for parsed in parse_runner_output(exe):
                    if parsed.outcome in ("failed", "error"):
                        failing_exec_indices_by_key[(parsed.runner, parsed.test_id)].append(
                            exec_idx
                        )

    trajectories: list[tuple[int, int, TestTrajectory]] = []

    for order_idx, key in enumerate(encounter_order):
        runner, test_id = key
        entries = grouped[key]
        test_attempts = [att for _, att in entries]

        # Only track tests that have actually failed/errored at least once.
        has_failure = any(att.outcome in ("failed", "error") for att in test_attempts)
        if not has_failure:
            continue

        # Check if an unscoped or matching scoped passing execution occurred AFTER
        # the last failure when the last recorded attempt is still failing.
        if test_attempts[-1].outcome in ("failed", "error") and pass_signals:
            last_fail_exec_idx: int | None = None
            last_fail_turn = test_attempts[-1].turn_index
            last_fail_loc = test_attempts[-1].assertion_loc

            for e_idx, att in reversed(entries):
                if att.outcome in ("failed", "error"):
                    last_fail_exec_idx = e_idx
                    last_fail_turn = att.turn_index
                    last_fail_loc = att.assertion_loc
                    break

            if last_fail_exec_idx is None and key in failing_exec_indices_by_key:
                last_fail_exec_idx = failing_exec_indices_by_key[key][-1]

            for pass_exec_idx, signal in pass_signals:
                is_after = (
                    pass_exec_idx > last_fail_exec_idx
                    if last_fail_exec_idx is not None
                    else signal.turn_index >= last_fail_turn
                )
                if not is_after:
                    continue
                if _pass_signal_covers_test(
                    signal,
                    test_id,
                    runner,
                    last_fail_loc,
                    go_package=go_package_by_test.get(test_id),
                ):
                    test_attempts = [
                        *test_attempts,
                        TestAttempt(
                            test_id=test_id,
                            runner=runner,
                            outcome="passed",
                            turn_index=signal.turn_index,
                            command=signal.command,
                        ),
                    ]
                    break

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
    pass_signal: RunnerPassSignal | None = None


def _normalize_hunk(text: str | None) -> str | None:
    if text is None:
        return None
    norm = sanitize_unicode(text).replace("\r\n", "\n")
    return norm if norm.strip() else None


def _detect_revert_on_path(edits: Sequence[FileEdit]) -> tuple[bool, int | None]:
    """Return ``(reverted, reverted_from_turn)`` if a later edit restores an earlier state."""
    seen_old_texts: dict[str, int] = {}
    seen_pairs: dict[tuple[str, str], int] = {}
    write_history: list[tuple[str, int]] = []

    for edit in edits:
        if edit.operation == "edit":
            old_norm = _normalize_hunk(edit.old_text)
            new_norm = _normalize_hunk(edit.new_text)
            if old_norm is None or new_norm is None or old_norm == new_norm:
                continue
            # Direct swap back: previous (A -> B), now (B -> A)
            if (new_norm, old_norm) in seen_pairs:
                return True, seen_pairs[(new_norm, old_norm)]
            # Multi-step cycle back: an earlier edit replaced `new_norm` and now we restore it
            if new_norm in seen_old_texts:
                return True, seen_old_texts[new_norm]
            seen_old_texts.setdefault(old_norm, edit.turn_index)
            seen_pairs.setdefault((old_norm, new_norm), edit.turn_index)
        elif edit.operation == "write":
            new_norm = _normalize_hunk(edit.new_text)
            if new_norm is None:
                continue
            # Restores a previous full-file write content after a different intermediate write
            if len(write_history) >= 2 and write_history[-1][0] != new_norm:
                for prev_content, prev_turn in write_history[:-1]:
                    if prev_content == new_norm:
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

    # Track failed generic commands chronologically so generic passes are recognized.
    ordered_execs = _order_tool_executions(tool_executions)
    failed_generic_cmds: set[str] = set()
    go_package_by_test: dict[str, str] = {}
    has_tool_fail_events = False

    for exe in ordered_execs:
        clean_out = strip_ansi(exe.output)
        runner = detect_runner(exe.command, clean_out)
        norm_cmd = normalize_command(exe.command)
        parsed = parse_runner_output(
            exe,
            emit_generic_pass=(runner == "generic" and norm_cmd in failed_generic_cmds),
        )
        failing = [a for a in parsed if a.outcome in ("failed", "error")]
        if failing:
            has_tool_fail_events = True
            if runner == "generic":
                failed_generic_cmds.add(norm_cmd)
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
                pass_sig.runner != "generic" or norm_cmd in failed_generic_cmds
            ):
                events.append(
                    _TimelineEvent(
                        kind="pass",
                        turn_index=exe.turn_index,
                        seq=exe.seq,
                        order=order_counter,
                        pass_signal=pass_sig,
                    )
                )
                order_counter += 1

    # Fallback: if caller supplied pre-parsed `attempts` without matching `tool_executions`,
    # synthesize fail/pass timeline events from `attempts` grouped by turn_index.
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

    # Group edits by normalized path while preserving display path and event positions.
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
        first_edit_pos = path_edits_with_pos[0][0]
        last_edit_pos = path_edits_with_pos[-1][0]

        reverted, reverted_from_turn = _detect_revert_on_path(edits_only)

        # Failures relevant to this file's edit cycle:
        # include any failure immediately preceding the first edit (triggering the debug loop)
        # plus all failures after the first edit.
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

        # Check if clean passing run(s) after the last edit actually cover EVERY failing
        # test observed in this file's window (each covered after its own last failure).
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
                        p_ev.pass_signal is None
                        or _pass_signal_covers_test(
                            p_ev.pass_signal,
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

        # Count failures that occurred after at least one edit to this file, and check
        # whether edits actually interleaved with failures (preventing false positives
        # from unrelated edits made before a single later failure).
        fails_after_edit = [item for item in window_fails if item[0] > first_edit_pos]
        edits_after_first_fail = (
            sum(1 for pos, _ in path_edits_with_pos if pos > window_fails[0][0])
            if window_fails
            else 0
        )

        # Check for identical/stalled consecutive failure runs separated by at least one edit.
        has_stalled_failures = False
        for i in range(1, len(window_fails)):
            prev_pos, prev_ev = window_fails[i - 1]
            cur_pos, cur_ev = window_fails[i]
            edit_between = any(prev_pos < e_pos < cur_pos for e_pos, _ in path_edits_with_pos)
            if not edit_between:
                continue
            if (
                prev_ev.fail_signatures
                and cur_ev.fail_signatures
                and (prev_ev.fail_signatures & cur_ev.fail_signatures)
            ):
                has_stalled_failures = True
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
        norm_cmd = normalize_command(exe.command)
        parsed = parse_runner_output(
            exe,
            emit_generic_pass=(runner == "generic" and norm_cmd in failed_generic_cmds),
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
