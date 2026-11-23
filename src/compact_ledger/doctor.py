"""Environment, hook, plugin, and store diagnostics for ``compact-ledger doctor``."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from compact_ledger.hooks import (
    DEFAULT_CLAUDE_COMMAND,
    DEFAULT_CLAUDE_SETTINGS_RELPATH,
    _match_and_rewrite_compact_ledger_hook,
    _matcher_covers_event,
    _parse_tokens,
)
from compact_ledger.store import (
    DEFAULT_DIR_NAME,
    ENV_STORE_DIR,
    CorruptSnapshotError,
    SessionStore,
    find_project_root,
)

__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "format_doctor_report_json",
    "format_doctor_report_text",
    "run_doctor",
]

CheckStatus = Literal["ok", "warn", "fail"]

_INSTALL_CMD = "uv tool install git+https://github.com/1solomonwakhungu/compact-ledger.git"


@dataclass(slots=True)
class DoctorCheck:
    """Result of a single ``compact-ledger doctor`` diagnostic check."""

    name: str
    status: CheckStatus
    summary: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class DoctorReport:
    """Aggregate report returned by :func:`run_doctor`."""

    checks: list[DoctorCheck]
    project_root: str
    store_dir: str
    settings_path: str

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(check.status == "warn" for check in self.checks)

    @property
    def fixes(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for check in self.checks:
            if check.fix and check.fix not in seen:
                seen.add(check.fix)
                ordered.append(check.fix)
        return ordered

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "has_warnings": self.has_warnings,
            "project_root": self.project_root,
            "store_dir": self.store_dir,
            "settings_path": self.settings_path,
            "checks": [c.to_dict() for c in self.checks],
            "fixes": self.fixes,
        }


def _extract_executable_from_command(cmd: str) -> str | None:
    """Return the executable token from a hook command string, or None if plugin-relative."""
    if "${CLAUDE_PLUGIN_ROOT}" in cmd or "$CLAUDE_PLUGIN_ROOT" in cmd:
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", ".", "~")):
        tokens.pop(0)
    if not tokens:
        return None
    return tokens[0]


def _inspect_hooks_object(
    hooks_obj: Any,
    *,
    env_path: str | None,
) -> tuple[bool, bool, list[str], list[str]]:
    """Inspect a Claude Code ``hooks`` dict for PreCompact and SessionStart wiring.

    Returns ``(has_pre_compact, has_session_start, commands_found, missing_executables)``.
    """
    if not isinstance(hooks_obj, dict):
        return False, False, [], []

    expected_prefix_tokens = _parse_tokens(DEFAULT_CLAUDE_COMMAND)
    has_pre = False
    has_start = False
    commands_found: list[str] = []
    missing_execs: list[str] = []

    for event_name, subcmd in (("PreCompact", "pre-compact"), ("SessionStart", "session-start")):
        entries = hooks_obj.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            if not _matcher_covers_event(event_name, entry.get("matcher")):
                continue
            for hook_item in entry["hooks"]:
                is_match, _ = _match_and_rewrite_compact_ledger_hook(
                    hook_item,
                    subcmd,
                    command_prefix=DEFAULT_CLAUDE_COMMAND,
                    expected_prefix_tokens=expected_prefix_tokens,
                )
                if not is_match:
                    continue
                if event_name == "PreCompact":
                    has_pre = True
                else:
                    has_start = True
                cmd = hook_item.get("command")
                if isinstance(cmd, str):
                    commands_found.append(cmd)
                    exe = _extract_executable_from_command(cmd)
                    if exe is not None:
                        expanded = os.path.expanduser(exe)
                        if (
                            shutil.which(expanded, path=env_path) is None
                            and exe not in missing_execs
                        ):
                            missing_execs.append(exe)

    return has_pre, has_start, commands_found, missing_execs


def _check_python_version(version_info: tuple[int, int, int]) -> DoctorCheck:
    ver_str = f"{version_info[0]}.{version_info[1]}.{version_info[2]}"
    if version_info >= (3, 11):
        return DoctorCheck(
            name="python_version",
            status="ok",
            summary=f"Python {ver_str} (>= 3.11 required)",
            details={"version": ver_str, "executable": sys.executable},
        )
    return DoctorCheck(
        name="python_version",
        status="fail",
        summary=f"Python {ver_str} is below required >= 3.11",
        fix="uv python install 3.12",
        details={"version": ver_str, "executable": sys.executable},
    )


def _check_claude_plugin(project_root: Path) -> tuple[DoctorCheck, bool]:
    """Inspect ``.claude-plugin/`` if present. Returns ``(check, plugin_provides_hooks)``."""
    plugin_dir = project_root / ".claude-plugin"
    if not plugin_dir.exists():
        return (
            DoctorCheck(
                name="claude_plugin",
                status="ok",
                summary="No local .claude-plugin/ directory (CLI hooks mode)",
                details={"present": False},
            ),
            False,
        )

    manifest_path = plugin_dir / "plugin.json"
    hooks_path = project_root / "hooks" / "hooks.json"
    launcher_path = project_root / "bin" / "compact-ledger"
    issues: list[str] = []
    fix: str | None = None
    plugin_name: str | None = None
    plugin_version: str | None = None
    hooks_ok = False

    if not manifest_path.is_file():
        issues.append("missing .claude-plugin/plugin.json")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                issues.append(".claude-plugin/plugin.json is not a JSON object")
            else:
                plugin_name = (
                    manifest.get("name") if isinstance(manifest.get("name"), str) else None
                )
                plugin_version = (
                    manifest.get("version") if isinstance(manifest.get("version"), str) else None
                )
                if not plugin_name:
                    issues.append(".claude-plugin/plugin.json missing 'name'")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"invalid .claude-plugin/plugin.json: {exc}")

    if hooks_path.is_file():
        try:
            hooks_data = json.loads(hooks_path.read_text(encoding="utf-8"))
            if isinstance(hooks_data, dict):
                has_pre, has_start, _, _ = _inspect_hooks_object(
                    hooks_data.get("hooks"), env_path=None
                )
                hooks_ok = has_pre and has_start
                if not hooks_ok:
                    missing = [
                        name
                        for name, ok in (("PreCompact", has_pre), ("SessionStart", has_start))
                        if not ok
                    ]
                    issues.append(f"hooks/hooks.json missing {', '.join(missing)}")
            else:
                issues.append("hooks/hooks.json is not a JSON object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"invalid hooks/hooks.json: {exc}")
    else:
        issues.append("missing hooks/hooks.json")

    launcher_exec = False
    if launcher_path.is_file():
        launcher_exec = os.access(launcher_path, os.X_OK) or sys.platform == "win32"
        if not launcher_exec:
            issues.append("bin/compact-ledger is not executable")
            fix = f"chmod +x {shlex.quote(str(launcher_path))}"
    else:
        issues.append("missing bin/compact-ledger launcher")

    details = {
        "present": True,
        "manifest_path": str(manifest_path),
        "name": plugin_name,
        "version": plugin_version,
        "hooks_valid": hooks_ok,
        "launcher_executable": launcher_exec,
        "issues": issues,
    }
    if not issues:
        label = f"{plugin_name} {plugin_version}".strip() if plugin_name else "plugin"
        return (
            DoctorCheck(
                name="claude_plugin",
                status="ok",
                summary=f"Valid local Claude Code plugin ({label}); hooks and launcher ready",
                details=details,
            ),
            True,
        )

    return (
        DoctorCheck(
            name="claude_plugin",
            status="fail",
            summary=f"Plugin issues in .claude-plugin/: {'; '.join(issues)}",
            fix=fix,
            details=details,
        ),
        False,
    )


def _check_cli_on_path(env_path: str | None, *, plugin_ready: bool) -> DoctorCheck:
    resolved = shutil.which(DEFAULT_CLAUDE_COMMAND, path=env_path)
    if resolved is not None:
        return DoctorCheck(
            name="cli_on_path",
            status="ok",
            summary=f"compact-ledger found on PATH ({resolved})",
            details={"executable": resolved},
        )
    if plugin_ready:
        return DoctorCheck(
            name="cli_on_path",
            status="warn",
            summary="compact-ledger not on PATH (local plugin launcher works inside Claude Code)",
            fix=_INSTALL_CMD,
            details={"executable": None, "plugin_launcher_available": True},
        )
    return DoctorCheck(
        name="cli_on_path",
        status="fail",
        summary="compact-ledger is not on PATH (Claude Code hooks cannot invoke it)",
        fix=_INSTALL_CMD,
        details={"executable": None},
    )


def _check_claude_hooks(
    settings_target: Path,
    *,
    explicit_settings_path: bool,
    env_path: str | None,
    plugin_ready: bool,
    home_dir: Path | None,
) -> DoctorCheck:
    init_fix = (
        f"compact-ledger init --write --settings-path {shlex.quote(str(settings_target))}"
        if explicit_settings_path
        else "compact-ledger init --write"
    )

    def _read_settings(path: Path) -> tuple[dict[str, Any] | None, str | None]:
        if not path.exists():
            return None, None
        if not path.is_file():
            return None, f"{path} exists but is not a regular file"
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return None, f"{path} is empty (invalid JSON)"
            parsed = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return None, f"invalid JSON in {path}: {exc}"
        if not isinstance(parsed, dict):
            return None, f"{path} must contain a JSON object at top level"
        return parsed, None

    data, error = _read_settings(settings_target)
    if error is not None:
        return DoctorCheck(
            name="claude_hooks",
            status="fail",
            summary=error,
            fix=init_fix,
            details={
                "settings_path": str(settings_target),
                "pre_compact": False,
                "session_start": False,
            },
        )

    has_pre = False
    has_start = False
    commands_found: list[str] = []
    missing_execs: list[str] = []
    if data is not None:
        has_pre, has_start, commands_found, missing_execs = _inspect_hooks_object(
            data.get("hooks"), env_path=env_path
        )

    if has_pre and has_start:
        details = {
            "settings_path": str(settings_target),
            "pre_compact": True,
            "session_start": True,
            "commands": commands_found,
            "missing_executables": missing_execs,
        }
        if missing_execs:
            return DoctorCheck(
                name="claude_hooks",
                status="warn",
                summary=(
                    f"PreCompact and SessionStart wired in {settings_target}, "
                    f"but command not on PATH: {', '.join(missing_execs)}"
                ),
                fix=_INSTALL_CMD,
                details=details,
            )
        return DoctorCheck(
            name="claude_hooks",
            status="ok",
            summary=f"PreCompact and SessionStart hooks wired in {settings_target}",
            details=details,
        )

    # Check user-level ~/.claude/settings.json when project settings were not explicitly overridden
    if not explicit_settings_path and home_dir is not None:
        global_settings = home_dir / DEFAULT_CLAUDE_SETTINGS_RELPATH
        if global_settings != settings_target:
            g_data, g_err = _read_settings(global_settings)
            if g_err is None and g_data is not None:
                g_pre, g_start, g_cmds, g_missing = _inspect_hooks_object(
                    g_data.get("hooks"), env_path=env_path
                )
                if g_pre and g_start:
                    details = {
                        "settings_path": str(global_settings),
                        "scope": "user",
                        "pre_compact": True,
                        "session_start": True,
                        "commands": g_cmds,
                        "missing_executables": g_missing,
                    }
                    if g_missing:
                        return DoctorCheck(
                            name="claude_hooks",
                            status="warn",
                            summary=(
                                f"Hooks wired globally in {global_settings}, "
                                f"but command not on PATH: {', '.join(g_missing)}"
                            ),
                            fix=_INSTALL_CMD,
                            details=details,
                        )
                    return DoctorCheck(
                        name="claude_hooks",
                        status="ok",
                        summary=(
                            f"PreCompact and SessionStart hooks wired globally in {global_settings}"
                        ),
                        details=details,
                    )

    if not explicit_settings_path and plugin_ready:
        return DoctorCheck(
            name="claude_hooks",
            status="ok",
            summary=(
                "No project .claude/settings.json hooks "
                "(provided by local .claude-plugin/hooks/hooks.json)"
            ),
            details={
                "settings_path": str(settings_target),
                "pre_compact": has_pre,
                "session_start": has_start,
                "provided_by_plugin": True,
            },
        )

    missing_events = [
        name for name, ok in (("PreCompact", has_pre), ("SessionStart", has_start)) if not ok
    ]
    if data is None:
        summary = f"{settings_target} not found (missing {', '.join(missing_events)} hooks)"
    else:
        summary = f"Missing {', '.join(missing_events)} hook(s) in {settings_target}"
    return DoctorCheck(
        name="claude_hooks",
        status="fail",
        summary=summary,
        fix=init_fix,
        details={
            "settings_path": str(settings_target),
            "pre_compact": has_pre,
            "session_start": has_start,
            "missing_events": missing_events,
        },
    )


def _nearest_existing_ancestor(path: Path) -> Path | None:
    cur = path
    for _ in range(64):
        if cur.exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _store_is_gitignored(store_root: Path, project_root: Path) -> bool:
    store_ignore = store_root / ".gitignore"
    if store_ignore.is_file() and not store_ignore.is_symlink():
        with contextlib.suppress(OSError, UnicodeDecodeError):
            lines = [ln.strip() for ln in store_ignore.read_text(encoding="utf-8").splitlines()]
            if "*" in lines or "/*" in lines:
                return True
    repo_ignore = project_root / ".gitignore"
    if repo_ignore.is_file() and not repo_ignore.is_symlink():
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for ln in repo_ignore.read_text(encoding="utf-8").splitlines():
                stripped = ln.strip().strip("/")
                if stripped == DEFAULT_DIR_NAME or stripped == store_root.name:
                    return True
    return False


def _check_store(store: SessionStore, project_root: Path) -> DoctorCheck:
    root = store.root_dir
    sessions_dir = store.sessions_dir

    if root.is_symlink() or sessions_dir.is_symlink():
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Store path uses a symlink (refused for safety): {root}",
            fix=f"rm {shlex.quote(str(root))} && mkdir -p {shlex.quote(str(root))}",
            details={"store_dir": str(root), "exists": True, "writable": False},
        )

    exists = root.exists()
    if exists:
        if not root.is_dir():
            return DoctorCheck(
                name="store",
                status="fail",
                summary=f"Store path exists but is not a directory: {root}",
                details={"store_dir": str(root), "exists": True, "writable": False},
            )
        writable = os.access(root, os.W_OK | os.X_OK)
        if sessions_dir.exists():
            if not sessions_dir.is_dir():
                return DoctorCheck(
                    name="store",
                    status="fail",
                    summary=f"Sessions path exists but is not a directory: {sessions_dir}",
                    details={"store_dir": str(root), "exists": True, "writable": False},
                )
            writable = writable and os.access(sessions_dir, os.W_OK | os.X_OK)
    else:
        ancestor = _nearest_existing_ancestor(root)
        writable = bool(
            ancestor is not None and ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
        )

    if not writable:
        target_for_chmod = root if exists else (_nearest_existing_ancestor(root) or root)
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Store directory is not writable: {root}",
            fix=f"chmod u+rwx {shlex.quote(str(target_for_chmod))}",
            details={"store_dir": str(root), "exists": exists, "writable": False},
        )

    gitignored = _store_is_gitignored(root, project_root) if exists else True
    sessions: list[str] = []
    hook_sessions = 0
    latest_created_at: str | None = None
    latest_session_id: str | None = None
    total_pins = 0
    total_trajectories = 0
    corrupt_count = 0

    if sessions_dir.is_dir():
        sessions = store.list_sessions()
        for entry in sorted(sessions_dir.glob("*.json")):
            if entry.is_symlink() or not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                raw_data = json.loads(entry.read_text(encoding="utf-8"))
                sid = raw_data.get("session_id") if isinstance(raw_data, dict) else None
                if not isinstance(sid, str) or not sid.strip():
                    corrupt_count += 1
                    continue
                snap = store.load_snapshot(sid)
                if snap is None:
                    continue
                if snap.metadata.get("hook_event") == "PreCompact":
                    hook_sessions += 1
                total_pins += len(snap.pins)
                total_trajectories += len(snap.trajectories)
                if latest_created_at is None or snap.created_at > latest_created_at:
                    latest_created_at = snap.created_at
                    latest_session_id = snap.session_id
            except (
                CorruptSnapshotError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ):
                corrupt_count += 1

    details: dict[str, Any] = {
        "store_dir": str(root),
        "exists": exists,
        "writable": True,
        "gitignored": gitignored,
        "session_count": len(sessions),
        "hook_snapshot_count": hook_sessions,
        "latest_session_id": latest_session_id,
        "latest_created_at": latest_created_at,
        "total_pins": total_pins,
        "total_trajectories": total_trajectories,
        "corrupt_files": corrupt_count,
    }

    if exists and not gitignored:
        return DoctorCheck(
            name="store",
            status="warn",
            summary=(
                f"Store {root} is writable ({len(sessions)} session(s)), "
                "but missing .gitignore (snapshots hold verbatim user messages)"
            ),
            fix=f"printf '*\\n' > {shlex.quote(str(root / '.gitignore'))}",
            details=details,
        )

    if corrupt_count > 0:
        return DoctorCheck(
            name="store",
            status="warn",
            summary=(
                f"Store {root} is writable with {len(sessions)} valid session(s) and "
                f"{corrupt_count} unreadable snapshot file(s)"
            ),
            details=details,
        )

    if not exists:
        summary = (
            f"Store {root} not created yet (parent directory writable; .gitignore auto-created "
            "on first write); 0 snapshots recorded"
        )
    elif len(sessions) == 0:
        summary = f"Store {root} writable and git-ignored; 0 snapshots recorded yet"
    elif hook_sessions > 0:
        summary = (
            f"Store {root} writable and git-ignored; {len(sessions)} session(s) "
            f"({hook_sessions} from PreCompact hook, "
            f"latest: {latest_session_id} @ {latest_created_at})"
        )
    else:
        summary = (
            f"Store {root} writable and git-ignored; {len(sessions)} session(s) "
            f"(0 from PreCompact hook yet, latest: {latest_session_id} @ {latest_created_at})"
        )

    return DoctorCheck(
        name="store",
        status="ok",
        summary=summary,
        details=details,
    )


def run_doctor(
    *,
    cwd: Path | None = None,
    store_dir: str | Path | None = None,
    settings_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    python_version: tuple[int, int, int] | None = None,
    home_dir: Path | None = None,
) -> DoctorReport:
    """Run all ``compact-ledger doctor`` checks without mutating disk state."""
    env_map = os.environ if env is None else env
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    project_root = find_project_root(base_cwd)

    if store_dir is not None:
        store = SessionStore(store_dir=store_dir)
    else:
        env_store = env_map.get(ENV_STORE_DIR)
        if env_store and env_store.strip():
            store = SessionStore(store_dir=env_store.strip())
        else:
            store = SessionStore(base_dir=project_root)

    explicit_settings = settings_path is not None
    if settings_path is not None:
        resolved_settings = Path(settings_path).expanduser()
    else:
        resolved_settings = project_root / DEFAULT_CLAUDE_SETTINGS_RELPATH

    resolved_home = home_dir
    if resolved_home is None:
        with contextlib.suppress(OSError, RuntimeError):
            resolved_home = Path.home()

    env_path = env_map.get("PATH")
    py_ver = python_version if python_version is not None else sys.version_info[:3]

    py_check = _check_python_version(py_ver)
    plugin_check, plugin_ready = _check_claude_plugin(project_root)
    path_check = _check_cli_on_path(env_path, plugin_ready=plugin_ready)
    hooks_check = _check_claude_hooks(
        resolved_settings,
        explicit_settings_path=explicit_settings,
        env_path=env_path,
        plugin_ready=plugin_ready,
        home_dir=resolved_home,
    )
    store_check = _check_store(store, project_root)

    return DoctorReport(
        checks=[py_check, path_check, hooks_check, plugin_check, store_check],
        project_root=str(project_root),
        store_dir=str(store.root_dir),
        settings_path=str(resolved_settings),
    )


def format_doctor_report_text(report: DoctorReport) -> str:
    """Format a :class:`DoctorReport` as concise human-readable terminal output."""
    tags = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    lines: list[str] = [
        "compact-ledger doctor",
        f"Project:  {report.project_root}",
        f"Store:    {report.store_dir}",
        "-" * 72,
    ]
    for check in report.checks:
        tag = f"[{tags[check.status]}]"
        lines.append(f"{tag:<6} {check.name:<15} {check.summary}")
    lines.append("-" * 72)

    if report.fixes:
        lines.append("Suggested fixes:")
        for fix_cmd in report.fixes:
            lines.append(f"  {fix_cmd}")
    elif report.ok:
        lines.append("All checks passed.")

    return "\n".join(lines)


def format_doctor_report_json(report: DoctorReport) -> str:
    """Format a :class:`DoctorReport` as indented JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
