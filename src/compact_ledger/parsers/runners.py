"""Deterministic test runner output parsers for compact-ledger.

Parses tool/shell outputs from ``pytest``, ``vitest``, ``jest``, ``go test``,
and generic failing commands into structured ``TestAttempt`` records using only
the Python standard library and strictly linear-time (ReDoS-safe) patterns.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from compact_ledger.models import TestAttempt, ToolExecution, sanitize_unicode

# Single-pass, non-backtracking ANSI escape stripper (CSI, OSC, and 2-byte ESC).
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

# Cap per-line regex inspection length so adversarial multi-megabyte single lines
# cannot degrade parsing performance.
_MAX_LINE_SCAN = 2048
_MAX_VALUE_LEN = 200
_MAX_MESSAGE_LEN = 240

_SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "bash",
        "shell",
        "shell_command",
        "exec_command",
        "local_shell",
        "container.exec",
        "command",
        "run_command",
    }
)

_WRAPPER_SINGLE = frozenset({"env", "time", "nice", "nohup", "xvfb-run", "npx", "pnpx", "bunx"})
_WRAPPER_WITH_RUN = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch"})
_NODE_PKG_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")

# Runner detection output heuristics (all linear, anchored or simple literal checks).
_PYTEST_SESSION_RE = re.compile(
    r"^={3,}\s*(?:test session starts|FAILURES|ERRORS|short test summary info)\s*={3,}$"
)
_PYTEST_SUMMARY_BAR_RE = re.compile(
    r"^(?:={3,}\s+)?\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)\b[^=\n]*\bin\s+[0-9.]+s\b"
)
_PYTEST_NODE_STATUS_RE = re.compile(
    r"^(?:(PASSED|FAILED|ERROR)\s+(\S+::\S+)|(\S+::\S+)\s+(PASSED|FAILED|ERROR))(?:\s|$)"
)
_PYTEST_FAIL_HEADER_RE = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$")
_PYTEST_ERROR_HEADER_RE = re.compile(r"^ERROR\s+(?:at\s+\w+\s+of\s+)?(.+)$")
_PYTEST_COLLECT_ERROR_RE = re.compile(r"^ERROR\s+collecting\s+(\S+)")
_PYTEST_LOC_LINE_RE = re.compile(r"^([^\s:][^:]*\.py):(\d+):\s*(.+)?$")
_PYTEST_ASSERT_CMP_RE = re.compile(
    r"^assert\s+(.+?)\s+(==|!=|<=|>=|<|>|is not|is|not in|in)\s+(.+)$"
)
_PYTEST_DID_NOT_RAISE_RE = re.compile(r"DID NOT RAISE\s+(.+)$")

_EXPECTED_GOT_RE = re.compile(
    r"\bexpected[:\s]+([^,;\n]+?)(?:,\s*|\s+but\s+|\s+)(?:got|actual|received)[:\s]+(.+)$",
    re.IGNORECASE,
)
_GOT_EXPECTED_RE = re.compile(
    r"\b(?:got|actual|received)[:\s]+([^,;\n]+?)(?:,\s*|\s+)(?:want|wanted|expected)[:\s]+(.+)$",
    re.IGNORECASE,
)
_WANT_GOT_RE = re.compile(
    r"\b(?:want|wanted)[:\s]+([^,;\n]+?)(?:,\s*|\s+)(?:got|actual|received)[:\s]+(.+)$",
    re.IGNORECASE,
)

# Vitest / Jest patterns.
_VITEST_BANNER_RE = re.compile(r"^\s*RUN\s+v\d+\.\d+")
_VITEST_TEST_FILES_RE = re.compile(r"^\s*Test Files\s+(.+)$")
_JEST_TEST_SUITES_RE = re.compile(r"^\s*Test Suites:\s+(.+)$")
_JS_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests:?\s+(.+)$")
_JS_FILE_STATUS_RE = re.compile(
    r"^\s*(PASS|FAIL|✓|✔|×|✕|✗)\s+([^\s(][^(\n]*?\.(?:[cm]?[jt]sx?|spec\.[jt]sx?|test\.[jt]sx?))(?:\s|$)"
)
_VITEST_FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+([^\s>]+)\s*>\s*(.+)$")
_JEST_BULLET_RE = re.compile(r"^\s*●\s+(.+)$")
_JS_CROSS_TEST_RE = re.compile(r"^\s*[×✕✗]\s+(.+?)(?:\s+\(\d+\s*m?s\)|\s+\d+\s*m?s)?\s*$")
_JS_CHECK_TEST_RE = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\)|\s+\d+\s*m?s)?\s*$")
_JS_EXPECTED_LINE_RE = re.compile(
    r"^\s*Expected(?:\s+(?:value|substring|pattern|length|number of calls))?:\s*(.+)$"
)
_JS_RECEIVED_LINE_RE = re.compile(
    r"^\s*Received(?:\s+(?:value|string|length|number of calls))?:\s*(.+)$"
)
_VITEST_ASSERTION_RE = re.compile(
    r"AssertionError:\s*expected\s+(.+?)\s+to\s+(?:(?:deeply|strictly)\s+)?"
    r"(?:be|equal|eql|match|contain)\s+(.+?)(?:\s+//\s+.*)?$",
    re.IGNORECASE,
)
_JS_STACK_PAREN_RE = re.compile(r"\(([^()\s][^()]*?\.[cm]?[jt]sx?):(\d+)(?::\d+)?\)")
_JS_STACK_BARE_RE = re.compile(r"(?:^|\s|❯\s*)([^\s()<>]+\.[cm]?[jt]sx?):(\d+)(?::\d+)?(?:\s|$)")

# Go test patterns.
_GO_RUN_RE = re.compile(r"^\s*===\s*RUN\s+(\S+)")
_GO_RESULT_RE = re.compile(r"^\s*---\s*(FAIL|PASS|SKIP):\s*(\S+)(?:\s+\([^)]*\))?")
_GO_FILE_LINE_RE = re.compile(r"^\s*([^\s:]+\.go):(\d+)(?::\d+)?:\s*(.*)$")
_GO_PKG_OK_RE = re.compile(r"^ok\s+(\S+)(?:\s+(.*))?$")
_GO_PKG_FAIL_RE = re.compile(r"^FAIL\s+(\S+)(?:\s+(.*))?$")
_GO_TESTIFY_EXP_RE = re.compile(r"^\s*expected\s*:\s*(.+)$", re.IGNORECASE)
_GO_TESTIFY_ACT_RE = re.compile(r"^\s*actual\s*:\s*(.+)$", re.IGNORECASE)

# Generic noise line filters.
_DECORATIVE_LINE_RE = re.compile(r"^[\s=\-*_~#]+$")
_EXIT_CODE_ONLY_RE = re.compile(
    r"^(?:Exit code[:\s]+|Command exited with (?:non-zero )?status\s+"
    r"|Process exited with code\s+)-?\d+\s*$",
    re.IGNORECASE,
)
_ERROR_KEYWORD_RE = re.compile(
    r"\b(?:error|exception|failed|failure|fatal|panic|traceback|cannot|"
    r"not found|denied|invalid|syntaxerror|typeerror|valueerror|assertionerror)\b",
    re.IGNORECASE,
)
_COUNT_TOKEN_RE = re.compile(r"\b(\d+)\s+(passed|failed|errors?|skipped)\b", re.IGNORECASE)


@dataclass(slots=True)
class RunnerPassSignal:
    """Structured summary of a clean passing runner execution for scope-aware resolution."""

    runner: str
    turn_index: int
    command: str | None = None
    passed_count: int = 0
    failed_count: int = 0
    passed_files: list[str] = field(default_factory=list)
    passed_packages: list[str] = field(default_factory=list)
    explicit_test_ids: list[str] = field(default_factory=list)


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes and normalize line endings in linear time."""
    if not text:
        return ""
    clean = sanitize_unicode(text)
    if "\x1b" in clean:
        clean = _ANSI_RE.sub("", clean)
    if "\r" in clean:
        clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    return clean


