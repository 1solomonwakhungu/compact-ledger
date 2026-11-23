"""Deterministic test runner output parsers for compact-ledger.

Parses tool/shell outputs from ``pytest``, ``vitest``, ``jest``, ``go test``,
and generic failing commands into structured ``TestAttempt`` records using only
the Python standard library and strictly linear-time (ReDoS-safe) scanning.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from compact_ledger.models import TestAttempt, ToolExecution, sanitize_unicode

# Single-pass, non-backtracking ANSI escape stripper:
# - CSI sequences: ESC [ params intermediates final
# - OSC sequences: ESC ] ... (BEL or ST)
# - Charset designations: ESC ( B, ESC ) 0, etc.
# - 2-byte ESC sequences including ESC 7 / ESC 8 (DECSC/DECRC)
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()#][0-9A-Za-z]|[@-Z\\-_78])"
)

# Cap per-line scanning length so adversarial multi-megabyte single lines stay fast.
_MAX_LINE_SCAN = 2048
_MAX_VALUE_LEN = 200
_MAX_MESSAGE_LEN = 240
_MAX_GENERIC_TEST_ID_LEN = 120

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

# Read-only inspection / probe commands that should never be detected as test runners
# from their stdout, and whose non-zero exits (e.g. grep miss, test -f) are not failures.
_NON_RUNNER_COMMANDS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "bat",
        "rg",
        "grep",
        "egrep",
        "fgrep",
        "ag",
        "ack",
        "echo",
        "printf",
        "sed",
        "awk",
        "jq",
        "ls",
        "find",
        "fd",
        "stat",
        "wc",
        "diff",
        "pwd",
        "which",
        "type",
        "test",
        "[",
        "true",
        "false",
        "systemctl",
        # Housekeeping whose non-zero exit says nothing about the code under test
        "mkdir",
        "rm",
        "cp",
        "mv",
        "touch",
        "chmod",
        "sleep",
        "du",
        "df",
        "sort",
        "uniq",
        "cut",
        "tr",
        "tee",
        "tree",
        "file",
        "date",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "printenv",
        "env",
        "ps",
        "kill",
        "lsof",
        "curl",
        "wget",
        "gh",
    }
)
_GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "diff",
        "status",
        "log",
        "show",
        "grep",
        "rev-parse",
        "ls-files",
        "branch",
        "tag",
        "describe",
        "blame",
        "cat-file",
        # Sync / navigation: a failed fetch or checkout is not a failing test
        "fetch",
        "pull",
        "push",
        "remote",
        "ls-remote",
        "ls-tree",
        "checkout",
        "switch",
        "restore",
        "stash",
        "config",
        "add",
        "worktree",
        "clone",
        "merge-base",
        "reflog",
        "shortlog",
        "check-ignore",
    }
)

_WRAPPER_SINGLE = frozenset({"env", "time", "nohup", "xvfb-run", "npx", "pnpx", "bunx"})
_WRAPPER_WITH_RUN = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch"})
_NODE_PKG_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SCOPE_AFFECTING_ENV_VARS = frozenset({"PYTEST_ADDOPTS"})
_CHDIR_COMMANDS = frozenset({"cd", "pushd", "popd"})
_CHDIR_FLAGS = frozenset({"-C", "--directory", "--rootdir", "--cwd", "--dir", "--root"})
_SCOPE_NARROWING_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "--project",
        "--changed",
        "--related",
    }
)

# Strictly linear-time regexes (no nested/overlapping quantifiers).
_PYTEST_SESSION_RE = re.compile(
    r"^={3,}\s*(?:test session starts|FAILURES|ERRORS|short test summary info)\s*={3,}$"
)
_PYTEST_SUMMARY_BAR_RE = re.compile(
    r"^(?:={3,}\s+)?\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
    r"[^=\n]*\bin\s+[0-9.]+s\b"
)
_PYTEST_COLLECT_ERROR_RE = re.compile(r"^ERROR\s+collecting\s+(\S+)")
_PYTEST_LOC_LINE_RE = re.compile(r"^([^\s:][^:\n]*\.py):(\d+):(.*)$")
_PYTEST_DID_NOT_RAISE_RE = re.compile(r"DID NOT RAISE\s+(.+)$")

# Single-pass keyword finder for linear-time expected/got/want extraction (zero quantifiers).
_KV_KEYWORD_RE = re.compile(r"\b(expected|want|wanted|got|actual|received)\b", re.IGNORECASE)

# Vitest / Jest strictly linear patterns.
_VITEST_BANNER_RE = re.compile(r"^\s*RUN\s+v\d+\.\d+")
_VITEST_TEST_FILES_RE = re.compile(r"^\s*Test Files\s+(.+)$")
_JEST_TEST_SUITES_RE = re.compile(r"^\s*Test Suites:\s+(.+)$")
_JS_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests:?\s+(.+)$")
# Filename must have NO spaces so test names like `✓ parses config.ts 1ms` never match as files.
_JS_FILE_STATUS_RE = re.compile(
    r"^\s*(PASS|FAIL|✓|✔|×|✕|✗)\s+([^\s(]+\.(?:[cm]?[jt]sx?|spec\.[jt]sx?|test\.[jt]sx?))"
    r"(?:\s|$)"
)
_VITEST_FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+([^\s>]+)\s+>\s+(.+)$")
_VITEST_SUITE_FAIL_RE = re.compile(r"^\s*FAIL\s+([^\s\[]+)\s+\[\s*\1\s*\]")
_JEST_BULLET_RE = re.compile(r"^\s*●\s+(.+)$")
_JEST_NON_FAILURE_BULLETS = (
    "Deprecation Warning",
    "Validation Warning",
    "Console",
    "Cannot log after tests are done",
)
_JS_EXPECTED_LINE_RE = re.compile(
    r"^\s*Expected(?:\s+(?:value|substring|pattern|length|number of calls))?:\s*(.+)$"
)
_JS_RECEIVED_LINE_RE = re.compile(
    r"^\s*Received(?:\s+(?:value|string|length|number of calls))?:\s*(.+)$"
)
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_VITEST_TO_OP_RE = re.compile(
    r"\s+to\s+(?:(?:deeply|strictly)\s+)?(?:be|equal|eql|match|contain)\s+",
    re.IGNORECASE,
)

# Go test strictly linear patterns.
_GO_RUN_RE = re.compile(r"^\s*===\s*(?:RUN|CONT|NAME)\s+(\S+)")
_GO_RESULT_RE = re.compile(r"^\s*---\s*(FAIL|PASS|SKIP):\s*(\S+)(?:\s+\([^)]*\))?")
_GO_FILE_LINE_RE = re.compile(r"^\s*([^\s:]+\.go):(\d+)(?::\d+)?:\s*(.*)$")
_GO_PKG_OK_RE = re.compile(r"^ok\s+(\S+)\s+(?:\d+(?:\.\d+)?s|\(cached\)|\[no test)")
_GO_PKG_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+(?:\d+(?:\.\d+)?s|\[(?:build|setup) failed\])")
_GO_KV_LINE_RE = re.compile(r"^\s*(expected|want|wanted|actual|got)\s*:\s*(.+)$", re.IGNORECASE)

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
_COUNT_TOKEN_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b",
    re.IGNORECASE,
)

_ASSERT_OPS: tuple[str, ...] = (
    " == ",
    " != ",
    " <= ",
    " >= ",
    " is not ",
    " not in ",
    " < ",
    " > ",
    " is ",
    " in ",
)


@dataclass(slots=True)
class RunnerInvocation:
    """Parsed shell runner invocation with explicit scope confidence."""

    runner: str | None
    args: list[str] = field(default_factory=list)
    scope_known: bool = False
    has_chdir: bool = False


@dataclass(slots=True)
class RunnerPassSignal:
    """Structured summary of a clean passing runner execution for scope-aware resolution."""

    runner: str
    turn_index: int
    command: str | None = None
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    deselected_count: int = 0
    passed_files: list[str] = field(default_factory=list)
    passed_packages: list[str] = field(default_factory=list)
    explicit_test_ids: list[str] = field(default_factory=list)
    is_command_only_pass: bool = False


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


def _bounded_generic_test_id(command: str | None) -> str:
    """Return a normalized, length-bounded test_id for a generic shell command."""
    if not command:
        return ""
    clean = strip_ansi(command).strip()
    if not clean:
        return ""
    first_line = " ".join(clean.split("\n", 1)[0].split())
    full_norm = " ".join(clean.split())
    if len(full_norm) <= _MAX_GENERIC_TEST_ID_LEN:
        return full_norm
    digest = hashlib.sha256(full_norm.encode("utf-8")).hexdigest()[:8]
    prefix_budget = _MAX_GENERIC_TEST_ID_LEN - len(digest) - 6
    prefix = first_line[:prefix_budget].rstrip()
    return f"{prefix}... [{digest}]"


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


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Remove shell I/O redirection operators and their target filenames from argv tokens."""
    cleaned: list[str] = []
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok in (">", ">>", "<", "<<", "<<<", "&>", "&>>", ">&", "<&"):
            idx += 2 if idx + 1 < n else 1
            continue
        if len(tok) >= 2 and tok[0].isdigit() and tok[1] in (">", "<"):
            if tok in ("2>", "2>>", "1>", "1>>", "0<"):
                idx += 2 if idx + 1 < n else 1
            else:
                idx += 1
            continue
        if tok.startswith((">", "<", "&>")):
            idx += 1
            continue
        cleaned.append(tok)
        idx += 1
    return cleaned


