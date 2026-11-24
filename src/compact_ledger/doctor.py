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
from compact_ledger.models import CompactionSnapshot
from compact_ledger.store import (
    DEFAULT_DIR_NAME,
    ENV_STORE_DIR,
    CorruptSnapshotError,
    SessionStore,
    find_project_root,
    sanitize_session_id,
)

__all__ = [
    "DOCTOR_SCHEMA_VERSION",
    "DoctorCheck",
    "DoctorReport",
    "format_doctor_report_json",
    "format_doctor_report_text",
    "run_doctor",
]

DOCTOR_SCHEMA_VERSION = 1
CheckStatus = Literal["ok", "warn", "fail"]

_INSTALL_CMD = "uv tool install compact-ledger"
_LOCAL_CLAUDE_SETTINGS_RELPATH = Path(".claude") / "settings.local.json"
_INSTALLED_PLUGINS_RELPATH = Path(".claude") / "plugins" / "installed_plugins.json"


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
    schema_version: int = DOCTOR_SCHEMA_VERSION

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
            "schema_version": self.schema_version,
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
        fix="uv tool install --python 3.12 compact-ledger",
        details={"version": ver_str, "executable": sys.executable},
    )


def _plugin_enabled_in_settings(settings_data: dict[str, Any]) -> bool | None:
    """Return ``True`` if enabled, ``False`` if explicitly disabled, else ``None``."""
    enabled = settings_data.get("enabledPlugins")
    if isinstance(enabled, dict):
        result: bool | None = None
        for key, val in enabled.items():
            if not isinstance(key, str):
                continue
            base = key.split("@", 1)[0].strip().lower()
            if base == "compact-ledger":
                result = bool(val)
        return result
    if isinstance(enabled, list):
        for item in enabled:
            if isinstance(item, str) and item.split("@", 1)[0].strip().lower() == "compact-ledger":
                return True
    return None