def normalize_command(command: str | None) -> str:
    """Normalize a shell command string for deterministic matching."""
    if not command:
        return ""
    return " ".join(strip_ansi(command).split())


def _cap_line(line: str) -> str:
    return line[:_MAX_LINE_SCAN] if len(line) > _MAX_LINE_SCAN else line


def _clean_text(text: str | None, max_len: int) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(sanitize_unicode(text).split())
    if not collapsed:
        return None
    if len(collapsed) > max_len:
        return collapsed[: max_len - 3].rstrip() + "..."
    return collapsed


def _clean_value(text: str | None) -> str | None:
    return _clean_text(text, _MAX_VALUE_LEN)


def _clean_message(text: str | None) -> str | None:
    return _clean_text(text, _MAX_MESSAGE_LEN)


def _normalize_rel_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _split_shell_subcommands(command: str) -> list[list[str]]:
    """Split a shell command string into subcommand argv lists, respecting quotes."""
    subcommands: list[list[str]] = []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Unclosed quote fallback: split cleanly on whitespace without failing.
        tokens = command.split()

    current: list[str] = []
    for tok in tokens:
        if tok in ("&&", "||", ";", "|", "&"):
            if current:
                subcommands.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        subcommands.append(current)
    return subcommands


def _unwrap_runner_tokens(tokens: list[str]) -> list[str]:
    """Strip leading env assignments and command wrappers (uv run, npx, etc.)."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if _ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        base = PurePosixPath(tok).name.lower()
        if base == "timeout":
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                idx += 1
            if idx < n:
                idx += 1  # duration
            continue
        if base in _WRAPPER_SINGLE:
            idx += 1
            while (
                idx < n and tokens[idx].startswith("-") and base in ("env", "npx", "pnpx", "bunx")
            ):
                idx += 1
            continue
        if base in _WRAPPER_WITH_RUN and idx + 1 < n and tokens[idx + 1] == "run":
            idx += 2
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if (
                    flag
                    in (
                        "-w",
                        "--with",
                        "--directory",
                        "--project",
                        "--python",
                        "--group",
                        "--extra",
                    )
                    and idx < n
                ):
                    idx += 1
            continue
        if base in _NODE_PKG_MANAGERS and idx + 1 < n and tokens[idx + 1] == "exec":
            # Keep `npm test` / `pnpm test` intact; unwrap `pnpm exec vitest`
            idx += 2
            if idx < n and tokens[idx] == "--":
                idx += 1
            continue
        if base == "pnpm" and idx + 1 < n and tokens[idx + 1].lower() in ("vitest", "jest"):
            idx += 1
            continue
        if base == "yarn" and idx + 1 < n and tokens[idx + 1].lower() in ("vitest", "jest"):
            idx += 1
            continue
        if base == "bun" and idx + 1 < n and tokens[idx + 1].lower() in ("vitest", "jest"):
            idx += 1
            continue
        break
    return tokens[idx:]


def extract_runner_invocation(command: str | None) -> tuple[str | None, list[str]]:
    """Return ``(runner, args_after_runner)`` from a shell command, if recognized."""
    if not command:
        return None, []
    clean_cmd = strip_ansi(command)
    detected: tuple[str | None, list[str]] = (None, [])
    for sub in _split_shell_subcommands(clean_cmd):
        unwrapped = _unwrap_runner_tokens(sub)
        if not unwrapped:
            continue
        exe = PurePosixPath(unwrapped[0]).name.lower()
        rest = unwrapped[1:]
        if exe in ("pytest", "py.test"):
            detected = ("pytest", rest)
        elif (
            exe.startswith("python")
            and len(rest) >= 2
            and rest[0] == "-m"
            and rest[1] in ("pytest", "py.test")
        ):
            detected = ("pytest", rest[2:])
        elif exe == "vitest":
            detected = ("vitest", rest)
        elif exe == "jest":
            detected = ("jest", rest)
        elif exe == "go" and rest and rest[0] == "test":
            detected = ("go", rest[1:])
    return detected


def detect_runner(command: str | None, output: str) -> str:
    """Detect the test runner from command and output (pytest, vitest, jest, go, generic)."""
    cmd_runner, _ = extract_runner_invocation(command)
    if cmd_runner is not None:
        return cmd_runner

    clean_output = strip_ansi(output)
    if not clean_output:
        return "generic"

    lines = clean_output.split("\n")
    has_pytest = False
    has_vitest = False
    has_jest = False
    has_go = False

    for raw_line in lines[:400]:
        line = _cap_line(raw_line)
        if not line.strip():
            continue
        if (
            _PYTEST_SESSION_RE.match(line)
            or _PYTEST_SUMMARY_BAR_RE.match(line)
            or _PYTEST_NODE_STATUS_RE.match(line)
            or _PYTEST_COLLECT_ERROR_RE.match(line)
        ):
            has_pytest = True
            break
        if (
            _VITEST_BANNER_RE.match(line)
            or _VITEST_TEST_FILES_RE.match(line)
            or _VITEST_FAIL_LINE_RE.match(line)
        ):
            has_vitest = True
        if _JEST_TEST_SUITES_RE.match(line) or _JEST_BULLET_RE.match(line):
            has_jest = True
        if (
            _GO_RESULT_RE.match(line)
            or _GO_RUN_RE.match(line)
            or _GO_PKG_OK_RE.match(line)
            or _GO_PKG_FAIL_RE.match(line)
        ):
            has_go = True

    if has_pytest:
        return "pytest"
    if has_vitest:
        return "vitest"
    if has_jest:
        return "jest"
    if has_go:
        return "go"
    return "generic"


def _extract_expected_actual_from_text(text: str) -> tuple[str | None, str | None]:
    """Extract (expected, actual) from common textual assertion phrases."""
    capped = _cap_line(text)
    m = _EXPECTED_GOT_RE.search(capped)
    if m:
        return _clean_value(m.group(1)), _clean_value(m.group(2))
    m = _GOT_EXPECTED_RE.search(capped)
    if m:
        return _clean_value(m.group(2)), _clean_value(m.group(1))
    m = _WANT_GOT_RE.search(capped)
    if m:
        return _clean_value(m.group(1)), _clean_value(m.group(2))
    m = _VITEST_ASSERTION_RE.search(capped)
    if m:
        return _clean_value(m.group(2)), _clean_value(m.group(1))
    return None, None


def _parse_pytest_e_lines(
    e_lines: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Return ``(expected, actual, message)`` from pytest ``E   `` lines."""
    expected: str | None = None
    actual: str | None = None
    message: str | None = None

    for raw in e_lines:
        stripped = _cap_line(raw).strip()
        if not stripped:
            continue

        # pytest.raises DID NOT RAISE
        dnr = _PYTEST_DID_NOT_RAISE_RE.search(stripped)
        if dnr:
            expected = expected or _clean_value(f"raises {dnr.group(1).strip()}")
            actual = actual or "DID NOT RAISE"
            message = message or _clean_message(stripped)
            continue

        # assert <left> <op> <right>
        cmp_match = _PYTEST_ASSERT_CMP_RE.match(stripped)
        if cmp_match:
            left, op, right = (
                cmp_match.group(1).strip(),
                cmp_match.group(2),
                cmp_match.group(3).strip(),
            )
            if actual is None:
                actual = _clean_value(left)
            if expected is None:
                expected = _clean_value(right if op in ("==", "is") else f"{op} {right}")
            message = message or _clean_message(stripped)
            continue

        # AssertionError: expected X, got Y (or got Y, want X)
        exp_t, act_t = _extract_expected_actual_from_text(stripped)
        if exp_t is not None or act_t is not None:
            expected = expected or exp_t
            actual = actual or act_t
            message = message or _clean_message(stripped)
            continue

        # Fallback: first informative E-line (skipping pytest diff noise)
        if message is None and not stripped.startswith(
            (
                "+",
                "-",
                "?",
                "Full diff:",
                "Differing items:",
                "Left contains",
                "Right contains",
                "Use -v",
            )
        ):
            message = _clean_message(stripped)

    if message is None and e_lines:
        message = _clean_message(e_lines[0])
    return expected, actual, message