def _split_shell_subcommands(command: str) -> list[list[str]]:
    """Split a shell command into subcommand argv lists, respecting quotes and groups."""
    # Collapse POSIX backslash-newline line continuations before lexing.
    prepped = command.replace("\\\r\n", " ").replace("\\\n", " ")

    subcommands: list[list[str]] = []
    try:
        lexer = shlex.shlex(prepped, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        raw_tokens = list(lexer)
    except ValueError:
        raw_tokens = prepped.replace("\n", " ; ").split()

    # Re-join `2>` + `&` + `1` produced when splitting `2>&1` and normalize `{`/`}` group tokens.
    tokens: list[str] = []
    idx = 0
    n = len(raw_tokens)
    while idx < n:
        tok = raw_tokens[idx]
        if (
            tok.endswith((">", "<"))
            and idx + 2 < n
            and raw_tokens[idx + 1] == "&"
            and (raw_tokens[idx + 2].isdigit() or raw_tokens[idx + 2] == "-")
        ):
            tokens.append(tok + "&" + raw_tokens[idx + 2])
            idx += 3
            continue
        if tok == "&" and idx + 1 < n and raw_tokens[idx + 1].startswith(">"):
            tokens.append("&" + raw_tokens[idx + 1])
            idx += 2
            continue
        # Separate standalone brace-group tokens `{` and `}`.
        if tok in ("{", "}"):
            tokens.append(";")
            idx += 1
            continue
        tokens.append(tok)
        idx += 1

    separators = {"&&", "||", ";", "|", "&", "(", ")", "\n"}
    current: list[str] = []
    for tok in tokens:
        if tok in separators or ("\n" in tok and not tok.strip()):
            if current:
                cleaned = _strip_redirections(current)
                if cleaned:
                    subcommands.append(cleaned)
                current = []
        else:
            current.append(tok)
    if current:
        cleaned = _strip_redirections(current)
        if cleaned:
            subcommands.append(cleaned)
    return subcommands


def _unwrap_runner_tokens(tokens: list[str]) -> tuple[list[str], bool, bool]:
    """Strip env vars and wrappers. Returns ``(remaining, has_chdir, has_scope_env)``."""
    idx = 0
    n = len(tokens)
    has_chdir = False
    has_scope_env = False
    while idx < n:
        tok = tokens[idx]
        if _ENV_ASSIGN_RE.match(tok):
            var_name = tok.split("=", 1)[0]
            if var_name in _SCOPE_AFFECTING_ENV_VARS:
                has_scope_env = True
            idx += 1
            continue
        base = PurePosixPath(tok).name.lower()
        if base == "timeout":
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if flag in ("-s", "--signal", "-k", "--kill-after") and idx < n:
                    idx += 1
            if idx < n:
                idx += 1
            continue
        if base == "nice":
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if flag in ("-n", "--adjustment") and idx < n:
                    idx += 1
            continue
        if base in _WRAPPER_SINGLE:
            idx += 1
            if base == "env":
                while idx < n:
                    cur = tokens[idx]
                    if cur in ("-C", "--chdir") and idx + 1 < n:
                        has_chdir = True
                        idx += 2
                        continue
                    if cur.startswith(("-C=", "--chdir=")):
                        has_chdir = True
                        idx += 1
                        continue
                    if cur in ("-u", "--unset") and idx + 1 < n:
                        idx += 2
                        continue
                    if _ENV_ASSIGN_RE.match(cur):
                        if cur.split("=", 1)[0] in _SCOPE_AFFECTING_ENV_VARS:
                            has_scope_env = True
                        idx += 1
                        continue
                    if cur.startswith("-"):
                        idx += 1
                        continue
                    break
            elif base in ("npx", "pnpx", "bunx"):
                while idx < n and tokens[idx].startswith("-"):
                    flag = tokens[idx]
                    idx += 1
                    if flag in ("-p", "--package", "-c", "--call") and idx < n:
                        idx += 1
            continue
        if base in _WRAPPER_WITH_RUN and idx + 1 < n and tokens[idx + 1] == "run":
            idx += 2
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                if flag in ("--directory", "-C", "--project") or flag.startswith(
                    ("--directory=", "-C=", "--project=")
                ):
                    has_chdir = True
                idx += 1
                if (
                    flag
                    in (
                        "-w",
                        "--with",
                        "--directory",
                        "-C",
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
            idx += 2
            if idx < n and tokens[idx] == "--":
                idx += 1
            continue
        if base in ("pnpm", "yarn", "bun") and idx + 1 < n:
            if tokens[idx + 1] in ("-C", "--dir", "--cwd") and idx + 2 < n:
                has_chdir = True
                idx += 3
                continue
            if tokens[idx + 1].lower() in ("vitest", "jest"):
                idx += 1
                continue
        break
    return tokens[idx:], has_chdir, has_scope_env


def _extract_python_module_pytest(rest: list[str]) -> list[str] | None:
    """Given args after `python[3]`, return pytest args if invoking `-m pytest`."""
    idx = 0
    n = len(rest)
    while idx < n:
        tok = rest[idx]
        if tok == "-m":
            if idx + 1 < n and rest[idx + 1] in ("pytest", "py.test"):
                return rest[idx + 2 :]
            return None
        if tok in ("-X", "-W", "--check-hash-based-pycs"):
            idx += 2
            continue
        if tok.startswith("-") and tok not in ("-c", "--"):
            idx += 1
            continue
        return None
    return None


def _is_single_subcommand_probe(sub: list[str]) -> bool:
    """Return True if one shell subcommand is a read-only probe/inspector."""
    idx = 0
    while idx < len(sub) and _ENV_ASSIGN_RE.match(sub[idx]):
        idx += 1
    if idx >= len(sub):
        return True
    raw_exe = PurePosixPath(sub[idx]).name.lower()
    # Package/env runners like `hatch run test` or `pdm run test` are NEVER POSIX `test` probes.
    if raw_exe in _WRAPPER_WITH_RUN or raw_exe in _NODE_PKG_MANAGERS:
        return False
    if raw_exe in _CHDIR_COMMANDS:
        return True
    if raw_exe in _NON_RUNNER_COMMANDS:
        return True
    if raw_exe == "git":
        g_idx = idx + 1
        while g_idx < len(sub) and sub[g_idx].startswith("-"):
            flag = sub[g_idx]
            g_idx += 1
            if flag in ("-C", "-c", "--git-dir", "--work-tree") and g_idx < len(sub):
                g_idx += 1
        subcmd = sub[g_idx].lower() if g_idx < len(sub) else ""
        return subcmd in _GIT_READONLY_SUBCOMMANDS
    return False


def _is_non_runner_command(command: str | None) -> bool:
    """Return True iff EVERY subcommand in ``command`` is a read-only probe/inspector."""
    if not command:
        return False
    subs = _split_shell_subcommands(strip_ansi(command))
    if not subs:
        return False
    return all(_is_single_subcommand_probe(sub) for sub in subs)


def parse_runner_invocation(command: str | None) -> RunnerInvocation:
    """Parse a shell command into a ``RunnerInvocation`` with scope certainty."""
    if not command:
        return RunnerInvocation(runner=None, args=[], scope_known=False, has_chdir=False)

    clean_cmd = strip_ansi(command)
    subs = _split_shell_subcommands(clean_cmd)
    has_chdir = False
    has_scope_env = False
    found: list[tuple[str, list[str]]] = []

    for sub in subs:
        unwrapped, wrapper_chdir, wrapper_env = _unwrap_runner_tokens(sub)
        if wrapper_chdir:
            has_chdir = True
        if wrapper_env:
            has_scope_env = True
        if not unwrapped:
            continue
        exe = PurePosixPath(unwrapped[0]).name.lower()
        rest = unwrapped[1:]
        if exe in _CHDIR_COMMANDS:
            has_chdir = True
            continue
        if exe in ("pytest", "py.test"):
            found.append(("pytest", rest))
        elif exe.startswith("python"):
            py_args = _extract_python_module_pytest(rest)
            if py_args is not None:
                found.append(("pytest", py_args))
        elif exe == "vitest":
            found.append(("vitest", rest))
        elif exe == "jest":
            found.append(("jest", rest))
        elif exe == "go" and rest and rest[0] == "test":
            found.append(("go", rest[1:]))

    if not found:
        return RunnerInvocation(runner=None, args=[], scope_known=False, has_chdir=has_chdir)

    runner, args = found[-1]
    has_scope_narrowing = False
    for arg in args:
        if arg in _CHDIR_FLAGS or any(arg.startswith(f + "=") for f in _CHDIR_FLAGS):
            has_chdir = True
        if arg in _SCOPE_NARROWING_FLAGS or any(
            arg.startswith(f + "=") for f in _SCOPE_NARROWING_FLAGS
        ):
            has_scope_narrowing = True

    if runner == "vitest" and args and args[0] == "related":
        has_scope_narrowing = True

    scope_known = (
        (not has_chdir) and (not has_scope_env) and (not has_scope_narrowing) and (len(found) == 1)
    )
    return RunnerInvocation(runner=runner, args=args, scope_known=scope_known, has_chdir=has_chdir)


def extract_runner_invocation(command: str | None) -> tuple[str | None, list[str]]:
    """Return ``(runner, args_after_runner)`` from a shell command, if recognized."""
    inv = parse_runner_invocation(command)
    return inv.runner, inv.args


def _parse_pytest_node_status_line(stripped: str) -> tuple[str, str] | None:
    """Linear-time parser for pytest verbose status lines (``PASSED node`` or ``node PASSED``)."""
    parts = stripped.split()
    if len(parts) < 2:
        return None
    first, second = parts[0], parts[1]
    if first in ("PASSED", "FAILED", "ERROR") and "::" in second:
        return first, second
    if second in ("PASSED", "FAILED", "ERROR") and "::" in first:
        return second, first
    return None


def detect_runner(command: str | None, output: str) -> str:
    """Detect the test runner from command and output (pytest, vitest, jest, go, generic)."""
    inv = parse_runner_invocation(command)
    if inv.runner is not None:
        return inv.runner

    if _is_non_runner_command(command):
        return "generic"

    clean_output = strip_ansi(output)
    if not clean_output:
        return "generic"

    lines = clean_output.split("\n")
    sample = lines if len(lines) <= 400 else lines[:200] + lines[-200:]

    has_pytest = False
    has_vitest = False
    has_jest = False
    has_go = False

    for raw_line in sample:
        line = _cap_line(raw_line)
        stripped = line.strip()
        if not stripped:
            continue
        if (
            _PYTEST_SESSION_RE.match(stripped)
            or _PYTEST_SUMMARY_BAR_RE.match(stripped)
            or _PYTEST_COLLECT_ERROR_RE.match(stripped)
        ):
            has_pytest = True
            break
        node_status = _parse_pytest_node_status_line(stripped)
        if node_status is not None and ".py::" in node_status[1]:
            has_pytest = True
            break
        if (
            _VITEST_BANNER_RE.match(line)
            or _VITEST_TEST_FILES_RE.match(line)
            or _VITEST_FAIL_LINE_RE.match(line)
            or _VITEST_SUITE_FAIL_RE.match(line)
        ):
            has_vitest = True
        if _JEST_TEST_SUITES_RE.match(line) or stripped.startswith("expect(received)."):
            has_jest = True
        if (
            _GO_RESULT_RE.match(line)
            or _GO_RUN_RE.match(line)
            or _GO_PKG_OK_RE.match(stripped)
            or _GO_PKG_FAIL_RE.match(stripped)
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


def _strip_kv_val_edges(raw: str) -> str:
    val = raw.strip()
    if val.startswith((":", "=")):
        val = val[1:].strip()
    if val.endswith((",", ";")):
        val = val[:-1].strip()
    return val


def _extract_expected_actual_from_text(text: str) -> tuple[str | None, str | None]:
    """Strictly linear-time extraction of ``(expected, actual)`` from assertion text."""
    norm = " ".join(_cap_line(text).split())
    if not norm:
        return None, None

    lower = norm.lower()
    ae_idx = lower.find("assertionerror:")
    if ae_idx != -1:
        exp_idx = lower.find("expected ", ae_idx)
        if exp_idx != -1:
            after_exp = norm[exp_idx + len("expected ") :]
            op_m = _VITEST_TO_OP_RE.search(after_exp)
            if op_m:
                act_part = after_exp[: op_m.start()].strip()
                exp_part = after_exp[op_m.end() :].strip()
                comment_idx = exp_part.find(" // ")
                if comment_idx != -1:
                    exp_part = exp_part[:comment_idx].strip()
                return _clean_value(exp_part), _clean_value(act_part)

    matches = list(_KV_KEYWORD_RE.finditer(norm))
    if len(matches) >= 2:
        expected_words = {"expected", "want", "wanted"}
        actual_words = {"got", "actual", "received"}
        for i in range(len(matches) - 1):
            m1, m2 = matches[i], matches[i + 1]
            k1 = m1.group(1).lower()
            k2 = m2.group(1).lower()
            if (k1 in expected_words and k2 in actual_words) or (
                k1 in actual_words and k2 in expected_words
            ):
                v1_raw = norm[m1.end() : m2.start()]
                v1_clean = _strip_kv_val_edges(v1_raw)
                if v1_clean.lower().endswith(" but"):
                    v1_clean = v1_clean[:-4].strip()
                v2_end = matches[i + 2].start() if i + 2 < len(matches) else len(norm)
                v2_clean = _strip_kv_val_edges(norm[m2.end() : v2_end])
                if v1_clean and v2_clean:
                    if k1 in expected_words:
                        return _clean_value(v1_clean), _clean_value(v2_clean)
                    return _clean_value(v2_clean), _clean_value(v1_clean)

    return None, None


def _parse_pytest_assert_comparison(line: str) -> tuple[str, str, str] | None:
    """Linear-time parser for `assert <left> <op> <right>`."""
    norm = " ".join(_cap_line(line).split())
    if not norm.startswith("assert "):
        return None
    body = norm[len("assert ") :]
    for op in _ASSERT_OPS:
        pos = body.find(op)
        if pos > 0:
            left = body[:pos].strip()
            right = body[pos + len(op) :].strip()
            if left and right:
                return left, op.strip(), right
    return None


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

        dnr = _PYTEST_DID_NOT_RAISE_RE.search(stripped)
        if dnr:
            expected = expected or _clean_value(f"raises {dnr.group(1).strip()}")
            actual = actual or "DID NOT RAISE"
            message = message or _clean_message(stripped)
            continue

        cmp_parsed = _parse_pytest_assert_comparison(stripped)
        if cmp_parsed is not None:
            left, op, right = cmp_parsed
            if actual is None:
                actual = _clean_value(left)
            if expected is None:
                expected = _clean_value(right if op in ("==", "is") else f"{op} {right}")
            message = message or _clean_message(stripped)
            continue

        exp_t, act_t = _extract_expected_actual_from_text(stripped)
        if exp_t is not None or act_t is not None:
            expected = expected or exp_t
            actual = actual or act_t
            message = message or _clean_message(stripped)
            continue

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


def _parse_pytest_fail_header(stripped: str) -> str | None:
    """Linear-time parser for pytest `___ test_name ___` block headers."""
    if not (stripped.startswith("___") and stripped.endswith("___")):
        return None
    inner = stripped.strip("_").strip()
    return inner if inner else None


def _split_pytest_summary_node_and_detail(rest: str) -> tuple[str, str | None]:
    """Split ``node_id - detail`` without truncating parametrized IDs containing `` - ``."""
    depth = 0
    n = len(rest)
    for i in range(n):
        ch = rest[i]
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        elif depth == 0 and rest.startswith(" - ", i):
            return rest[:i].strip(), rest[i + 3 :].strip()
    return rest.strip(), None


def _normalize_pytest_block_key(header_name: str) -> str:
    h = header_name.strip()
    if h.startswith("ERROR at ") and " of " in h:
        h = h.split(" of ", 1)[1].strip()
    return h


def _pytest_header_matches_node(
    header_name: str,
    node_id: str,
    *,
    block_file_hint: str | None = None,
    block_outcome: str | None = None,
    summary_outcome: str | None = None,
) -> bool:
    """Match a pytest failure section header to a full node ID without cross-file swaps."""
    if block_outcome and summary_outcome and block_outcome != summary_outcome:
        return False

    h = _normalize_pytest_block_key(header_name)

    if block_file_hint and "::" in node_id:
        node_file = _normalize_rel_path(node_id.split("::", 1)[0])
        hint_norm = _normalize_rel_path(block_file_hint)
        if not (
            node_file == hint_norm
            or node_file.endswith("/" + hint_norm)
            or hint_norm.endswith("/" + node_file)
        ):
            return False

    if h == node_id:
        return True
    if "::" in node_id:
        tail = node_id.split("::", 1)[1]
        if tail == h or tail.replace("::", ".") == h:
            return True
        last_part = node_id.rsplit("::", 1)[-1]
        if last_part == h:
            return True
    return False


def _parse_pytest(execution: ToolExecution, clean_output: str) -> list[TestAttempt]:
    """Parse pytest output into TestAttempt records in linear time."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    blocks: list[dict[str, str | None]] = []
    in_section: str | None = None
    current_block: dict[str, object] | None = None

    summary_failures: list[tuple[str, str, str | None]] = []
    verbose_passes: list[str] = []
    verbose_failures: list[tuple[str, str]] = []
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
            header_name = _parse_pytest_fail_header(stripped)
            if header_name is not None:
                _flush_block()
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
                if line.startswith(("E   ", "E\t")):
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
            if stripped.startswith(("FAILED ", "ERROR ")):
                status_word, rest = stripped.split(None, 1)
                outcome = "error" if status_word == "ERROR" else "failed"
                node_raw, detail = _split_pytest_summary_node_and_detail(rest)
                node_id = _normalize_rel_path(node_raw)
                if node_id:
                    summary_failures.append((outcome, node_id, detail))
            elif stripped.startswith("PASSED "):
                node_id = _normalize_rel_path(stripped.split(None, 1)[1].strip())
                if node_id:
                    verbose_passes.append(node_id)
            continue

        col_m = _PYTEST_COLLECT_ERROR_RE.match(stripped)
        if col_m:
            collect_errors.append((_normalize_rel_path(col_m.group(1)), None))
            continue

        if stripped.startswith(("FAILED ", "ERROR ")) and " - " in stripped:
            status_word, rest = stripped.split(None, 1)
            node_raw, detail = _split_pytest_summary_node_and_detail(rest)
            if "::" in node_raw:
                outcome = "error" if status_word == "ERROR" else "failed"
                summary_failures.append((outcome, _normalize_rel_path(node_raw), detail))
                continue

        node_status = _parse_pytest_node_status_line(stripped)
        if node_status is not None:
            status_word, raw_node = node_status
            node_id = _normalize_rel_path(raw_node)
            if status_word == "PASSED":
                verbose_passes.append(node_id)
            elif status_word == "FAILED":
                verbose_failures.append(("failed", node_id))
            elif status_word == "ERROR":
                verbose_failures.append(("error", node_id))

    _flush_block()

    # Index detailed blocks by normalized header key for O(1) summary matching.
    blocks_by_key: dict[str, list[int]] = defaultdict(list)
    for idx, blk in enumerate(blocks):
        key = _normalize_pytest_block_key(blk["header"] or "")
        blocks_by_key[key].append(idx)

    attempts: list[TestAttempt] = []
    seen_fail_ids: set[str] = set()
    used_blocks: set[int] = set()

    for outcome, node_id, detail in summary_failures:
        candidate_keys: list[str] = [node_id]
        if "::" in node_id:
            tail = node_id.split("::", 1)[1]
            candidate_keys.append(tail)
            dotted = tail.replace("::", ".")
            if dotted != tail:
                candidate_keys.append(dotted)
            last_part = node_id.rsplit("::", 1)[-1]
            if last_part not in candidate_keys:
                candidate_keys.append(last_part)

        matched_idx: int | None = None
        for key in candidate_keys:
            for idx in blocks_by_key.get(key, ()):
                if idx in used_blocks:
                    continue
                blk = blocks[idx]
                if _pytest_header_matches_node(
                    blk["header"] or "",
                    node_id,
                    block_file_hint=blk["file_hint"],
                    block_outcome=blk["outcome"],
                    summary_outcome=outcome,
                ):
                    matched_idx = idx
                    break
            if matched_idx is not None:
                break

        if matched_idx is None:
            for key in candidate_keys:
                for idx in blocks_by_key.get(key, ()):
                    if idx in used_blocks or blocks[idx]["file_hint"] is not None:
                        continue
                    blk = blocks[idx]
                    if _pytest_header_matches_node(
                        blk["header"] or "",
                        node_id,
                        block_outcome=blk["outcome"],
                        summary_outcome=outcome,
                    ):
                        matched_idx = idx
                        break
                if matched_idx is not None:
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

    # Index verbose failures by tail for O(1) unmatched-block lookup.
    verbose_fail_by_tail: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for v_out, v_node in verbose_failures:
        verbose_fail_by_tail[v_node].append((v_out, v_node))
        if "::" in v_node:
            tail = v_node.split("::", 1)[1]
            verbose_fail_by_tail[tail].append((v_out, v_node))
            verbose_fail_by_tail[tail.replace("::", ".")].append((v_out, v_node))
            verbose_fail_by_tail[v_node.rsplit("::", 1)[-1]].append((v_out, v_node))

    for idx, blk in enumerate(blocks):
        if idx in used_blocks:
            continue
        header = blk["header"] or ""
        col_m = _PYTEST_COLLECT_ERROR_RE.match(header)
        if col_m:
            node_id = _normalize_rel_path(col_m.group(1))
        else:
            node_id = ""
            h_key = _normalize_pytest_block_key(header)
            for v_out, v_node in verbose_fail_by_tail.get(h_key, ()):
                if _pytest_header_matches_node(
                    header,
                    v_node,
                    block_file_hint=blk["file_hint"],
                    block_outcome=blk["outcome"],
                    summary_outcome=v_out,
                ):
                    node_id = v_node
                    break
            if not node_id:
                clean_header = h_key
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

    if not attempts and (
        (execution.exit_code is not None and execution.exit_code != 0) or execution.is_error
    ):
        for raw_line in lines:
            line = _cap_line(raw_line).strip()
            loc_m = _PYTEST_LOC_LINE_RE.match(line)
            if loc_m and loc_m.group(3).strip():
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


def _parse_js_location_token(raw_token: str) -> tuple[str, str] | None:
    """Linear-time parser for a single stack-frame token like `(src/a.test.ts:42:18)`."""
    tok = raw_token.strip("()<>[]❯,")
    if not tok or ":" not in tok:
        return None
    parts = tok.split(":")
    if len(parts) < 2:
        return None
    # Walk from left to find `<file>.<js/ts ext>:<digits>`
    for idx in range(len(parts) - 1):
        candidate_file = ":".join(parts[: idx + 1])
        candidate_line = parts[idx + 1]
        if candidate_line.isdigit() and candidate_file.lower().endswith(_JS_EXTS):
            return _normalize_rel_path(candidate_file), candidate_line
    return None


def _extract_js_location(lines: list[str], preferred_file: str | None) -> str | None:
    """Strictly linear-time extraction of ``file:line`` from a Vitest/Jest failure block."""
    first_non_module: str | None = None
    for raw in lines:
        line = _cap_line(raw)
        if ":" not in line:
            continue
        for tok in line.split():
            if ":" not in tok:
                continue
            parsed = _parse_js_location_token(tok)
            if parsed is None:
                continue
            fpath, lineno = parsed
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

        exp_m = _JS_EXPECTED_LINE_RE.match(line)
        if exp_m and expected is None:
            expected = _clean_value(exp_m.group(1))
            continue
        rec_m = _JS_RECEIVED_LINE_RE.match(line)
        if rec_m and actual is None:
            actual = _clean_value(rec_m.group(1))
            continue

        if stripped.startswith(("- Expected", "+ Received")):
            in_diff = True
            continue
        if in_diff:
            if stripped.startswith("- ") and diff_expected is None:
                diff_expected = _clean_value(stripped[2:])
                continue
            if stripped.startswith("+ ") and diff_received is None:
                diff_received = _clean_value(stripped[2:])
                continue

        if "AssertionError:" in stripped:
            exp_t, act_t = _extract_expected_actual_from_text(stripped)
            if exp_t is not None or act_t is not None:
                expected = expected or exp_t
                actual = actual or act_t
                message = message or _clean_message(stripped)
                continue

        if message is None and stripped.startswith(
            (
                "expect(",
                "AssertionError:",
                "Error:",
                "TypeError:",
                "ReferenceError:",
                "RangeError:",
                "SyntaxError:",
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


def _strip_trailing_js_duration(text: str) -> str:
    """Linear-time removal of trailing Jest/Vitest timing like `8ms`, `1.2s`, `(5 ms)`."""
    parts = text.split()
    if len(parts) < 2:
        return " ".join(parts)
    last = parts[-1]
    # Form 1: `(5 ms)` -> last two tokens are `(<digits>` and `ms)` / `s)`
    if (
        len(parts) >= 3
        and last in ("ms)", "s)")
        and parts[-2].startswith("(")
        and parts[-2][1:].isdigit()
    ):
        return " ".join(parts[:-2])
    # Form 2: `(5ms)` or `5ms` or `5s`
    inner = last[1:-1] if (last.startswith("(") and last.endswith(")")) else last
    if inner.endswith("ms") and inner[:-2].isdigit():
        return " ".join(parts[:-1])
    if inner.endswith("s") and inner[:-1].isdigit():
        return " ".join(parts[:-1])
    return " ".join(parts)


def _parse_js_inline_marker(stripped: str, markers: tuple[str, ...]) -> str | None:
    """Strictly linear-time parser for `× test name 12ms` / `✓ test name (5 ms)` lines."""
    for mk in markers:
        if stripped.startswith(mk):
            rest = stripped[len(mk) :].strip()
            if not rest:
                return None
            cleaned = _strip_trailing_js_duration(rest)
            return cleaned if cleaned else None
    return None


def _js_output_has_failure_markers(lines: list[str]) -> bool:
    """Return True if Jest/Vitest output explicitly reports failures."""
    for raw_line in lines:
        line = _cap_line(raw_line)
        if _VITEST_FAIL_LINE_RE.match(line) or _VITEST_SUITE_FAIL_RE.match(line):
            return True
        file_m = _JS_FILE_STATUS_RE.match(line) if " > " not in line else None
        if file_m and file_m.group(1) in ("FAIL", "×", "✕", "✗"):
            return True
        if (
            _JS_TESTS_SUMMARY_RE.match(line)
            or _VITEST_TEST_FILES_RE.match(line)
            or _JEST_TEST_SUITES_RE.match(line)
        ):
            for m in _COUNT_TOKEN_RE.finditer(line):
                if m.group(2).lower() in ("failed", "error", "errors") and int(m.group(1)) > 0:
                    return True
    return False


def _parse_js_runner(execution: ToolExecution, clean_output: str, runner: str) -> list[TestAttempt]:
    """Parse Vitest or Jest output into TestAttempt records."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    # Treat Claude Code clean runs (exit_code=None, is_error=False) with no failure markers
    # as passing so stray `●` warnings (e.g. TCPSERVERWRAP, Cannot log after tests are done)
    # never become phantom test failures.
    run_looks_clean = (
        execution.exit_code in (0, None)
        and not execution.is_error
        and not _js_output_has_failure_markers(lines)
    )

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

        if (
            _VITEST_TEST_FILES_RE.match(line)
            or _JEST_TEST_SUITES_RE.match(line)
            or _JS_TESTS_SUMMARY_RE.match(line)
            or (stripped.startswith("⎯") and len(stripped) >= 6)
        ):
            _flush_js_block()
            continue

        vf_m = _VITEST_FAIL_LINE_RE.match(line)
        if vf_m:
            _flush_js_block()
            fpath = _normalize_rel_path(vf_m.group(1))
            current_file = fpath
            current_block_file = fpath
            current_block_id = _normalize_js_test_id(fpath, vf_m.group(2))
            current_block_lines = []
            continue

        vsf_m = _VITEST_SUITE_FAIL_RE.match(line)
        if vsf_m:
            _flush_js_block()
            fpath = _normalize_rel_path(vsf_m.group(1))
            current_file = fpath
            current_block_file = fpath
            current_block_id = fpath
            current_block_lines = []
            continue

        file_m = _JS_FILE_STATUS_RE.match(line) if " > " not in line else None
        if file_m:
            _flush_js_block()
            current_file = _normalize_rel_path(file_m.group(2))
            continue

        bullet_m = _JEST_BULLET_RE.match(line)
        if bullet_m:
            _flush_js_block()
            bullet_title = bullet_m.group(1).strip()
            if not run_looks_clean and not bullet_title.startswith(_JEST_NON_FAILURE_BULLETS):
                current_block_file = current_file
                current_block_id = _normalize_js_test_id(current_file, bullet_title)
                current_block_lines = []
            continue

        if current_block_id is not None:
            current_block_lines.append(line)
            continue

        cross_name = _parse_js_inline_marker(stripped, ("× ", "✕ ", "✗ "))
        if cross_name is not None:
            inline_cross_failures.append((current_file, cross_name))
            continue

        check_name = _parse_js_inline_marker(stripped, ("✓ ", "✔ "))
        if check_name is not None:
            tid = _normalize_js_test_id(current_file, check_name)
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


def _qualify_go_test_id(test_name: str, pkg: str | None) -> str:
    if pkg:
        return f"{pkg}::{test_name}"
    return test_name


def _parse_go_test(execution: ToolExecution, clean_output: str) -> list[TestAttempt]:
    """Parse ``go test`` output (verbose, non-verbose, parallel) into TestAttempt records."""
    lines = clean_output.split("\n")
    turn_index = execution.turn_index
    command = execution.command

    test_logs: dict[str, list[tuple[str | None, str]]] = {}
    failed_tests: list[str] = []
    passed_tests: list[str] = []
    failed_set: set[str] = set()
    passed_set: set[str] = set()
    build_errors: list[tuple[str, str]] = []

    test_package: dict[str, str] = {}
    pending_pkg_tests: list[str] = []

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
            pending_pkg_tests.append(tname)
            if status == "FAIL" and tname not in failed_set:
                failed_set.add(tname)
                failed_tests.append(tname)
            elif status == "PASS" and tname not in passed_set:
                passed_set.add(tname)
                passed_tests.append(tname)
            continue

        pkg_ok = _GO_PKG_OK_RE.match(stripped)
        if pkg_ok:
            pkg = pkg_ok.group(1)
            for tname in pending_pkg_tests:
                test_package.setdefault(tname, pkg)
            pending_pkg_tests.clear()
            active_test = None
            in_result_for = None
            continue

        pkg_fail = _GO_PKG_FAIL_RE.match(stripped)
        if pkg_fail:
            pkg = pkg_fail.group(1)
            for tname in pending_pkg_tests:
                test_package.setdefault(tname, pkg)
            pending_pkg_tests.clear()
            active_test = None
            in_result_for = None
            continue

        if stripped in ("PASS", "FAIL"):
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

        if target_test is not None and line.startswith(("    ", "\t")):
            test_logs.setdefault(target_test, []).append((None, stripped))

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
            kv_m = _GO_KV_LINE_RE.match(text)
            if kv_m:
                key = kv_m.group(1).lower()
                val = _clean_value(kv_m.group(2))
                if key in ("expected", "want", "wanted") and expected is None:
                    expected = val
                elif key in ("actual", "got") and actual is None:
                    actual = val
                continue
            if message is None and not text.startswith(("Error Trace:", "Test:", "Messages:")):
                message = _clean_message(text)

        if message is None and expected is not None and actual is not None:
            message = _clean_message(f"got {actual}, want {expected}")

        attempts.append(
            TestAttempt(
                test_id=_qualify_go_test_id(tname, test_package.get(tname)),
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

    for tname in passed_tests:
        if tname not in failed_set:
            attempts.append(
                TestAttempt(
                    test_id=_qualify_go_test_id(tname, test_package.get(tname)),
                    runner="go",
                    outcome="passed",
                    turn_index=turn_index,
                    command=command,
                )
            )

    return attempts


def _extract_last_meaningful_error_line(clean_output: str) -> str | None:
    """Extract the last meaningful error line from command output."""
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

    tail = non_empty[-25:]
    for candidate in reversed(tail):
        if _ERROR_KEYWORD_RE.search(candidate):
            return _clean_message(candidate)
    return _clean_message(tail[-1])


def _is_command_execution(execution: ToolExecution) -> bool:
    if execution.command and execution.command.strip():
        return True
    return execution.tool_name.lower() in _SHELL_TOOL_NAMES


def _build_command_failure_attempt(
    execution: ToolExecution,
    clean_output: str,
    *,
    runner: str,
    outcome: str = "failed",
) -> list[TestAttempt]:
    test_id = _bounded_generic_test_id(execution.command)
    if not test_id:
        return []
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
            test_id=test_id,
            runner=runner,
            outcome="error" if outcome == "error" else "failed",
            turn_index=execution.turn_index,
            message=msg,
            command=execution.command,
        )
    ]


def _parse_generic(
    execution: ToolExecution,
    clean_output: str,
    *,
    emit_generic_pass: bool = False,
) -> list[TestAttempt]:
    """Fallback parser for non-test-runner failing (and optionally passing) CLI commands."""
    if not _is_command_execution(execution):
        return []
    if _is_non_runner_command(execution.command):
        return []

    test_id = _bounded_generic_test_id(execution.command)
    if not test_id:
        return []

    is_failure = (
        execution.exit_code is not None and execution.exit_code != 0
    ) or execution.is_error
    if is_failure:
        return _build_command_failure_attempt(execution, clean_output, runner="generic")

    if emit_generic_pass and execution.exit_code in (0, None) and not execution.is_error:
        return [
            TestAttempt(
                test_id=test_id,
                runner="generic",
                outcome="passed",
                turn_index=execution.turn_index,
                command=execution.command,
            )
        ]

    return []


def extract_pass_signal(execution: ToolExecution) -> RunnerPassSignal | None:
    """Return a ``RunnerPassSignal`` if ``execution`` represents a clean passing test run."""
    if not _is_command_execution(execution):
        return None
    if execution.is_error or (execution.exit_code is not None and execution.exit_code != 0):
        return None

    clean_output = strip_ansi(execution.output)
    runner = detect_runner(execution.command, clean_output)
    if runner == "generic":
        if _is_non_runner_command(execution.command):
            return None
        test_id = _bounded_generic_test_id(execution.command)
        if execution.exit_code in (0, None) and test_id:
            return RunnerPassSignal(
                runner="generic",
                turn_index=execution.turn_index,
                command=execution.command,
                passed_count=1,
                failed_count=0,
                explicit_test_ids=[test_id],
            )
        return None

    lines = clean_output.split("\n")
    passed_count = 0
    failed_count = 0
    skipped_count = 0
    deselected_count = 0
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
                    elif kind in ("skipped", "xfailed", "xpassed"):
                        skipped_count += count
                    elif kind == "deselected":
                        deselected_count += count
            else:
                node_status = _parse_pytest_node_status_line(line)
                if node_status is not None:
                    status, raw_nid = node_status
                    nid = _normalize_rel_path(raw_nid)
                    if status == "PASSED":
                        explicit_ids.append(nid)
                    elif status in ("FAILED", "ERROR"):
                        failed_count += 1
        if failed_count > 0:
            return None
        if passed_count == 0 and not explicit_ids:
            # Support redirected empty-stdout clean exits (e.g. `uv run pytest > out.log 2>&1`)
            # ONLY for resolving command-keyed fallback attempts of the identical command.
            if not clean_output.strip() and execution.command:
                return RunnerPassSignal(
                    runner="pytest",
                    turn_index=execution.turn_index,
                    command=execution.command,
                    passed_count=0,
                    failed_count=0,
                    is_command_only_pass=True,
                )
            return None
        return RunnerPassSignal(
            runner="pytest",
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(explicit_ids)),
            failed_count=0,
            skipped_count=skipped_count,
            deselected_count=deselected_count,
            explicit_test_ids=explicit_ids,
        )

    if runner in ("vitest", "jest"):
        for raw_line in lines:
            line = _cap_line(raw_line)
            file_m = _JS_FILE_STATUS_RE.match(line) if " > " not in line else None
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
                    elif kind in ("skipped", "xfailed", "xpassed"):
                        skipped_count += count
        if failed_count > 0:
            return None
        if passed_count == 0 and not passed_files:
            if not clean_output.strip() and execution.command:
                return RunnerPassSignal(
                    runner=runner,
                    turn_index=execution.turn_index,
                    command=execution.command,
                    passed_count=0,
                    failed_count=0,
                    is_command_only_pass=True,
                )
            return None
        return RunnerPassSignal(
            runner=runner,
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(passed_files)),
            failed_count=0,
            skipped_count=skipped_count,
            passed_files=passed_files,
        )

    if runner == "go":
        pending_pass_names: list[str] = []
        for raw_line in lines:
            line = _cap_line(raw_line).strip()
            res_m = _GO_RESULT_RE.match(line)
            if res_m:
                if res_m.group(1) == "PASS":
                    pending_pass_names.append(res_m.group(2))
                elif res_m.group(1) == "FAIL":
                    failed_count += 1
                elif res_m.group(1) == "SKIP":
                    skipped_count += 1
                continue
            ok_m = _GO_PKG_OK_RE.match(line)
            if ok_m:
                if "[no test" not in line:
                    pkg = ok_m.group(1)
                    passed_packages.append(pkg)
                    for tname in pending_pass_names:
                        explicit_ids.append(_qualify_go_test_id(tname, pkg))
                pending_pass_names.clear()
                continue
            if _GO_PKG_FAIL_RE.match(line) or line == "FAIL":
                failed_count += 1
                pending_pass_names.clear()
            elif line == "PASS":
                passed_count = max(passed_count, 1)
        explicit_ids.extend(pending_pass_names)
        if failed_count > 0:
            return None
        if passed_count == 0 and not passed_packages and not explicit_ids:
            if not clean_output.strip() and execution.command:
                return RunnerPassSignal(
                    runner="go",
                    turn_index=execution.turn_index,
                    command=execution.command,
                    passed_count=0,
                    failed_count=0,
                    is_command_only_pass=True,
                )
            return None
        return RunnerPassSignal(
            runner="go",
            turn_index=execution.turn_index,
            command=execution.command,
            passed_count=max(passed_count, len(passed_packages), len(explicit_ids)),
            failed_count=0,
            skipped_count=skipped_count,
            passed_packages=passed_packages,
            explicit_test_ids=explicit_ids,
        )

    return None


def extract_go_failed_packages(output: str) -> dict[str, str]:
    """Map failing Go test names (bare and qualified) to their enclosing Go package."""
    clean_output = strip_ansi(output)
    mapping: dict[str, str] = {}
    pending_tests: list[str] = []
    for raw_line in clean_output.split("\n"):
        line = _cap_line(raw_line)
        res_m = _GO_RESULT_RE.match(line)
        if res_m and res_m.group(1) == "FAIL":
            pending_tests.append(res_m.group(2))
            continue
        stripped = line.strip()
        pkg_fail = _GO_PKG_FAIL_RE.match(stripped)
        if pkg_fail:
            pkg = pkg_fail.group(1)
            for tname in pending_tests:
                mapping[tname] = pkg
                mapping[_qualify_go_test_id(tname, pkg)] = pkg
            pending_tests.clear()
            continue
        if _GO_PKG_OK_RE.match(stripped):
            pending_tests.clear()
    return mapping


def parse_runner_output(
    execution: ToolExecution,
    *,
    emit_generic_pass: bool = False,
) -> list[TestAttempt]:
    """Parse a single ``ToolExecution`` into structured ``TestAttempt`` records."""
    if not _is_command_execution(execution):
        return []
    clean_output = strip_ansi(execution.output)
    runner = detect_runner(execution.command, clean_output)
    is_failure = (
        execution.exit_code is not None and execution.exit_code != 0
    ) or execution.is_error

    parsed: list[TestAttempt]
    if runner == "pytest":
        parsed = _parse_pytest(execution, clean_output)
    elif runner in ("vitest", "jest"):
        parsed = _parse_js_runner(execution, clean_output, runner)
    elif runner == "go":
        parsed = _parse_go_test(execution, clean_output)
    else:
        return _parse_generic(execution, clean_output, emit_generic_pass=emit_generic_pass)

    if not parsed and is_failure:
        return _build_command_failure_attempt(
            execution,
            clean_output,
            runner=runner,
            outcome="error",
        )
    return parsed


def parse_tool_executions(executions: Sequence[ToolExecution]) -> list[TestAttempt]:
    """Parse a sequence of ``ToolExecution`` records in order.

    Tracks failing generic commands so that a subsequent clean run of the exact same
    normalized command emits a ``passed`` ``TestAttempt`` without polluting the ledger
    with unrelated shell commands.
    """
    attempts: list[TestAttempt] = []
    failed_generic_commands: set[str] = set()

    for execution in executions:
        if not _is_command_execution(execution):
            continue
        clean_output = strip_ansi(execution.output)
        runner = detect_runner(execution.command, clean_output)
        if runner == "generic":
            test_id = _bounded_generic_test_id(execution.command)
            is_known_failed = bool(test_id and test_id in failed_generic_commands)
            parsed = _parse_generic(
                execution,
                clean_output,
                emit_generic_pass=is_known_failed,
            )
            for item in parsed:
                if item.outcome in ("failed", "error"):
                    failed_generic_commands.add(item.test_id)
            attempts.extend(parsed)
        else:
            attempts.extend(parse_runner_output(execution))

    return attempts


__all__ = [
    "RunnerInvocation",
    "RunnerPassSignal",
    "detect_runner",
    "extract_go_failed_packages",
    "extract_pass_signal",
    "extract_runner_invocation",
    "normalize_command",
    "parse_runner_invocation",
    "parse_runner_output",
    "parse_tool_executions",
    "strip_ansi",
]