def _has_installed_plugin_file(home_dir: Path | None, *, project_root: Path) -> bool:
    if home_dir is None:
        return False
    installed_path = home_dir / _INSTALLED_PLUGINS_RELPATH
    try:
        if not installed_path.is_file():
            return False
        data = json.loads(installed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return False

    def _entry_applies(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return True
        scope = entry.get("scope")
        proj_path = entry.get("projectPath")
        if scope == "project" and isinstance(proj_path, str) and proj_path.strip():
            with contextlib.suppress(OSError, RuntimeError):
                return Path(proj_path).expanduser().resolve() == project_root.resolve()
        return True

    if isinstance(data, dict):
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            for k, val in plugins.items():
                if not isinstance(k, str) or k.split("@", 1)[0].strip().lower() != "compact-ledger":
                    continue
                if isinstance(val, list):
                    if any(_entry_applies(item) for item in val):
                        return True
                elif _entry_applies(val):
                    return True
        if isinstance(plugins, list):
            for p in plugins:
                if (
                    isinstance(p, dict)
                    and isinstance(p.get("name"), str)
                    and p["name"].strip().lower() == "compact-ledger"
                    and _entry_applies(p)
                ):
                    return True
    return False


def _check_claude_plugin(project_root: Path) -> tuple[DoctorCheck, bool]:
    """Inspect local ``.claude-plugin/`` ONLY when it belongs to ``compact-ledger``.

    Returns ``(check, local_plugin_valid)``.
    """
    plugin_dir = project_root / ".claude-plugin"
    try:
        plugin_dir_exists = plugin_dir.exists()
    except OSError as exc:
        return (
            DoctorCheck(
                name="claude_plugin",
                status="fail",
                summary=f"Cannot inspect {plugin_dir}: {exc}",
                details={"present": False, "error": str(exc)},
            ),
            False,
        )

    if not plugin_dir_exists:
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

    try:
        if not manifest_path.is_file():
            # Unrelated repo with a .claude-plugin/ directory that has no compact-ledger manifest.
            return (
                DoctorCheck(
                    name="claude_plugin",
                    status="ok",
                    summary="Local .claude-plugin/ has no compact-ledger manifest (skipped)",
                    details={"present": False},
                ),
                False,
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as exc:
            return (
                DoctorCheck(
                    name="claude_plugin",
                    status="fail",
                    summary=f"Invalid .claude-plugin/plugin.json: {exc}",
                    details={"present": True, "manifest_path": str(manifest_path)},
                ),
                False,
            )

        if not isinstance(manifest, dict):
            return (
                DoctorCheck(
                    name="claude_plugin",
                    status="fail",
                    summary=".claude-plugin/plugin.json is not a JSON object",
                    details={"present": True, "manifest_path": str(manifest_path)},
                ),
                False,
            )

        raw_name = manifest.get("name")
        plugin_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        plugin_version = (
            manifest.get("version") if isinstance(manifest.get("version"), str) else None
        )
        # Never fail an unrelated project that happens to ship its own Claude Code plugin!
        if plugin_name is not None and plugin_name != "compact-ledger":
            return (
                DoctorCheck(
                    name="claude_plugin",
                    status="ok",
                    summary=(
                        f"Local .claude-plugin/ belongs to {plugin_name!r}, not compact-ledger"
                    ),
                    details={"present": False, "unrelated_plugin": plugin_name},
                ),
                False,
            )
        if plugin_name is None:
            issues.append(".claude-plugin/plugin.json missing 'name'")

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
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                RecursionError,
            ) as exc:
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
    except OSError as exc:
        return (
            DoctorCheck(
                name="claude_plugin",
                status="fail",
                summary=f"Cannot inspect local plugin files: {exc}",
                details={"present": True, "error": str(exc)},
            ),
            False,
        )

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
                summary=(
                    f"Valid local Claude Code plugin ({label}); hooks/hooks.json and launcher ready"
                ),
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


def _read_settings_file(path: Path) -> tuple[dict[str, Any] | None, str | None, str | None, bool]:
    """Read one Claude Code settings file safely.

    Returns ``(parsed_dict_or_None, error_message_or_None, error_kind_or_None, file_existed)``
    where ``error_kind`` is ``"io"``, ``"json"``, ``"type"``, or ``None``.
    """
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        return None, f"cannot access {path}: {exc}", "io", False
    if not exists:
        return None, None, None, False
    try:
        if not path.is_file():
            return None, f"{path} exists but is not a regular file", "type", True
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path}: {exc}", "io", True
    if not raw.strip():
        return None, f"{path} is empty (invalid JSON)", "json", True
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return None, f"invalid JSON in {path}: {exc}", "json", True
    if not isinstance(parsed, dict):
        return None, f"{path} must contain a JSON object at top level", "json", True
    return parsed, None, None, True


def _check_claude_hooks(
    settings_target: Path,
    *,
    project_root: Path,
    explicit_settings_path: bool,
    env_path: str | None,
    local_plugin_valid: bool,
    home_dir: Path | None,
) -> DoctorCheck:
    init_fix = (
        f"compact-ledger init --write --settings-path {shlex.quote(str(settings_target))}"
        if explicit_settings_path
        else "compact-ledger init --write"
    )

    # Scopes ordered from lowest to highest precedence so higher-precedence settings
    # (project_local > project > user) cleanly override flags like disableAllHooks / enabledPlugins.
    scopes: list[tuple[str, Path]] = []
    if explicit_settings_path:
        scopes.append(("target", settings_target))
    else:
        local_settings = project_root / _LOCAL_CLAUDE_SETTINGS_RELPATH
        if home_dir is not None:
            user_settings = home_dir / DEFAULT_CLAUDE_SETTINGS_RELPATH
            if user_settings not in (settings_target, local_settings):
                scopes.append(("user", user_settings))
        scopes.append(("project", settings_target))
        if local_settings != settings_target:
            scopes.append(("project_local", local_settings))

    merged_pre = False
    merged_start = False
    commands_found: list[str] = []
    missing_execs: list[str] = []
    wired_sources: list[str] = []
    read_errors: list[tuple[str, Path, str, str | None]] = []
    disable_all_hooks_effective = False
    disable_all_hooks_path: str | None = None
    plugin_enabled_state: bool | None = None
    target_existed = False

    for scope_name, path in scopes:
        data, err, err_kind, existed = _read_settings_file(path)
        if path == settings_target:
            target_existed = existed
        if err is not None:
            read_errors.append((scope_name, path, err, err_kind))
            continue
        if data is None:
            continue
        if isinstance(data.get("disableAllHooks"), bool):
            disable_all_hooks_effective = data["disableAllHooks"]
            disable_all_hooks_path = str(path) if disable_all_hooks_effective else None
        plugin_state = _plugin_enabled_in_settings(data)
        if plugin_state is not None:
            plugin_enabled_state = plugin_state
        pre, start, cmds, missing = _inspect_hooks_object(data.get("hooks"), env_path=env_path)
        if pre or start:
            wired_sources.append(str(path))
        merged_pre = merged_pre or pre
        merged_start = merged_start or start
        commands_found.extend(cmds)
        for exe in missing:
            if exe not in missing_execs:
                missing_execs.append(exe)

    # Report corrupt / unreadable target settings file directly with an accurate fix.
    for scope_name, err_path, err_msg, err_kind in read_errors:
        if err_path == settings_target or (not merged_pre and not merged_start):
            if err_kind == "io":
                err_fix = f"Check read permissions on {shlex.quote(str(err_path))}"
            elif err_kind == "type":
                err_fix = f"Replace non-file {shlex.quote(str(err_path))} with a valid JSON file"
            else:
                err_fix = (
                    f"Fix {shlex.quote(str(err_path))} so it contains valid JSON, "
                    f"then run: {init_fix}"
                )
            return DoctorCheck(
                name="claude_hooks",
                status="fail",
                summary=err_msg,
                fix=err_fix,
                details={
                    "settings_path": str(err_path),
                    "scope": scope_name,
                    "pre_compact": merged_pre,
                    "session_start": merged_start,
                },
            )

    if disable_all_hooks_effective and disable_all_hooks_path is not None:
        return DoctorCheck(
            name="claude_hooks",
            status="fail",
            summary=(
                f"Claude Code hooks are disabled "
                f"(disableAllHooks: true in {disable_all_hooks_path})"
            ),
            fix=(
                'Set "disableAllHooks": false (or remove it) in '
                f"{shlex.quote(disable_all_hooks_path)}"
            ),
            details={
                "settings_path": disable_all_hooks_path,
                "disable_all_hooks": True,
                "pre_compact": merged_pre,
                "session_start": merged_start,
            },
        )

    if merged_pre and merged_start:
        details = {
            "settings_path": wired_sources[0] if wired_sources else str(settings_target),
            "wired_sources": wired_sources,
            "pre_compact": True,
            "session_start": True,
            "commands": commands_found,
            "missing_executables": missing_execs,
        }
        source_desc = ", ".join(wired_sources)
        if missing_execs:
            return DoctorCheck(
                name="claude_hooks",
                status="warn",
                summary=(
                    f"PreCompact and SessionStart wired in {source_desc}, "
                    f"but command not on PATH: {', '.join(missing_execs)}"
                ),
                fix=_INSTALL_CMD,
                details=details,
            )
        return DoctorCheck(
            name="claude_hooks",
            status="ok",
            summary=f"PreCompact and SessionStart hooks wired in {source_desc}",
            details=details,
        )

    if not explicit_settings_path and plugin_enabled_state is True:
        return DoctorCheck(
            name="claude_hooks",
            status="ok",
            summary="compact-ledger Claude Code plugin is enabled in settings",
            details={
                "settings_path": str(settings_target),
                "pre_compact": True,
                "session_start": True,
                "provided_by_installed_plugin": True,
            },
        )

    if (
        not explicit_settings_path
        and plugin_enabled_state is None
        and _has_installed_plugin_file(home_dir, project_root=project_root)
    ):
        return DoctorCheck(
            name="claude_hooks",
            status="warn",
            summary=(
                "compact-ledger appears in installed_plugins.json but is not explicitly enabled "
                "in enabledPlugins or .claude/settings.json"
            ),
            fix=init_fix,
            details={
                "settings_path": str(settings_target),
                "pre_compact": merged_pre,
                "session_start": merged_start,
                "installed_plugin_unconfirmed": True,
            },
        )

    if not explicit_settings_path and local_plugin_valid:
        return DoctorCheck(
            name="claude_hooks",
            status="warn",
            summary=(
                "Local plugin hooks/hooks.json is valid, but hooks are not wired in "
                ".claude/settings.json or enabled as an installed plugin"
            ),
            fix=init_fix,
            details={
                "settings_path": str(settings_target),
                "pre_compact": merged_pre,
                "session_start": merged_start,
                "local_plugin_source_only": True,
            },
        )

    missing_events = [
        name for name, ok in (("PreCompact", merged_pre), ("SessionStart", merged_start)) if not ok
    ]
    if not target_existed and not wired_sources:
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
            "pre_compact": merged_pre,
            "session_start": merged_start,
            "missing_events": missing_events,
        },
    )