def _pytest_header_matches_node(header_name: str, node_id: str) -> bool:
    """Match a pytest failure section header (e.g. ``TestCls.test_x[1]``) to a full node ID."""
    h = header_name.strip()
    if h.startswith("ERROR at "):
        # e.g. "ERROR at setup of test_foo" or "ERROR at teardown of TestA.test_b"
        parts = h.split(" of ", 1)
        if len(parts) == 2:
            h = parts[1].strip()
    if h == node_id:
        return True
    if "::" in node_id:
        tail = node_id.split("::", 1)[1]
        if tail == h:
            return True
        if tail.replace("::", ".") == h:
            return True
        last_part = node_id.rsplit("::", 1)[-1]
        if last_part == h:
            return True
    return False


def _parse_pytest(execution: ToolExecution, clean_output: str) -> list[TestAttempt]:
    """Parse pytest output into TestAttempt records (both failures/errors and explicit passes)."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    # 1. Extract detailed blocks from FAILURES / ERRORS sections.
    # Each block entry: {header, outcome, assertion_loc, expected, actual, message, file_hint}
    blocks: list[dict[str, str | None]] = []
    in_section: str | None = None  # "FAILURES" | "ERRORS" | "SUMMARY" | None
    current_block: dict[str, object] | None = None

    summary_failures: list[tuple[str, str, str | None]] = []  # (outcome, node_id, detail)
    verbose_passes: list[str] = []
    verbose_failures: list[tuple[str, str]] = []  # (outcome, node_id)
    collect_errors: list[tuple[str, str | None]] = []

    def _flush_block() -> None:
        nonlocal current_block
        if current_block is None:
            return
        e_lines: list[str] = current_block["e_lines"]  # type: ignore[assignment]
        expected, actual, message = _parse_pytest_e_lines(e_lines)
        loc = current_block["assertion_loc"]
        exc_hint = current_block["exc_hint"]
        if message is None and isinstance(exc_hint, str):
            message = _clean_message(exc_hint)
        blocks.append(
            {
                "header": str(current_block["header"]),
                "outcome": str(current_block["outcome"]),
                "assertion_loc": str(loc) if loc else None,
                "expected": expected,
                "actual": actual,
                "message": message,
                "file_hint": str(current_block["file_hint"])
                if current_block["file_hint"]
                else None,
            }
        )
        current_block = None

    for raw_line in lines:
        line = _cap_line(raw_line)
        stripped = line.strip()

        # Section transitions
        if stripped.startswith("===") and stripped.endswith("==="):
            upper = stripped.upper()
            if "FAILURES" in upper:
                _flush_block()
                in_section = "FAILURES"
                continue
            if "ERRORS" in upper:
                _flush_block()
                in_section = "ERRORS"
                continue
            if "SHORT TEST SUMMARY INFO" in upper:
                _flush_block()
                in_section = "SUMMARY"
                continue
            if _PYTEST_SUMMARY_BAR_RE.match(stripped) or "WARNINGS SUMMARY" in upper:
                _flush_block()
                in_section = None
                continue

        if in_section in ("FAILURES", "ERRORS"):
            hdr_match = _PYTEST_FAIL_HEADER_RE.match(stripped)
            if hdr_match:
                _flush_block()
                header_name = hdr_match.group(1).strip()
                collect_m = _PYTEST_COLLECT_ERROR_RE.match(header_name)
                if collect_m:
                    collect_errors.append((_normalize_rel_path(collect_m.group(1)), None))
                outcome = (
                    "error"
                    if in_section == "ERRORS" or header_name.startswith("ERROR ")
                    else "failed"
                )
                current_block = {
                    "header": header_name,
                    "outcome": outcome,
                    "e_lines": [],
                    "assertion_loc": None,
                    "file_hint": None,
                    "exc_hint": None,
                }
                continue

            if current_block is not None:
                if line.startswith("E   ") or line.startswith("E\t"):
                    current_block["e_lines"].append(line[1:].strip())  # type: ignore[union-attr]
                else:
                    loc_m = _PYTEST_LOC_LINE_RE.match(stripped)
                    if loc_m:
                        fpath = _normalize_rel_path(loc_m.group(1))
                        lineno = loc_m.group(2)
                        rest = (loc_m.group(3) or "").strip()
                        current_block["assertion_loc"] = f"{fpath}:{lineno}"
                        if current_block["file_hint"] is None:
                            current_block["file_hint"] = fpath
                        if rest and not rest.startswith("in "):
                            current_block["exc_hint"] = rest
                continue

        if in_section == "SUMMARY":
            # FAILED tests/test_a.py::test_x - AssertionError: assert 400 == 100
            # ERROR tests/test_a.py::test_y - RuntimeError: boom
            if stripped.startswith(("FAILED ", "ERROR ")):
                status_word, rest = stripped.split(None, 1)
                outcome = "error" if status_word == "ERROR" else "failed"
                if " - " in rest:
                    node_id, detail = rest.split(" - ", 1)
                else:
                    node_id, detail = rest, None
                node_id = _normalize_rel_path(node_id.strip())
                if node_id:
                    summary_failures.append((outcome, node_id, detail.strip() if detail else None))
            elif stripped.startswith("PASSED "):
                node_id = _normalize_rel_path(stripped.split(None, 1)[1].strip())
                if node_id:
                    verbose_passes.append(node_id)
            continue

        # Outside detailed sections: check verbose PASSED/FAILED/ERROR lines and collection errors
        col_m = _PYTEST_COLLECT_ERROR_RE.match(stripped)
        if col_m:
            collect_errors.append((_normalize_rel_path(col_m.group(1)), None))
            continue

        if stripped.startswith(("FAILED ", "ERROR ")) and " - " in stripped:
            status_word, rest = stripped.split(None, 1)
            node_part, detail_part = rest.split(" - ", 1)
            if "::" in node_part:
                outcome = "error" if status_word == "ERROR" else "failed"
                summary_failures.append(
                    (outcome, _normalize_rel_path(node_part.strip()), detail_part.strip())
                )
                continue

        node_m = _PYTEST_NODE_STATUS_RE.match(stripped)
        if node_m:
            if node_m.group(1) and node_m.group(2):
                status_word = node_m.group(1)
                node_id = _normalize_rel_path(node_m.group(2))
            else:
                node_id = _normalize_rel_path(node_m.group(3))
                status_word = node_m.group(4)
            if status_word == "PASSED":
                verbose_passes.append(node_id)
            elif status_word == "FAILED":
                verbose_failures.append(("failed", node_id))
            elif status_word == "ERROR":
                verbose_failures.append(("error", node_id))

    _flush_block()

    attempts: list[TestAttempt] = []
    seen_fail_ids: set[str] = set()
    used_blocks: set[int] = set()

    # 2. Match summary failures with detailed blocks for full node_id + rich details.
    for outcome, node_id, detail in summary_failures:
        matched_idx: int | None = None
        for idx, blk in enumerate(blocks):
            if idx in used_blocks:
                continue
            if _pytest_header_matches_node(blk["header"] or "", node_id):
                matched_idx = idx
                break

        assertion_loc: str | None = None
        expected: str | None = None
        actual: str | None = None
        message: str | None = None

        if matched_idx is not None:
            used_blocks.add(matched_idx)
            blk = blocks[matched_idx]
            assertion_loc = blk["assertion_loc"]
            expected = blk["expected"]
            actual = blk["actual"]
            message = blk["message"]
        if detail:
            if expected is None and actual is None:
                d_exp, d_act, d_msg = _parse_pytest_e_lines([detail])
                # Also handle `AssertionError: assert 400 == 100`
                if d_exp is None and d_act is None and ": assert " in detail:
                    after_colon = detail.split(": ", 1)[1]
                    d_exp, d_act, _ = _parse_pytest_e_lines([after_colon])
                expected = expected or d_exp
                actual = actual or d_act
                message = message or _clean_message(detail) or d_msg
            elif message is None:
                message = _clean_message(detail)

        if node_id not in seen_fail_ids:
            seen_fail_ids.add(node_id)
            attempts.append(
                TestAttempt(
                    test_id=node_id,
                    runner="pytest",
                    assertion_loc=assertion_loc,
                    expected=expected,
                    actual=actual,
                    outcome="error" if outcome == "error" else "failed",
                    turn_index=turn_index,
                    message=message,
                    command=command,
                )
            )

    # 3. Emit any detailed blocks not present in short test summary info.
    for idx, blk in enumerate(blocks):
        if idx in used_blocks:
            continue
        header = blk["header"] or ""
        if _PYTEST_COLLECT_ERROR_RE.match(header):
            col_m = _PYTEST_COLLECT_ERROR_RE.match(header)
            node_id = _normalize_rel_path(col_m.group(1)) if col_m else header
        else:
            # Reconstruct node_id using verbose_failures match or file_hint + header
            node_id = ""
            for _, v_node in verbose_failures:
                if _pytest_header_matches_node(header, v_node):
                    node_id = v_node
                    break
            if not node_id:
                clean_header = header
                if clean_header.startswith("ERROR at ") and " of " in clean_header:
                    clean_header = clean_header.split(" of ", 1)[1].strip()
                clean_header = (
                    clean_header.replace(".", "::")
                    if "::" not in clean_header and "[" not in clean_header
                    else clean_header
                )
                if blk["file_hint"] and "::" not in header:
                    node_id = f"{blk['file_hint']}::{clean_header}"
                else:
                    node_id = _normalize_rel_path(clean_header)

        if node_id and node_id not in seen_fail_ids:
            seen_fail_ids.add(node_id)
            attempts.append(
                TestAttempt(
                    test_id=node_id,
                    runner="pytest",
                    assertion_loc=blk["assertion_loc"],
                    expected=blk["expected"],
                    actual=blk["actual"],
                    outcome="error" if blk["outcome"] == "error" else "failed",
                    turn_index=turn_index,
                    message=blk["message"],
                    command=command,
                )
            )

    # 4. Emit any verbose failures not already emitted.
    for outcome, node_id in verbose_failures:
        if node_id not in seen_fail_ids:
            seen_fail_ids.add(node_id)
            attempts.append(
                TestAttempt(
                    test_id=node_id,
                    runner="pytest",
                    outcome="error" if outcome == "error" else "failed",
                    turn_index=turn_index,
                    command=command,
                )
            )

    # 5. Emit collection errors not already emitted.
    for col_path, col_msg in collect_errors:
        if col_path not in seen_fail_ids:
            seen_fail_ids.add(col_path)
            attempts.append(
                TestAttempt(
                    test_id=col_path,
                    runner="pytest",
                    outcome="error",
                    turn_index=turn_index,
                    message=_clean_message(col_msg or f"ERROR collecting {col_path}"),
                    command=command,
                )
            )

    # 6. Emit explicit verbose PASSED attempts (for tests that weren't marked failed).
    seen_pass_ids: set[str] = set()
    for node_id in verbose_passes:
        if node_id not in seen_fail_ids and node_id not in seen_pass_ids:
            seen_pass_ids.add(node_id)
            attempts.append(
                TestAttempt(
                    test_id=node_id,
                    runner="pytest",
                    outcome="passed",
                    turn_index=turn_index,
                    command=command,
                )
            )

    # 7. Fallback for --tb=line failures if nothing else matched and run failed.
    if not attempts and (
        (execution.exit_code is not None and execution.exit_code != 0) or execution.is_error
    ):
        for raw_line in lines:
            line = _cap_line(raw_line).strip()
            loc_m = _PYTEST_LOC_LINE_RE.match(line)
            if loc_m and loc_m.group(3):
                fpath = _normalize_rel_path(loc_m.group(1))
                loc = f"{fpath}:{loc_m.group(2)}"
                rest = loc_m.group(3).strip()
                exp, act = _extract_expected_actual_from_text(rest)
                attempts.append(
                    TestAttempt(
                        test_id=loc,
                        runner="pytest",
                        assertion_loc=loc,
                        expected=exp,
                        actual=act,
                        outcome="failed",
                        turn_index=turn_index,
                        message=_clean_message(rest),
                        command=command,
                    )
                )
                break

    return attempts


def _extract_js_location(lines: list[str], preferred_file: str | None) -> str | None:
    """Extract the best ``file:line`` stack location from a Vitest/Jest failure block."""
    first_non_module: str | None = None
    for raw in lines:
        line = _cap_line(raw)
        for m in (_JS_STACK_PAREN_RE.search(line), _JS_STACK_BARE_RE.search(line)):
            if not m:
                continue
            fpath = _normalize_rel_path(m.group(1))
            lineno = m.group(2)
            if "node_modules/" in fpath or fpath.startswith("node:"):
                continue
            loc = f"{fpath}:{lineno}"
            if preferred_file and (
                fpath == preferred_file
                or fpath.endswith("/" + preferred_file)
                or preferred_file.endswith("/" + fpath)
            ):
                return loc
            if first_non_module is None:
                first_non_module = loc
    return first_non_module


def _parse_js_block_details(
    block_lines: list[str],
    preferred_file: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract ``(assertion_loc, expected, actual, message)`` from a Vitest/Jest block."""
    expected: str | None = None
    actual: str | None = None
    message: str | None = None
    diff_expected: str | None = None
    diff_received: str | None = None
    in_diff = False

    for raw in block_lines:
        line = _cap_line(raw)
        stripped = line.strip()
        if not stripped:
            continue

        # Explicit Expected: / Received:
        exp_m = _JS_EXPECTED_LINE_RE.match(line)
        if exp_m and expected is None:
            expected = _clean_value(exp_m.group(1))
            continue
        rec_m = _JS_RECEIVED_LINE_RE.match(line)
        if rec_m and actual is None:
            actual = _clean_value(rec_m.group(1))
            continue

        # Diff markers: - Expected / + Received
        if stripped.startswith("- Expected"):
            in_diff = True
            continue
        if stripped.startswith("+ Received"):
            in_diff = True
            continue
        if in_diff:
            if stripped.startswith("- ") and diff_expected is None:
                diff_expected = _clean_value(stripped[2:])
                continue
            if stripped.startswith("+ ") and diff_received is None:
                diff_received = _clean_value(stripped[2:])
                continue

        # Vitest AssertionError: expected X to be Y
        vit_m = _VITEST_ASSERTION_RE.search(stripped)
        if vit_m:
            if actual is None:
                actual = _clean_value(vit_m.group(1))
            if expected is None:
                expected = _clean_value(vit_m.group(2))
            if message is None:
                message = _clean_message(stripped)
            continue

        if message is None and (
            stripped.startswith(
                (
                    "expect(",
                    "AssertionError:",
                    "Error:",
                    "TypeError:",
                    "ReferenceError:",
                    "RangeError:",
                )
            )
        ):
            message = _clean_message(stripped)

    if expected is None and diff_expected is not None:
        expected = diff_expected
    if actual is None and diff_received is not None:
        actual = diff_received

    loc = _extract_js_location(block_lines, preferred_file)
    if message is None and expected is not None and actual is not None:
        message = _clean_message(f"expected {expected}, received {actual}")
    return loc, expected, actual, message


