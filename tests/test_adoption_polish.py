"""Tests for PR #6 adoption polish: Codex code-mode exec parsing, contradictory negative
constraint retirement, and `compact-ledger doctor` diagnostics.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from compact_ledger.cli import main
from compact_ledger.doctor import run_doctor
from compact_ledger.hooks import handle_pre_compact
from compact_ledger.ledger import build_attempt_ledger
from compact_ledger.models import PinEntry, UserTurn
from compact_ledger.parsers import parse_transcript_lines
from compact_ledger.pins import extract_pins
from compact_ledger.renderer import render_snapshot
from compact_ledger.store import ENV_STORE_DIR, SessionStore


def _turns(*texts: str) -> list[UserTurn]:
    return [UserTurn(turn_index=i + 1, text=t, seq=i + 1) for i, t in enumerate(texts)]


def _active_texts(pins: list[PinEntry]) -> list[str]:
    return [p.text for p in pins if p.superseded_by is None]


def _jsonl(records: list[dict[str, object]]) -> list[str]:
    return [json.dumps(rec) for rec in records]


# ---------------------------------------------------------------------------
# 1. Real Codex CLI rollout parsing (item_completed + code-mode exec JS)
# ---------------------------------------------------------------------------


def test_codex_real_rollout_item_completed_commands_file_changes_and_dedup() -> None:
    pytest_fail_1 = (
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_tax _____________________________________\n"
        ">       assert calc_tax() == 100\n"
        "E       assert 400 == 100\n\n"
        "tests/test_tax.py:12: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_tax.py::test_tax - assert 400 == 100\n"
        "============================== 1 failed in 0.04s ===============================\n"
    )
    pytest_fail_2 = pytest_fail_1.replace("400", "200")

    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-real-1", "cwd": "/repo"}},
        # Dual user message: response_item + event_msg item_completed(UserMessage)
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Always use Decimal, never float."}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-20T10:00:00Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "id": "u1",
                    "content": [{"type": "text", "text": "Always use Decimal, never float."}],
                },
            },
        },
        # Code-mode exec wrapping pytest run 1 (backed by CommandExecution event)
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:02Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_1",
                "input": (
                    'const r = await tools.exec_command({"cmd":"pytest tests/test_tax.py"}); '
                    "text(r.output);"
                ),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-20T10:00:03Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-cmd-1",
                    "command": ["/bin/zsh", "-lc", "pytest tests/test_tax.py"],
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": pytest_fail_1,
                },
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:03Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_1",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": pytest_fail_1},
                ],
            },
        },
        # Code-mode exec wrapping apply_patch with add, update+move, delete + failed FileChange
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:04Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_2",
                "input": (
                    'text(await tools.apply_patch("*** Begin Patch\\n'
                    "*** Update File: src/tax.py\\n@@\\n-return 400\\n+return 200\\n"
                    '*** End Patch"));'
                ),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-20T10:00:05Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "id": "exec-fc-1",
                    "status": "completed",
                    "changes": {
                        "src/tax.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,1 +1,1 @@\n-return 400\n+return 200\n",
                            "move_path": None,
                        },
                        "src/new_helper.py": {
                            "type": "add",
                            "content": "def helper():\n    return 1\n",
                        },
                        "src/old_name.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n",
                            "move_path": "src/renamed.py",
                        },
                        "src/obsolete.py": {
                            "type": "delete",
                            "content": "OLD = True\n",
                        },
                    },
                },
            },
        },
        # A failed FileChange must be ignored
        {
            "type": "event_msg",
            "timestamp": "2026-11-20T10:00:05Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "id": "exec-fc-fail",
                    "status": "failed",
                    "changes": {"src/never_applied.py": {"type": "add", "content": "x = 1"}},
                },
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:06Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_2",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": "{}"},
                ],
            },
        },
        # Second pytest run after edit -> PROGRESSING (400 -> 200)
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:07Z",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_3",
                "input": (
                    'const r = await tools.exec_command({"cmd":"pytest tests/test_tax.py"}); '
                    "text(r.output);"
                ),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-20T10:00:08Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-cmd-2",
                    "command": ["/bin/zsh", "-lc", "pytest tests/test_tax.py"],
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": pytest_fail_2,
                },
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-20T10:00:08Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_3",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": pytest_fail_2},
                ],
            },
        },
    ]

    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.format == "codex"
    assert parsed.session_id == "codex-real-1"
    # Dual UserMessage deduplicated to 1 turn
    assert [t.text for t in parsed.user_turns] == ["Always use Decimal, never float."]
    # Exactly 2 tool executions (no duplicates from custom_tool_call_output!)
    assert len(parsed.tool_executions) == 2
    assert all(e.command == "pytest tests/test_tax.py" for e in parsed.tool_executions)
    assert all(e.exit_code == 1 and e.is_error for e in parsed.tool_executions)
    # File changes extracted accurately and failed FileChange excluded
    assert parsed.modified_files == [
        "src/tax.py",
        "src/new_helper.py",
        "src/old_name.py",
        "src/renamed.py",
        "src/obsolete.py",
    ]
    assert "src/never_applied.py" not in parsed.modified_files
    assert parsed.file_edits[0].old_text == "return 400"
    assert parsed.file_edits[0].new_text == "return 200"

    # Full attempt ledger produces PROGRESSING trajectory (400 -> 200)
    _, trajectories, thrash = build_attempt_ledger(parsed)
    assert len(trajectories) == 1
    assert trajectories[0].status == "PROGRESSING"
    assert [a.actual for a in trajectories[0].attempts] == ["400", "200"]
    assert thrash == []


def test_codex_code_mode_js_fallback_promise_all_settled_template_literals_and_thrash() -> None:
    go_fail = (
        "=== RUN   TestCalc\n"
        "    calc_test.go:12: got 50, want 100\n"
        "--- FAIL: TestCalc (0.00s)\n"
        "FAIL\tgithub.com/acme/app/calc\t0.010s\n"
    )
    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-js-fallback"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix TestCalc."}],
            },
        },
        # Promise.allSettled with tools.exec_command + ignored web__run
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c1",
                "input": (
                    "// comment with fake tools.exec_command({cmd: 'rm -rf /'})\n"
                    "const r = await Promise.allSettled(["
                    'tools.exec_command({cmd: "go test ./calc/..."}), '
                    'tools.web__run({search_query: [{q: "go test"}]})'
                    "]); text(r);"
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.3 seconds\nOutput:\n",
                    },
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            [
                                {
                                    "status": "fulfilled",
                                    "value": {"chunk_id": "1", "exit_code": 1, "output": go_fail},
                                },
                                {"status": "fulfilled", "value": {"results": []}},
                            ]
                        ),
                    },
                ],
            },
        },
        # Template-literal patch edit 1
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c2",
                "input": (
                    "const p = `*** Begin Patch\n"
                    "*** Update File: calc/calc.go\n"
                    "@@\n"
                    "-return 50\n"
                    "+return 75\n"
                    "*** End Patch`;\n"
                    "await tools.apply_patch(p);"
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c2",
                "output": "Script completed\nWall time 0.1 seconds\nOutput:\n{}",
            },
        },
        # Failed patch via Script failed must NOT be recorded as a valid edit
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c_bad_patch",
                "input": (
                    'await tools.apply_patch("*** Begin Patch\\n'
                    '*** Add File: bad.go\\n+x\\n*** End Patch");'
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c_bad_patch",
                "output": "Script failed\nError: apply_patch verification failed",
            },
        },
        # Revert patch edit 2 + same failure -> triggers [THRASH/REVERT]
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c3",
                "input": (
                    'await tools.apply_patch({patch: "*** Begin Patch\\n'
                    "*** Update File: calc/calc.go\\n@@\\n-return 75\\n+return 50\\n"
                    '*** End Patch"});'
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c3",
                "output": "Script completed\nWall time 0.1 seconds\nOutput:\n{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c4",
                "input": (
                    "const out = await tools.shell_command("
                    '{command: ["go", "test", "./calc/..."]}); text(out.output);'
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c4",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": go_fail},
                ],
            },
        },
    ]

    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.modified_files == ["calc/calc.go"]
    assert "bad.go" not in parsed.modified_files
    # Commented-out rm -rf command must NOT be extracted
    assert all("rm -rf" not in (e.command or "") for e in parsed.tool_executions)

    _, trajectories, thrash = build_attempt_ledger(parsed)
    assert len(trajectories) == 1
    assert trajectories[0].runner == "go"
    assert trajectories[0].status == "STALLED"
    assert len(thrash) == 1
    assert thrash[0].path == "calc/calc.go"
    assert thrash[0].reverted is True


def test_codex_exec_js_parser_is_linear_time_on_large_scripts() -> None:
    big_js = "// " + ("x" * 100_000) + '\nawait tools.exec_command({cmd: "pytest -q"});'
    records = [
        {"type": "session_meta", "payload": {"id": "perf-codex"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "p1",
                "input": big_js,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "p1",
                "output": "Script completed\nWall time 0.1 seconds\nOutput:\n1 passed in 0.01s",
            },
        },
    ]
    t0 = time.perf_counter()
    parsed = parse_transcript_lines(_jsonl(records))
    assert time.perf_counter() - t0 < 0.5
    assert len(parsed.tool_executions) == 1
    assert parsed.tool_executions[0].command == "pytest -q"


# ---------------------------------------------------------------------------
# 2. Contradictory negative-constraint retirement & false-positive guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("opening", "correction"),
    [
        ("Use tabs. Never use spaces.", "Actually use 2 spaces instead of tabs."),
        ("Use tabs. Don't use spaces.", "Actually use 2 spaces instead of tabs."),
        ("Use tabs. No spaces.", "Actually switch from tabs to 2 spaces."),
        ("Use tabs. Avoid spaces.", "Instead of tabs, use 2 spaces."),
        ("Use tabs. Spaces are forbidden.", "Replace tabs with 2 spaces."),
        ("Never use spaces.", "Actually switch to 2 spaces."),
        ("Don't use pnpm.", "Actually switch to pnpm."),
        ("No mocks.", "Actually use mocks instead of real DB."),
    ],
)
def test_contradictory_negative_constraints_are_retired_by_adopting_override(
    opening: str,
    correction: str,
) -> None:
    pins = extract_pins(_turns(opening, correction))
    active = _active_texts(pins)
    assert active == [correction], f"Expected only correction active, got: {active}"
    override = next(p for p in pins if p.text == correction)
    assert override.source == "override"
    assert override.supersedes is not None
    # Every earlier rule (positive and contradicted negative) points to this override
    retired = [p for p in pins if p is not override]
    assert retired and all(p.superseded_by == override.pin_id for p in retired)


def test_contradictory_negative_constraint_false_positive_guards() -> None:
    # 1. Qualified prohibition ("trailing spaces") and scoped prohibition ("spaces in filenames")
    #    and unrelated prohibition ("db/migrations/") MUST ALL SURVIVE!
    pins = extract_pins(
        _turns(
            "Use tabs. Never use spaces.",
            "Never use trailing spaces.",
            "Never use spaces in filenames.",
            "Never touch db/migrations/.",
            "Actually use 2 spaces instead of tabs.",
        )
    )
    active = _active_texts(pins)
    assert "Never use spaces." not in active
    assert "Use tabs." not in active
    assert "Never use trailing spaces." in active
    assert "Never use spaces in filenames." in active
    assert "Never touch db/migrations/." in active
    assert "Actually use 2 spaces instead of tabs." in active

    # 2. When the override DOES mention the scope ("filenames"), the scoped prohibition IS retired.
    scoped_pins = extract_pins(
        _turns(
            "Never use spaces in filenames.",
            "Actually use spaces in filenames instead of underscores.",
        )
    )
    assert _active_texts(scoped_pins) == [
        "Actually use spaces in filenames instead of underscores."
    ]

    # 3. Negated override ("Actually don't use spaces") must NOT treat "spaces" as adopted.
    neg_override = extract_pins(
        _turns(
            "Never use spaces.",
            "Actually don't use tabs either.",
        )
    )
    assert "Never use spaces." in _active_texts(neg_override)

    # 4. Manual negative pins are NOT silently retired by implicit adoption.
    manual_neg = PinEntry(
        pin_id="pin-manual-no-spaces",
        text="Never use spaces.",
        source="manual",
        is_negative=True,
    )
    manual_pins = extract_pins(
        _turns("Use tabs.", "Actually use 2 spaces instead of tabs."),
        manual_pins=[manual_neg],
    )
    assert "Never use spaces." in _active_texts(manual_pins)

    # 5. Non-override turn does NOT retire an earlier negative constraint.
    non_ovr = extract_pins(_turns("Never use spaces.", "Use 2 spaces for YAML."))
    assert "Never use spaces." in _active_texts(non_ovr)

    # 6. Generic single-word prohibitions ("Never use config") are not retired by unrelated switch.
    generic_guard = extract_pins(
        _turns("Never use default.", "Use tabs.", "Actually use default instead of tabs.")
    )
    assert "Never use default." in _active_texts(generic_guard)


def test_rendered_markdown_hides_retired_contradictory_negative_constraint() -> None:
    from compact_ledger.models import CompactionSnapshot

    pins = extract_pins(
        _turns("Use tabs. Never use spaces.", "Actually use 2 spaces instead of tabs.")
    )
    md = render_snapshot(CompactionSnapshot(session_id="tabs-vs-spaces", pins=pins))
    assert "[OVERRIDE] Actually use 2 spaces instead of tabs." in md
    assert "(supersedes: Use tabs.)" in md
    assert "Never use spaces." not in md


# ---------------------------------------------------------------------------
# 3. `compact-ledger doctor` command & diagnostics
# ---------------------------------------------------------------------------


def _make_fake_cli(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "compact-ledger"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_doctor_reports_missing_path_and_hooks_with_copy_paste_fixes_and_no_disk_writes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()

    report = run_doctor(
        cwd=project,
        env={"PATH": str(tmp_path / "empty-bin")},
        home_dir=tmp_path / "home",
    )
    assert report.ok is False
    by_name = {c.name: c for c in report.checks}
    assert by_name["python_version"].status == "ok"
    assert by_name["cli_on_path"].status == "fail"
    assert "uv tool install" in (by_name["cli_on_path"].fix or "")
    assert by_name["claude_hooks"].status == "fail"
    assert by_name["claude_hooks"].fix == "compact-ledger init --write"
    assert by_name["claude_plugin"].status == "ok"
    assert by_name["claude_plugin"].details["present"] is False
    assert by_name["store"].status == "ok"
    assert by_name["store"].details["exists"] is False
    # Doctor must be strictly read-only and never create .compact-ledger
    assert not (project / ".compact-ledger").exists()


def test_doctor_green_after_init_and_pre_compact_hook_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    fake_bin = tmp_path / "bin"
    _make_fake_cli(fake_bin)

    monkeypatch.chdir(project)
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.setenv("PATH", str(fake_bin))

    # Wire hooks via init --write
    assert main(["init", "--write"]) == 0
    capsys.readouterr()

    # Simulate PreCompact firing
    transcript = project / "sess.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "doc-s1",
                "message": {"role": "user", "content": "Never push to main."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pre = handle_pre_compact(
        json.dumps(
            {"session_id": "doc-s1", "transcript_path": str(transcript), "cwd": str(project)}
        ),
        env={},
    )
    assert pre.exit_code == 0

    # CLI doctor text mode
    assert main(["doctor"]) == 0
    text_out = capsys.readouterr().out
    assert "compact-ledger doctor" in text_out
    assert "[OK]" in text_out
    assert "[FAIL]" not in text_out
    assert "1 from PreCompact hook" in text_out
    assert "All checks passed." in text_out

    # CLI doctor --json mode
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["has_warnings"] is False
    assert payload["fixes"] == []
    store_check = next(c for c in payload["checks"] if c["name"] == "store")
    assert store_check["details"]["session_count"] == 1
    assert store_check["details"]["hook_snapshot_count"] == 1
    assert store_check["details"]["gitignored"] is True


def test_doctor_detects_python_too_old_missing_gitignore_corrupt_settings_and_plugin_status(
    tmp_path: Path,
) -> None:
    project = tmp_path / "bad-proj"
    project.mkdir()
    (project / ".git").mkdir()

    # Corrupt .claude/settings.json
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{broken json", encoding="utf-8")

    # Store exists without .gitignore
    store = SessionStore(base_dir=project)
    store.sessions_dir.mkdir(parents=True)

    # Broken .claude-plugin (missing hooks/hooks.json and non-executable launcher)
    (project / ".claude-plugin").mkdir()
    (project / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "compact-ledger", "version": "0.1.0"}), encoding="utf-8"
    )
    (project / "bin").mkdir()
    launcher = project / "bin" / "compact-ledger"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR)

    report = run_doctor(
        cwd=project,
        env={"PATH": str(tmp_path / "empty")},
        python_version=(3, 10, 14),
        home_dir=tmp_path / "home",
    )
    assert report.ok is False
    by_name = {c.name: c for c in report.checks}
    assert by_name["python_version"].status == "fail"
    assert by_name["claude_hooks"].status == "fail"
    assert "invalid JSON" in by_name["claude_hooks"].summary
    assert by_name["claude_plugin"].status == "fail"
    if sys.platform != "win32":
        assert "chmod +x" in (by_name["claude_plugin"].fix or "")
    assert by_name["store"].status == "warn"
    assert ".gitignore" in by_name["store"].summary
    assert ".gitignore" in (by_name["store"].fix or "")


def test_doctor_validates_repo_claude_plugin_cleanly(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    fake_bin = tmp_path / "bin"
    _make_fake_cli(fake_bin)

    report = run_doctor(
        cwd=repo_root,
        store_dir=tmp_path / "isolated-store",
        env={"PATH": str(fake_bin)},
        home_dir=tmp_path / "home",
    )
    assert report.ok is True
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_plugin"].status == "ok"
    assert by_name["claude_plugin"].details["present"] is True
    assert by_name["claude_plugin"].details["hooks_valid"] is True
    # Local plugin source alone warns (does not falsely report hooks active unless installed/wired)
    assert by_name["claude_hooks"].status == "warn"
    assert by_name["claude_hooks"].fix == "compact-ledger init --write"


def test_doctor_cli_returns_nonzero_when_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "empty-proj"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "none"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    rc = main(["doctor", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["ok"] is False
    assert "compact-ledger init --write" in data["fixes"]


# ---------------------------------------------------------------------------
# 4. Adversarial regression tests from Fable 5.2 review (B1, B2, M1-M5)
# ---------------------------------------------------------------------------


def test_codex_late_yielded_and_parallel_exec_events_never_double_count() -> None:
    fail_out = (
        "=================================== FAILURES ===================================\n"
        "_________________________________ test_x _______________________________________\n"
        ">       assert calc() == 100\n"
        "E       assert 400 == 100\n\n"
        "tests/test_x.py:10: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_x.py::test_x - assert 400 == 100\n"
        "============================== 1 failed in 0.04s ===============================\n"
    )
    # 1. Late CommandExecution after Script completed (B1)
    late_records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-late"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "late_1",
                "input": (
                    'const r = await tools.exec_command({cmd: "pytest tests/test_x.py"}); '
                    "text(r.output);"
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "late_1",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": fail_out},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "cmd-late-1",
                    "command": ["/bin/zsh", "-lc", "pytest tests/test_x.py"],
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": fail_out,
                },
            },
        },
    ]
    parsed_late = parse_transcript_lines(_jsonl(late_records))
    assert len(parsed_late.tool_executions) == 1
    assert parsed_late.tool_executions[0].exit_code == 1
    _, trajs, _ = build_attempt_ledger(parsed_late)
    assert len(trajs) == 1
    assert trajs[0].status == "SINGLE_FAILURE"
    assert len(trajs[0].attempts) == 1

    # 2. Yielded exec ("Script running with cell ID ...") followed later by CommandExecution
    yielded_records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-yield"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "y1",
                "input": 'await tools.exec_command({cmd: "pytest tests/test_x.py"});',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "y1",
                "output": "Script running with cell ID g1:1",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "cmd-y1",
                    "command": ["/bin/zsh", "-lc", "pytest tests/test_x.py"],
                    "status": "failed",
                    "exit_code": 1,
                    "aggregated_output": fail_out,
                },
            },
        },
    ]
    parsed_yield = parse_transcript_lines(_jsonl(yielded_records))
    assert len(parsed_yield.tool_executions) == 1
    assert parsed_yield.tool_executions[0].exit_code == 1
    assert "assert 400 == 100" in parsed_yield.tool_executions[0].output

    # 3. Two parallel execs open at once + late FileChange after exec output
    parallel_records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-par"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "p1",
                "input": 'await tools.exec_command({cmd: "pytest tests/test_a.py"});',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "p2",
                "input": 'await tools.exec_command({cmd: "pytest tests/test_b.py"});',
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "ev-a",
                    "command": ["pytest", "tests/test_a.py"],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "1 passed",
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "ev-b",
                    "command": ["pytest", "tests/test_b.py"],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "1 passed",
                },
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "p1", "output": "ok"},
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "p2", "output": "ok"},
        },
        # Late FileChange after custom_tool_call_output must replace fallback, not duplicate
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "p_fc",
                "input": (
                    'await tools.apply_patch("*** Begin Patch\\n'
                    '*** Update File: src/a.py\\n@@\\n-1\\n+2\\n*** End Patch");'
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "p_fc", "output": "{}"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "id": "fc-late",
                    "status": "completed",
                    "changes": {
                        "src/a.py": {
                            "type": "update",
                            "unified_diff": "@@ -1,1 +1,1 @@\n-1\n+2\n",
                        }
                    },
                },
            },
        },
    ]
    parsed_par = parse_transcript_lines(_jsonl(parallel_records))
    assert len(parsed_par.tool_executions) == 2
    assert len(parsed_par.file_edits) == 1
    assert parsed_par.modified_files == ["src/a.py"]


def test_codex_patch_survives_failing_cmd_tail_alignment_shorthand_and_quadratic_guard() -> None:
    # M2: apply_patch followed by failing pytest in the SAME exec script keeps the patch!
    # M3: 2 commands run, only the 2nd structured JSON result printed -> aligns to tail!
    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-m2-m3"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "combo_1",
                "input": (
                    'await tools.apply_patch("*** Begin Patch\\n'
                    '*** Update File: src/calc.py\\n@@\\n-return 1\\n+return 2\\n*** End Patch");\n'
                    'await tools.exec_command({cmd: "ruff check ."});\n'
                    'const r = await tools.exec_command({cmd: "pytest tests/test_calc.py"});\n'
                    "text(JSON.stringify(r));"
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "combo_1",
                "output": json.dumps({"exit_code": 1, "output": "FAILED test_calc"}),
            },
        },
        # Shorthand {cmd} + string concatenation
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "short_1",
                "input": 'const cmd = "pytest -q"; await tools.exec_command({cmd});',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "short_1",
                "output": json.dumps({"exit_code": 0, "output": "1 passed"}),
            },
        },
        # Aborted exec at EOF with no output must NOT record apply_patch as applied
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "aborted_eof",
                "input": (
                    'await tools.apply_patch("*** Begin Patch\\n'
                    '*** Add File: src/aborted.py\\n+x=1\\n*** End Patch");'
                ),
            },
        },
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert parsed.modified_files == ["src/calc.py"]
    assert "src/aborted.py" not in parsed.modified_files
    by_cmd = {e.command: e for e in parsed.tool_executions}
    assert by_cmd["ruff check ."].exit_code is None
    assert by_cmd["ruff check ."].output == ""
    assert by_cmd["pytest tests/test_calc.py"].exit_code == 1
    assert "FAILED test_calc" in by_cmd["pytest tests/test_calc.py"].output
    assert by_cmd["pytest -q"].exit_code == 0

    # M5: 40KB identifier in object must stay strictly O(N) (< 0.2s)
    huge_ident = "a" * 40_000
    perf_records = [
        {"type": "session_meta", "payload": {"id": "codex-m5"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "m5",
                "input": f"await tools.exec_command({{cmd: {huge_ident}}});",
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "m5", "output": "ok"},
        },
    ]
    t0 = time.perf_counter()
    parse_transcript_lines(_jsonl(perf_records))
    assert time.perf_counter() - t0 < 0.2


def test_codex_three_origin_user_dedup_and_short_reply_isolation() -> None:
    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "codex-dedup"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Never use tokio."}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Never use tokio."}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "Never use tokio."}],
                },
            },
        },
        # Short distinct message "ok" must NOT be merged into "Never use tokio."
        {"type": "event_msg", "payload": {"type": "user_message", "message": "ok"}},
    ]
    parsed = parse_transcript_lines(_jsonl(records))
    assert [t.text for t in parsed.user_turns] == ["Never use tokio.", "ok"]


@pytest.mark.parametrize(
    ("ban", "later_turn"),
    [
        (
            "Don't add any new third-party dependencies.",
            "Actually use the existing third-party dependencies for retries.",
        ),
        ("Never use os.system.", "Actually use os.path.join for paths."),
        ("Never use console.log.", "Actually use console.table for the debug output."),
        ("Never use Python 2.", "Actually use Python 3.12."),
        ("Never use port 22.", "Actually use port 8080."),
        ("Never use TLS 1.0.", "Actually use TLS 1.3 for external traffic."),
        ("Never use sudo.", "Actually use apt without sudo."),
        ("Never use lodash.", "Actually use anything but lodash."),
        ("Never use mocks.", "Actually use real services unless you must use mocks."),
        ("Never use mocks.", "Actually we no longer use mocks anywhere, keep it that way."),
        ("Never use tabs.", "Actually the old code used to use tabs, keep ignoring that."),
        ("Never use redis.", "Actually use port 9090. Also we could use redis later maybe."),
        ("Never use mocks in integration tests.", "Actually use mocks in unit tests."),
        ("Never use console.log.", "Actually use console.log in the debug script."),
    ],
)
def test_b2_adversarial_overrides_never_falsely_retire_unrelated_bans(
    ban: str,
    later_turn: str,
) -> None:
    pins = extract_pins(_turns(ban, later_turn))
    active = _active_texts(pins)
    assert ban in active, f"Ban {ban!r} was falsely retired by {later_turn!r}! active={active}"


def test_m1_retracting_override_reactivates_extra_retired_ban() -> None:
    # Retracting with "Scratch that." reactivates "Never use spaces."
    pins = extract_pins(
        _turns(
            "Use tabs. Never use spaces.",
            "Actually use 2 spaces instead of tabs.",
            "Scratch that.",
        )
    )
    active = _active_texts(pins)
    assert "Never use spaces." in active

    # Follow-up override that STILL adopts spaces keeps "Never use spaces." retired!
    still_spaces = extract_pins(
        _turns(
            "Use tabs. Never use spaces.",
            "Actually use 2 spaces instead of tabs.",
            "Actually use 4 spaces instead of 2 spaces.",
        )
    )
    assert "Never use spaces." not in _active_texts(still_spaces)
    assert "Actually use 4 spaces instead of 2 spaces." in _active_texts(still_spaces)

    # Switching back to tabs reactivates "Never use spaces."!
    back_to_tabs = extract_pins(
        _turns(
            "Use tabs. Never use spaces.",
            "Actually use 2 spaces instead of tabs.",
            "Actually use tabs instead of spaces.",
        )
    )
    assert "Never use spaces." in _active_texts(back_to_tabs)


def test_m4_doctor_scopes_unrelated_plugin_disable_all_hooks_and_symlink_fixes(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    _make_fake_cli(fake_bin)

    # 1. Unrelated .claude-plugin/ repo with hooks in settings.local.json -> completely OK!
    proj = tmp_path / "other-plugin-repo"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / ".claude-plugin").mkdir()
    (proj / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "my-other-plugin", "version": "1.0.0"}), encoding="utf-8"
    )
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreCompact": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "compact-ledger hook pre-compact"}
                            ],
                        }
                    ],
                    "SessionStart": [
                        {
                            "matcher": "compact|resume",
                            "hooks": [
                                {"type": "command", "command": "compact-ledger hook session-start"}
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    # Repo .gitignore using .compact-ledger/* pattern
    (proj / ".gitignore").write_text(".compact-ledger/*\n", encoding="utf-8")
    (proj / ".compact-ledger").mkdir()

    r1 = run_doctor(cwd=proj, env={"PATH": str(fake_bin)}, home_dir=tmp_path / "h1")
    assert r1.ok is True
    by1 = {c.name: c for c in r1.checks}
    assert by1["claude_plugin"].status == "ok"
    assert by1["claude_hooks"].status == "ok"
    assert by1["store"].status == "ok"

    # 2. disableAllHooks: true must report FAIL!
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"disableAllHooks": True}), encoding="utf-8"
    )
    r2 = run_doctor(cwd=proj, env={"PATH": str(fake_bin)}, home_dir=tmp_path / "h1")
    assert r2.ok is False
    by2 = {c.name: c for c in r2.checks}
    assert by2["claude_hooks"].status == "fail"
    assert "disableAllHooks" in by2["claude_hooks"].summary

    # 3. Installed plugin in ~/.claude/settings.json -> hooks OK!
    p3 = tmp_path / "plugin-user-proj"
    p3.mkdir()
    home3 = tmp_path / "h3"
    (home3 / ".claude").mkdir(parents=True)
    (home3 / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"compact-ledger@marketplace": True}}),
        encoding="utf-8",
    )
    r3 = run_doctor(cwd=p3, env={"PATH": str(fake_bin)}, home_dir=home3)
    assert r3.ok is True
    by3 = {c.name: c for c in r3.checks}
    assert by3["claude_hooks"].status == "ok"

    # 4. Symlinked sessions/ targets sessions in fix command (not parent dir),
    #    and symlinked .gitignore removes symlink before writing!
    if sys.platform != "win32":
        p4 = tmp_path / "sym-proj"
        p4.mkdir()
        st = p4 / ".compact-ledger"
        st.mkdir()
        real_target = tmp_path / "outside"
        real_target.mkdir()
        (st / "sessions").symlink_to(real_target)
        r4 = run_doctor(cwd=p4, env={"PATH": str(fake_bin)}, home_dir=tmp_path / "h4")
        by4 = {c.name: c for c in r4.checks}
        assert by4["store"].status == "fail"
        assert "sessions" in (by4["store"].fix or "")

        p5 = tmp_path / "sym-ignore"
        p5.mkdir()
        st5 = p5 / ".compact-ledger"
        st5.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        (st5 / ".gitignore").symlink_to(victim)
        r5 = run_doctor(cwd=p5, env={"PATH": str(fake_bin)}, home_dir=tmp_path / "h5")
        by5 = {c.name: c for c in r5.checks}
        assert by5["store"].status == "warn"
        assert "rm " in (by5["store"].fix or "")