def _nearest_existing_ancestor(path: Path) -> Path | None:
    cur = path
    for _ in range(64):
        try:
            if cur.exists():
                return cur
        except OSError:
            return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _store_is_gitignored(store_root: Path, project_root: Path) -> bool:
    store_ignore = store_root / ".gitignore"
    try:
        if store_ignore.is_file() and not store_ignore.is_symlink():
            lines = [ln.strip() for ln in store_ignore.read_text(encoding="utf-8").splitlines()]
            if "*" in lines or "/*" in lines:
                return True
    except (OSError, UnicodeDecodeError):
        pass

    repo_ignore = project_root / ".gitignore"
    store_names = {DEFAULT_DIR_NAME, store_root.name}
    try:
        if repo_ignore.is_file() and not repo_ignore.is_symlink():
            for ln in repo_ignore.read_text(encoding="utf-8").splitlines():
                entry = ln.strip()
                if not entry or entry.startswith("#") or entry.startswith("!"):
                    continue
                if entry.startswith("**/"):
                    entry = entry[3:]
                entry = entry.lstrip("/")
                if entry.endswith("/*"):
                    entry = entry[:-2]
                elif entry.endswith("/**"):
                    entry = entry[:-3]
                entry = entry.rstrip("/")
                if entry in store_names:
                    return True
    except (OSError, UnicodeDecodeError):
        pass
    return False