def _normalize_js_test_id(file_path: str | None, raw_name: str) -> str:
    """Normalize Jest/Vitest test identifiers into ``file > suite > test`` format."""
    cleaned = " > ".join(
        part.strip() for part in raw_name.replace(" › ", " > ").split(" > ") if part.strip()
    )
    if file_path:
        norm_file = _normalize_rel_path(file_path)
        if cleaned == norm_file or cleaned.startswith(norm_file + " > "):
            return cleaned
        return f"{norm_file} > {cleaned}" if cleaned else norm_file
    return cleaned


def _parse_js_runner(execution: ToolExecution, clean_output: str, runner: str) -> list[TestAttempt]:
    """Parse Vitest or Jest output into TestAttempt records."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    attempts: list[TestAttempt] = []
    seen_fail_ids: set[str] = set()
    seen_pass_ids: set[str] = set()

    current_file: str | None = None
    current_block_id: str | None = None
    current_block_file: str | None = None
    current_block_lines: list[str] = []
    inline_cross_failures: list[tuple[str | None, str]] = []

    def _flush_js_block() -> None:
        nonlocal current_block_id, current_block_file, current_block_lines
        if current_block_id is None:
            return
        loc, expected, actual, message = _parse_js_block_details(
            current_block_lines,
            current_block_file,
        )
        if current_block_id not in seen_fail_ids:
            seen_fail_ids.add(current_block_id)
            attempts.append(
                TestAttempt(
                    test_id=current_block_id,
                    runner=runner,
                    assertion_loc=loc,
                    expected=expected,
                    actual=actual,
                    outcome="failed",
                    turn_index=turn_index,
                    message=message,
                    command=command,
                )
            )
        current_block_id = None
        current_block_file = None
        current_block_lines = []

    for raw_line in lines:
        line = _cap_line(raw_line)
        stripped = line.strip()

        # Stop collecting a failure block on summary banners or horizontal dividers
        if (
            _VITEST_TEST_FILES_RE.match(line)
            or _JEST_TEST_SUITES_RE.match(line)
            or _JS_TESTS_SUMMARY_RE.match(line)
            or (stripped.startswith("⎯") and len(stripped) >= 6)
        ):
            _flush_js_block()
            continue

        # Vitest failure block header: FAIL  src/calc.test.ts > Suite > test name
        vf_m = _VITEST_FAIL_LINE_RE.match(line)
        if vf_m:
            _flush_js_block()
            fpath = _normalize_rel_path(vf_m.group(1))
            current_file = fpath
            current_block_file = fpath
            current_block_id = _normalize_js_test_id(fpath, vf_m.group(2))
            current_block_lines = []
            continue

        # File-level PASS / FAIL header (not per-test lines containing ` > `)
        file_m = _JS_FILE_STATUS_RE.match(line) if " > " not in line else None
        if file_m:
            _flush_js_block()
            marker = file_m.group(1)
            fpath = _normalize_rel_path(file_m.group(2))
            current_file = fpath
            if marker in ("FAIL", "PASS", "✓", "✔", "×", "✕", "✗"):
                continue

        # Jest failure block header: ● Suite › test name
        bullet_m = _JEST_BULLET_RE.match(line)
        if bullet_m:
            _flush_js_block()
            current_block_file = current_file
            current_block_id = _normalize_js_test_id(current_file, bullet_m.group(1))
            current_block_lines = []
            continue

        # Inside an active failure block
        if current_block_id is not None:
            current_block_lines.append(line)
            continue

        # Outside failure blocks: capture inline × failed tests and ✓ passed tests
        cross_m = _JS_CROSS_TEST_RE.match(line)
        if cross_m:
            test_name = cross_m.group(1).strip()
            if " > " in line or not _JS_FILE_STATUS_RE.match(line):
                inline_cross_failures.append((current_file, test_name))
            continue

        check_m = _JS_CHECK_TEST_RE.match(line)
        if check_m and (" > " in line or not _JS_FILE_STATUS_RE.match(line)):
            test_name = check_m.group(1).strip()
            tid = _normalize_js_test_id(current_file, test_name)
            if tid and tid not in seen_fail_ids and tid not in seen_pass_ids:
                seen_pass_ids.add(tid)
                attempts.append(
                    TestAttempt(
                        test_id=tid,
                        runner=runner,
                        outcome="passed",
                        turn_index=turn_index,
                        command=command,
                    )
                )

    _flush_js_block()

    # Emit any inline `× test name` failures that did not have a detailed block
    for fpath, test_name in inline_cross_failures:
        tid = _normalize_js_test_id(fpath, test_name)
        already_matched = tid in seen_fail_ids or any(
            existing == tid or existing.endswith(" > " + test_name) for existing in seen_fail_ids
        )
        if tid and not already_matched:
            seen_fail_ids.add(tid)
            attempts.append(
                TestAttempt(
                    test_id=tid,
                    runner=runner,
                    outcome="failed",
                    turn_index=turn_index,
                    command=command,
                )
            )

    return attempts


def _parse_go_test(execution: ToolExecution, clean_output: str) -> list[TestAttempt]:
    """Parse ``go test`` output (both verbose and non-verbose) into TestAttempt records."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    # Track per-test log lines collected either after `=== RUN` or after `--- FAIL:`.
    test_logs: dict[str, list[tuple[str | None, str]]] = {}
    failed_tests: list[str] = []
    passed_tests: list[str] = []
    failed_set: set[str] = set()
    passed_set: set[str] = set()
    build_errors: list[tuple[str, str]] = []

    active_test: str | None = None
    in_result_for: str | None = None

    for raw_line in lines:
        line = _cap_line(raw_line)
        stripped = line.strip()
        if not stripped:
            continue

        run_m = _GO_RUN_RE.match(line)
        if run_m:
            active_test = run_m.group(1)
            in_result_for = None
            test_logs.setdefault(active_test, [])
            continue

        res_m = _GO_RESULT_RE.match(line)
        if res_m:
            status = res_m.group(1)
            tname = res_m.group(2)
            active_test = None
            in_result_for = tname if status == "FAIL" else None
            test_logs.setdefault(tname, [])
            if status == "FAIL" and tname not in failed_set:
                failed_set.add(tname)
                failed_tests.append(tname)
            elif status == "PASS" and tname not in passed_set:
                passed_set.add(tname)
                passed_tests.append(tname)
            continue

        if (
            _GO_PKG_OK_RE.match(stripped)
            or _GO_PKG_FAIL_RE.match(stripped)
            or stripped in ("PASS", "FAIL")
        ):
            active_test = None
            in_result_for = None
            continue

        target_test = in_result_for or active_test
        fl_m = _GO_FILE_LINE_RE.match(line)
        if fl_m:
            loc = f"{_normalize_rel_path(fl_m.group(1))}:{fl_m.group(2)}"
            msg = fl_m.group(3).strip()
            if target_test is not None:
                test_logs.setdefault(target_test, []).append((loc, msg))
            else:
                build_errors.append((loc, msg))
            continue

        if target_test is not None and (line.startswith(("    ", "\t"))):
            test_logs.setdefault(target_test, []).append((None, stripped))

    # Avoid emitting redundant parent test failures when a child subtest failed and
    # the parent test itself recorded no direct failure lines.
    attempts: list[TestAttempt] = []
    for tname in failed_tests:
        logs = test_logs.get(tname, [])
        has_failing_subtest = any(other.startswith(tname + "/") for other in failed_set)
        if has_failing_subtest and not logs:
            continue

        assertion_loc: str | None = None
        expected: str | None = None
        actual: str | None = None
        message: str | None = None

        for loc, text in logs:
            if loc and assertion_loc is None:
                assertion_loc = loc
            if not text:
                continue
            exp_t, act_t = _extract_expected_actual_from_text(text)
            if exp_t is not None or act_t is not None:
                expected = expected or exp_t
                actual = actual or act_t
                message = message or _clean_message(text)
                continue
            # testify/assert multiline style
            te_m = _GO_TESTIFY_EXP_RE.match(text)
            if te_m and expected is None:
                expected = _clean_value(te_m.group(1))
                continue
            ta_m = _GO_TESTIFY_ACT_RE.match(text)
            if ta_m and actual is None:
                actual = _clean_value(ta_m.group(1))
                continue
            if message is None and not text.startswith(("Error Trace:", "Test:", "Messages:")):
                message = _clean_message(text)

        if message is None and expected is not None and actual is not None:
            message = _clean_message(f"got {actual}, want {expected}")

        attempts.append(
            TestAttempt(
                test_id=tname,
                runner="go",
                assertion_loc=assertion_loc,
                expected=expected,
                actual=actual,
                outcome="failed",
                turn_index=turn_index,
                message=message,
                command=command,
            )
        )

    # Emit build/compile errors if no test failures were found.
    if (
        not attempts
        and build_errors
        and ((execution.exit_code is not None and execution.exit_code != 0) or execution.is_error)
    ):
        loc, msg = build_errors[0]
        attempts.append(
            TestAttempt(
                test_id=loc.split(":", 1)[0],
                runner="go",
                assertion_loc=loc,
                outcome="error",
                turn_index=turn_index,
                message=_clean_message(msg),
                command=command,
            )
        )

    # Emit explicit passing tests (skip parent passes only if they didn't fail).
    for tname in passed_tests:
        if tname not in failed_set:
            attempts.append(
                TestAttempt(
                    test_id=tname,
                    runner="go",
                    outcome="passed",
                    turn_index=turn_index,
                    command=command,
                )
            )

    return attempts


def _extract_last_meaningful_error_line(clean_output: str) -> str | None:
    """Extract the last meaningful error line from generic command output."""
    if not clean_output:
        return None
    non_empty: list[str] = []
    for raw_line in clean_output.split("\n"):
        stripped = _cap_line(raw_line).strip()
        if not stripped:
            continue
        if _DECORATIVE_LINE_RE.match(stripped) or _EXIT_CODE_ONLY_RE.match(stripped):
            continue
        non_empty.append(stripped)

    if not non_empty:
        return None

    # Prefer the last line with an explicit error keyword within the tail window,
    # otherwise fall back to the very last meaningful line.
    tail = non_empty[-25:]
    for candidate in reversed(tail):
        if _ERROR_KEYWORD_RE.search(candidate):
            return _clean_message(candidate)
    return _clean_message(tail[-1])


def _is_command_execution(execution: ToolExecution) -> bool:
    if execution.command and execution.command.strip():
        return True
    return execution.tool_name.lower() in _SHELL_TOOL_NAMES


def _parse_generic(
    execution: ToolExecution,
    clean_output: str,
    *,
    emit_generic_pass: bool = False,
) -> list[TestAttempt]:
    """Fallback parser for non-test-runner failing (and optionally passing) CLI commands."""
    if not _is_command_execution(execution):
        return []

    norm_cmd = normalize_command(execution.command)
    if not norm_cmd:
        return []

    is_failure = (
        execution.exit_code is not None and execution.exit_code != 0
    ) or execution.is_error
    if is_failure:
        last_line = _extract_last_meaningful_error_line(clean_output)
        if execution.exit_code is not None and last_line:
            msg = _clean_message(f"exit {execution.exit_code}: {last_line}")
        elif last_line:
            msg = last_line
        elif execution.exit_code is not None:
            msg = f"exit {execution.exit_code}"
        else:
            msg = "command failed"
        return [
            TestAttempt(
                test_id=norm_cmd,
                runner="generic",
                outcome="failed",
                turn_index=execution.turn_index,
                message=msg,
                command=execution.command,
            )
        ]

    if emit_generic_pass and execution.exit_code == 0 and not execution.is_error:
        return [
            TestAttempt(
                test_id=norm_cmd,
                runner="generic",
                outcome="passed",
                turn_index=execution.turn_index,
                command=execution.command,
            )
        ]

    return []