def _check_store(store: SessionStore, project_root: Path) -> DoctorCheck:
    root = store.root_dir
    sessions_dir = store.sessions_dir

    try:
        root_is_symlink = root.is_symlink()
        sessions_is_symlink = False if root_is_symlink else sessions_dir.is_symlink()
    except OSError as exc:
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Cannot inspect store path {root}: {exc}",
            details={"store_dir": str(root), "writable": False, "error": str(exc)},
        )

    if root_is_symlink or sessions_is_symlink:
        symlink_target = root if root_is_symlink else sessions_dir
        quoted_sym = shlex.quote(str(symlink_target))
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Store path uses a symlink (refused for safety): {symlink_target}",
            fix=f"rm {quoted_sym} && mkdir -p {quoted_sym}",
            details={"store_dir": str(root), "exists": True, "writable": False},
        )

    try:
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
                ancestor is not None
                and ancestor.is_dir()
                and os.access(ancestor, os.W_OK | os.X_OK)
            )
    except OSError as exc:
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Cannot inspect store permissions for {root}: {exc}",
            details={"store_dir": str(root), "writable": False, "error": str(exc)},
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

    try:
        if sessions_dir.is_dir():
            valid_sessions: set[str] = set()
            for entry in sorted(sessions_dir.glob("*.json")):
                if entry.is_symlink() or not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    raw_data = json.loads(entry.read_text(encoding="utf-8"))
                    sid = raw_data.get("session_id") if isinstance(raw_data, dict) else None
                    if (
                        not isinstance(sid, str)
                        or not sid.strip()
                        or "\x00" in sid
                        or sanitize_session_id(sid) != entry.stem
                    ):
                        corrupt_count += 1
                        continue
                    snap = CompactionSnapshot.from_dict(raw_data)
                    valid_sessions.add(snap.session_id)
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
                    RecursionError,
                ):
                    corrupt_count += 1
            sessions = sorted(valid_sessions)
    except OSError as exc:
        return DoctorCheck(
            name="store",
            status="fail",
            summary=f"Cannot read sessions in {sessions_dir}: {exc}",
            details={"store_dir": str(root), "exists": exists, "writable": writable},
        )

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
        ignore_path = root / ".gitignore"
        ignore_quoted = shlex.quote(str(ignore_path))
        ignore_is_symlink = False
        with contextlib.suppress(OSError):
            ignore_is_symlink = ignore_path.is_symlink()
        fix_cmd = (
            f"rm {ignore_quoted} && printf '*\\n' > {ignore_quoted}"
            if ignore_is_symlink
            else f"printf '*\\n' > {ignore_quoted}"
        )
        return DoctorCheck(
            name="store",
            status="warn",
            summary=(
                f"Store {root} is writable ({len(sessions)} session(s)), "
                "but missing .gitignore (snapshots hold verbatim user messages)"
            ),
            fix=fix_cmd,
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
    plugin_check, local_plugin_valid = _check_claude_plugin(project_root)
    path_check = _check_cli_on_path(env_path, plugin_ready=local_plugin_valid)
    hooks_check = _check_claude_hooks(
        resolved_settings,
        project_root=project_root,
        explicit_settings_path=explicit_settings,
        env_path=env_path,
        local_plugin_valid=local_plugin_valid,
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