def extract_pass_signal(execution: ToolExecution) -> RunnerPassSignal | None:
    """Return a ``RunnerPassSignal`` if ``execution`` represents a clean passing test run.

    Returns ``None`` if the execution failed, had errors, ran 0 tests, or is not a
    recognized passing test run.
    """
    if execution.is_error or (execution.exit_code is not None and execution.exit_code != 0):
        return None

    clean_output = strip_ansi(execution.output)
    runner = detect_runner(execution.command, clean_output)
    if runner == "generic":
        norm_cmd = normalize_command(execution.command)
        if execution.exit_code == 0 and norm_cmd and _is_command_execution(execution):
            return RunnerPassSignal(
                runner="generic",
                turn_index=execution.turn_index,
                command=execution.command,
                passed_count=1,
                failed_count=0,
                explicit_test_ids=[norm_cmd],
            )
        return None

    lines = clean_output.split("\n")
    passed_count = 0
    failed_count = 0
    passed_files: list[str] = []
    passed_packages: list[str] = []
    explicit_ids: list[str] = []

    if runner == "pytest":
        for raw_line in lines:
            line = _cap_line(raw_line).strip()
            if _PYTEST_SUMMARY_BAR_RE.match(line):
                for m in _COUNT_TOKEN_RE.finditer(line):
                    count = int(m.group(1))
                    kind = m.group(2).lower()
                    if kind == "passed":
                        passed_count = max(passed_count, count)
                    elif kind in ("failed", "error", "errors"):
                        failed_count += count
            else:
                node_m = _PYTEST_NODE_STATUS_RE.match(line)
                if node_m:
                    status = node_m.group(1) or node_m.group(4)
                    nid = _normalize_rel_path(node_m.group(2) or node_m.group(3))
                    if status == "PASSED":
                        explicit_ids.append(nid)
                    elif status in ("FAILED", "ERROR"):
                        failed_count += 1
        if failed_count > 0 or (passed_count == 0 and not explicit_ids):
            return None
        return RunnerPassSignal(
            runner="pytest",
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(explicit_ids)),
            failed_count=0,
            explicit_test_ids=explicit_ids,
        )

    if runner in ("vitest", "jest"):
        for raw_line in lines:
            line = _cap_line(raw_line)
            file_m = _JS_FILE_STATUS_RE.match(line)
            if file_m:
                marker = file_m.group(1)
                fpath = _normalize_rel_path(file_m.group(2))
                if marker in ("PASS", "✓", "✔"):
                    if fpath not in passed_files:
                        passed_files.append(fpath)
                elif marker in ("FAIL", "×", "✕", "✗"):
                    failed_count += 1
                continue
            if (
                _JS_TESTS_SUMMARY_RE.match(line)
                or _VITEST_TEST_FILES_RE.match(line)
                or _JEST_TEST_SUITES_RE.match(line)
            ):
                for m in _COUNT_TOKEN_RE.finditer(line):
                    count = int(m.group(1))
                    kind = m.group(2).lower()
                    if kind == "passed":
                        passed_count = max(passed_count, count)
                    elif kind in ("failed", "error", "errors"):
                        failed_count += count
        if failed_count > 0 or (passed_count == 0 and not passed_files):
            return None
        return RunnerPassSignal(
            runner=runner,
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(passed_files)),
            failed_count=0,
            passed_files=passed_files,
        )

    if runner == "go":
        for raw_line in lines:
            line = _cap_line(raw_line).strip()
            res_m = _GO_RESULT_RE.match(line)
            if res_m:
                if res_m.group(1) == "PASS":
                    explicit_ids.append(res_m.group(2))
                elif res_m.group(1) == "FAIL":
                    failed_count += 1
                continue
            ok_m = _GO_PKG_OK_RE.match(line)
            if ok_m:
                rest = ok_m.group(2) or ""
                if "[no test files]" not in rest:
                    passed_packages.append(ok_m.group(1))
                continue
            if _GO_PKG_FAIL_RE.match(line) or line == "FAIL":
                failed_count += 1
            elif line == "PASS":
                passed_count = max(passed_count, 1)
        if failed_count > 0 or (passed_count == 0 and not passed_packages and not explicit_ids):
            return None
        return RunnerPassSignal(
            runner="go",
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(passed_packages), len(explicit_ids)),
            failed_count=0,
            passed_packages=passed_packages,
            explicit_test_ids=explicit_ids,
        )

    return None


def extract_go_failed_packages(output: str) -> dict[str, str]:
    """Map failing Go test names to their enclosing Go package from ``FAIL <pkg>`` lines."""
    clean_output = strip_ansi(output)
    mapping: dict[str, str] = {}
    pending_tests: list[str] = []
    for raw_line in clean_output.split("\n"):
        line = _cap_line(raw_line)
        res_m = _GO_RESULT_RE.match(line)
        if res_m and res_m.group(1) == "FAIL":
            pending_tests.append(res_m.group(2))
            continue
        pkg_fail = _GO_PKG_FAIL_RE.match(line.strip())
        if pkg_fail:
            pkg = pkg_fail.group(1)
            for tname in pending_tests:
                mapping[tname] = pkg
            pending_tests.clear()
            continue
        if _GO_PKG_OK_RE.match(line.strip()):
            pending_tests.clear()
    return mapping


def parse_runner_output(
    execution: ToolExecution,
    *,
    emit_generic_pass: bool = False,
) -> list[TestAttempt]:
    """Parse a single ``ToolExecution`` into structured ``TestAttempt`` records."""
    clean_output = strip_ansi(execution.output)
    runner = detect_runner(execution.command, clean_output)

    if runner == "pytest":
        return _parse_pytest(execution, clean_output)
    if runner in ("vitest", "jest"):
        return _parse_js_runner(execution, clean_output, runner)
    if runner == "go":
        return _parse_go_test(execution, clean_output)
    return _parse_generic(execution, clean_output, emit_generic_pass=emit_generic_pass)


def parse_tool_executions(executions: Sequence[ToolExecution]) -> list[TestAttempt]:
    """Parse a sequence of ``ToolExecution`` records in order.

    Tracks failing generic commands so that a subsequent clean exit (``exit_code == 0``
    and ``not is_error``) of the exact same normalized command emits a ``passed``
    ``TestAttempt`` without polluting the ledger with unrelated shell commands.
    """
    attempts: list[TestAttempt] = []
    failed_generic_commands: set[str] = set()

    for execution in executions:
        clean_output = strip_ansi(execution.output)
        runner = detect_runner(execution.command, clean_output)
        if runner == "generic":
            norm_cmd = normalize_command(execution.command)
            is_known_failed = bool(norm_cmd and norm_cmd in failed_generic_commands)
            parsed = _parse_generic(
                execution,
                clean_output,
                emit_generic_pass=is_known_failed,
            )
            for item in parsed:
                if item.outcome in ("failed", "error"):
                    failed_generic_commands.add(item.test_id)
            attempts.extend(parsed)
        elif runner == "pytest":
            attempts.extend(_parse_pytest(execution, clean_output))
        elif runner in ("vitest", "jest"):
            attempts.extend(_parse_js_runner(execution, clean_output, runner))
        elif runner == "go":
            attempts.extend(_parse_go_test(execution, clean_output))

    return attempts


__all__ = [
    "RunnerPassSignal",
    "detect_runner",
    "extract_go_failed_packages",
    "extract_pass_signal",
    "extract_runner_invocation",
    "normalize_command",
    "parse_runner_output",
    "parse_tool_executions",
    "strip_ansi",
]
