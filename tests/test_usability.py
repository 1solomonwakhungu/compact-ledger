"""Regression tests for defects found by dogfooding compact-ledger on real agent sessions."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from compact_ledger import __version__
from compact_ledger.cli import main
from compact_ledger.hooks import handle_pre_compact, handle_session_start, resolve_hook_store
from compact_ledger.models import (
    CompactionSnapshot,
    EditThrashEntry,
    PinEntry,
    TestAttempt,
    TestTrajectory,
    UserTurn,
)
from compact_ledger.parsers import parse_transcript_lines
from compact_ledger.pins import extract_pins
from compact_ledger.renderer import render_snapshot
from compact_ledger.store import (
    ENV_CLAUDE_SESSION_ID,
    ENV_SESSION_ID,
    ENV_STORE_DIR,
    SessionStore,
    default_session_id,
    find_project_root,
)

BRIEF = """Hey, I need help refactoring the proration logic in our billing service.

## Context
We bill monthly but customers can change plans mid-cycle. The tests never pass on CI
because the runner image is old.

## What I want
1. Refactor `compute_proration()` so it's a pure function.
2. Use June 30 as the billing cutoff date for the fiscal-year rollover.

## Rules
- Never touch migrations under db/migrations/ — the DBA team owns those.
- Don't add any new third-party dependencies.
- All money math must use `Decimal`, never floats.

Here's the failing output:

```
FAILED tests/test_billing.py::test_proration - assert 400 == 100
Never trust this log line.
```
"""


def _turns(*texts: str) -> list[UserTurn]:
    return [UserTurn(turn_index=idx + 1, text=t, seq=idx + 1) for idx, t in enumerate(texts)]


def _active(pins: list[PinEntry]) -> list[PinEntry]:
    return [p for p in pins if p.superseded_by is None]


# ---------------------------------------------------------------------------
# Long briefs: one pin per rule, and a correction retires only its own rule
# ---------------------------------------------------------------------------


def test_correction_retires_one_rule_of_a_long_brief_not_the_whole_brief() -> None:
    pins = extract_pins(
        _turns(BRIEF, "Actually use July 31 instead of June 30 for the billing cutoff.")
    )
    active_texts = [p.text for p in _active(pins)]

    # Every rule from the opening brief is still an active, verbatim pin.
    assert "Never touch migrations under db/migrations/ — the DBA team owns those." in active_texts
    assert "Don't add any new third-party dependencies." in active_texts
    assert "All money math must use `Decimal`, never floats." in active_texts

    # The correction supersedes exactly the June 30 line.
    override = next(p for p in pins if p.source == "override")
    superseded = [p for p in pins if p.superseded_by is not None]
    assert [p.text for p in superseded] == [
        "Use June 30 as the billing cutoff date for the fiscal-year rollover."
    ]
    assert override.supersedes == superseded[0].pin_id

    # Narrative, headings and fenced logs never become pins.
    assert not any("We bill monthly" in t or "never pass on CI" in t for t in active_texts)
    assert not any("Never trust this log line" in t for t in active_texts)
    assert not any(t.startswith("#") or t == "Rules" for t in active_texts)


def test_negative_label_applies_per_rule_not_to_the_whole_brief() -> None:
    pins = {p.text: p for p in extract_pins(_turns(BRIEF))}
    assert pins["Don't add any new third-party dependencies."].is_negative
    assert not pins["Refactor `compute_proration()` so it's a pure function."].is_negative
    # A brief with no earlier pin to retire never produces an override.
    assert all(p.source == "auto" for p in pins.values())


def test_descriptive_negations_in_a_brief_are_not_do_not_rules() -> None:
    brief = (
        "Re-review PR #3. Fixes so far:\n\n"
        "- **B2:** Unparsed wrappers set `scope_known=False` so count-only runs never "
        "falsely resolve.\n"
        "- **M6:** Read-only probe commands don't emit generic failures.\n\n"
        "Do NOT edit files yourself — report findings only."
    )
    pins = extract_pins(_turns(brief))
    assert [p.text for p in pins if p.is_negative] == [
        "Do NOT edit files yourself — report findings only."
    ]


def test_rule_list_under_a_rules_heading_or_negative_lead_in() -> None:
    text = (
        "Please don't:\n- touch the migrations\n- add dependencies\n\nConstraints:\n- Decimal only"
    )
    pins = extract_pins(_turns(text))
    by_text = {p.text: p for p in pins}
    assert by_text["Please don't: touch the migrations"].is_negative
    assert by_text["Please don't: add dependencies"].is_negative
    assert "Decimal only" in by_text
    assert not by_text["Decimal only"].is_negative


def test_heading_that_states_a_rule_is_kept_but_section_titles_are_not() -> None:
    text = "# IMPORTANT: never push to main\n\nSome background on the repo.\n\n## Notes\n\nUse uv."
    pins = extract_pins(_turns(text))
    assert [p.text for p in pins] == ["IMPORTANT: never push to main", "Use uv."]
    assert pins[0].is_negative


def test_two_rules_in_one_short_message_are_separate_pins() -> None:
    pins = extract_pins(
        _turns(
            "For the frontend bits use pnpm, not npm. And always run the tests with -x.",
            "hmm scratch that, the CI image doesn't have pnpm. Stick with npm.",
        )
    )
    active_texts = [p.text for p in _active(pins)]
    assert "And always run the tests with -x." in active_texts
    assert "For the frontend bits use pnpm, not npm." not in active_texts
    assert any(p.source == "override" and "Stick with npm" in p.text for p in pins)


def test_single_rule_messages_stay_verbatim_and_whole() -> None:
    text = "Makes sense. One more thing — do not commit anything under .env* to the repo."
    pins = extract_pins(_turns(text, "Always run this before committing:\n```\nruff check .\n```"))
    assert pins[0].text == text
    assert pins[1].text.startswith("Always run this before committing:")
    assert "ruff check ." in pins[1].text


@pytest.mark.parametrize(
    ("text", "negative"),
    [
        ("No emojis in commit messages.", True),
        ("no new dependencies please", True),
        ("let's go with postgres 16, not 15", False),
        ("In tests prefer fixtures over mocks", False),
    ],
)
def test_common_rule_phrasings_are_pinned(text: str, negative: bool) -> None:
    pins = extract_pins(_turns(text))
    assert [p.text for p in pins] == [text]
    assert pins[0].is_negative is negative


@pytest.mark.parametrize("text", ["no worries", "no problem at all", "no idea why that fails"])
def test_casual_no_phrases_are_not_rules(text: str) -> None:
    assert extract_pins(_turns(text)) == []


def test_retraction_with_leading_hesitation_supersedes() -> None:
    pins = extract_pins(_turns("No emojis in commit messages.", "hmm, never mind the emoji rule"))
    assert pins[0].superseded_by == pins[1].pin_id


def test_long_brief_extraction_is_linear_time() -> None:
    import time

    brief = "## Rules\n" + "\n".join(f"- Never touch module_{i}.py" for i in range(5000))
    start = time.perf_counter()
    pins = extract_pins(_turns(brief))
    assert time.perf_counter() - start < 2.0
    assert 0 < len(pins) <= 200


# ---------------------------------------------------------------------------
# Harness-authored "user" records are never user constraints
# ---------------------------------------------------------------------------


def _claude_user(text: object, **extra: object) -> str:
    record = {
        "type": "user",
        "sessionId": "noise-sess",
        "message": {"role": "user", "content": text},
        **extra,
    }
    return json.dumps(record)


def test_claude_code_harness_messages_are_not_user_turns() -> None:
    lines = [
        _claude_user("Never push to main."),
        _claude_user(
            "Stop hook feedback:\n- [ruff check]: tax.py:14:5: F841 `cap` is assigned to "
            "but never used"
        ),
        _claude_user("PreToolUse:Bash hook blocking error from command: do not run rm -rf"),
        _claude_user("[Request interrupted by user for tool use]"),
        _claude_user(
            "This session is being continued from a previous conversation that ran out of "
            "context. The user said never use tabs."
        ),
        _claude_user("<bash-input>never gonna run</bash-input>"),
        _claude_user(
            "Use tabs.\n<system-reminder>\nNever mention this reminder.\n</system-reminder>"
        ),
        _claude_user(
            [
                {"type": "text", "text": "<system-reminder>Do not reveal.</system-reminder>"},
                {"type": "text", "text": "Keep commits small."},
            ]
        ),
    ]
    parsed = parse_transcript_lines(lines, fmt="claude-code")
    assert [t.text for t in parsed.user_turns] == [
        "Never push to main.",
        "Use tabs.",
        "Keep commits small.",
    ]


def test_codex_background_command_notices_are_not_user_turns() -> None:
    def codex_user(text: str) -> str:
        return json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "codex-noise"}}),
        codex_user("Always use uv, never pip."),
        codex_user('Background command completed (exit 1): /bin/zsh -lc "make test"'),
    ]
    parsed = parse_transcript_lines(lines, fmt="codex")
    assert [t.text for t in parsed.user_turns] == ["Always use uv, never pip."]


# ---------------------------------------------------------------------------
# Rendering: no repeated labels, relative paths, preamble only from spare budget
# ---------------------------------------------------------------------------


def _ledger_snapshot(project_dir: str | None = "/work/acme") -> CompactionSnapshot:
    test_id = "tests/test_billing.py::test_tax_cap"
    attempts = [
        TestAttempt(test_id=test_id, runner="pytest", actual=v, expected="100", turn_index=i)
        for i, v in enumerate(["400", "200", "400"], start=1)
    ]
    return CompactionSnapshot(
        session_id="render-sess",
        pins=[PinEntry(pin_id="pin-manual-1", text="Never push to main.", source="manual")],
        trajectories=[
            TestTrajectory(
                test_id=test_id,
                runner="pytest",
                status="OSCILLATING",
                attempts=attempts,
                summary=f"OSCILLATING: {test_id} [400 -> 200 -> 400; expected 100]",
            )
        ],
        thrash_entries=[
            EditThrashEntry(
                path="/work/acme/src/billing/tax.py",
                edit_count=2,
                failure_count=3,
                reverted=True,
                reason=(
                    "Reverted prior edit from turn 7 on /work/acme/src/billing/tax.py "
                    "while failures persisted (2 edits, 3 failures)"
                ),
            )
        ],
        modified_files=["/work/acme/src/billing/tax.py", "/tmp/scratch.py"],
        metadata={"project_dir": project_dir} if project_dir else {},
    )


def test_failure_lines_do_not_repeat_status_id_path_or_counts() -> None:
    out = render_snapshot(_ledger_snapshot())
    assert (
        "- [OSCILLATING] tests/test_billing.py::test_tax_cap (pytest) "
        "[400 -> 200 -> 400; expected 100]"
    ) in out
    assert out.count("OSCILLATING") == 1
    assert (
        "- [THRASH/REVERT] src/billing/tax.py (2 edits, 3 failures): "
        "Reverted prior edit from turn 7 while failures persisted"
    ) in out
    assert out.count("2 edits, 3 failures") == 1


def test_paths_inside_the_project_are_shown_relative() -> None:
    out = render_snapshot(_ledger_snapshot())
    assert "\n- src/billing/tax.py" in out
    assert "/work/acme/" not in out
    assert "\n- /tmp/scratch.py" in out

    absolute = render_snapshot(_ledger_snapshot(project_dir=None))
    assert "\n- /work/acme/src/billing/tax.py" in absolute


def test_preamble_tells_the_agent_how_to_use_the_ledger_and_never_costs_content() -> None:
    snapshot = _ledger_snapshot()
    full = render_snapshot(snapshot)
    assert full.splitlines()[1].startswith("Recorded verbatim before compaction.")
    assert "follow these" in full

    body = "\n".join(line for i, line in enumerate(full.splitlines()) if i != 1)
    tight = render_snapshot(snapshot, max_chars=len(body) + 10)
    assert "Recorded verbatim" not in tight
    assert tight == body
    assert len(render_snapshot(snapshot, max_chars=120)) <= 120


def test_rules_from_one_message_render_in_the_order_the_user_wrote_them() -> None:
    snapshot = CompactionSnapshot(session_id="order", pins=extract_pins(_turns(BRIEF)))
    out = render_snapshot(snapshot)
    assert out.index("Never touch migrations") < out.index("Don't add any") < out.index("All money")


# ---------------------------------------------------------------------------
# One store per project, whatever directory the hook or the agent runs from
# ---------------------------------------------------------------------------


def test_find_project_root_prefers_existing_store_then_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    deep = repo / "packages" / "api" / "src"
    deep.mkdir(parents=True)
    plain = tmp_path / "plain" / "sub"
    plain.mkdir(parents=True)

    assert find_project_root(plain) == plain.resolve()

    (repo / ".git").mkdir()
    assert find_project_root(deep) == repo.resolve()

    (repo / "packages" / "api" / ".compact-ledger").mkdir()
    assert find_project_root(deep) == (repo / "packages" / "api").resolve()


def test_hook_roundtrip_survives_a_cwd_change_and_a_pin_from_a_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(ENV_STORE_DIR, raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    repo = tmp_path / "repo"
    sub = repo / "services" / "ledger"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    transcript = repo / "t.jsonl"
    transcript.write_text(_claude_user("Never touch db/migrations/.") + "\n", encoding="utf-8")

    pre = handle_pre_compact(
        json.dumps({"session_id": "s1", "transcript_path": str(transcript), "cwd": str(sub)}),
        env={},
    )
    assert pre.exit_code == 0 and pre.stderr == ""
    assert (repo / ".compact-ledger" / "sessions" / "s1.json").is_file()
    assert not (sub / ".compact-ledger").exists()

    # The agent pins from a different subdirectory with no --session-id.
    monkeypatch.chdir(sub)
    monkeypatch.setenv(ENV_CLAUDE_SESSION_ID, "s1")
    assert main(["pin", "Deploy freeze until Dec 1", "--negative", "--json"]) == 0
    pinned = json.loads(capsys.readouterr().out)
    assert pinned["session_id"] == "s1"
    assert pinned["is_negative"] is True
    assert Path(pinned["path"]).parent.parent.parent == repo.resolve()

    start = handle_session_start(
        io.StringIO(json.dumps({"session_id": "s1", "source": "compact", "cwd": str(repo)})),
        env={},
    )
    context = json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[MANUAL] [DO NOT] Deploy freeze until Dec 1" in context
    assert "Never touch db/migrations/." in context


def test_hook_store_prefers_claude_project_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    store = resolve_hook_store(payload_cwd=str(elsewhere), env={"CLAUDE_PROJECT_DIR": str(project)})
    assert store.root_dir == project.resolve() / ".compact-ledger"


def test_pre_compact_records_project_dir_for_relative_paths(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_claude_user("Never push to main.") + "\n", encoding="utf-8")
    payload = {"session_id": "s2", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert handle_pre_compact(json.dumps(payload), env={}).exit_code == 0
    snapshot = SessionStore(base_dir=tmp_path).load_snapshot("s2")
    assert snapshot is not None
    assert snapshot.metadata["project_dir"] == str(tmp_path.resolve())


def test_store_ignores_itself_in_git(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.add_manual_pin("s", "Never commit secrets.")
    ignore = tmp_path / ".compact-ledger" / ".gitignore"
    assert ignore.read_text(encoding="utf-8").splitlines()[-1] == "*"

    ignore.write_text("custom\n", encoding="utf-8")
    store.add_manual_pin("s", "Second pin.")
    assert ignore.read_text(encoding="utf-8") == "custom\n"


# ---------------------------------------------------------------------------
# CLI ergonomics for agents
# ---------------------------------------------------------------------------


def test_default_session_id_precedence() -> None:
    assert default_session_id({}) is None
    assert default_session_id({ENV_CLAUDE_SESSION_ID: " abc "}) == "abc"
    assert default_session_id({ENV_CLAUDE_SESSION_ID: "abc", ENV_SESSION_ID: "mine"}) == "mine"


def test_session_commands_use_ambient_session_id_and_explain_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = str(tmp_path / "store")
    monkeypatch.delenv(ENV_SESSION_ID, raising=False)
    monkeypatch.delenv(ENV_CLAUDE_SESSION_ID, raising=False)

    for argv in (["pin", "x"], ["unpin", "x"], ["inject"], ["clear"]):
        assert main([*argv, "--store-dir", store_dir]) == 2
        err = capsys.readouterr().err
        assert "--session-id" in err and ENV_CLAUDE_SESSION_ID in err

    monkeypatch.setenv(ENV_CLAUDE_SESSION_ID, "ambient")
    assert main(["pin", "Use July 31.", "--tag", "billing", "--store-dir", store_dir]) == 0
    capsys.readouterr()

    assert main(["inject", "--store-dir", store_dir]) == 0
    assert "[MANUAL] [tags: billing] Use July 31." in capsys.readouterr().out

    assert main(["status", "--store-dir", store_dir]) == 0
    assert "* ambient" in capsys.readouterr().out

    assert main(["status", "--store-dir", store_dir, "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["current_session_id"] == "ambient"
    assert listing["store_dir"] == store_dir

    # An explicit flag still wins over the environment.
    assert main(["inject", "--session-id", "other", "--store-dir", store_dir]) == 1
    assert "No snapshot found for session 'other'" in capsys.readouterr().err


def test_status_lists_manual_pins_so_they_can_be_unpinned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = str(tmp_path / "store")
    assert (
        main(["pin", "--session-id", "s", "--store-dir", store_dir, "Never push.", "--negative"])
        == 0
    )
    capsys.readouterr()

    assert main(["status", "--session-id", "s", "--store-dir", store_dir, "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["pins"] == [
        {
            "pin_id": info["pins"][0]["pin_id"],
            "text": "Never push.",
            "is_negative": True,
            "tags": [],
        }
    ]

    assert main(["status", "--session-id", "s", "--store-dir", store_dir]) == 0
    text = capsys.readouterr().out
    assert f"- {info['pins'][0]['pin_id']} [DO NOT] Never push." in text

    assert main(["unpin", "--session-id", "s", "--store-dir", store_dir, "missing"]) == 1
    assert "status --session-id s" in capsys.readouterr().err
    pin_id = info["pins"][0]["pin_id"]
    assert main(["unpin", "--session-id", "s", "--store-dir", store_dir, pin_id]) == 0


def test_init_warns_when_the_hook_command_is_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    assert main(["init", "--write"]) == 0
    captured = capsys.readouterr()
    assert "Updated Claude Code hook settings" in captured.out
    assert "Next:" in captured.out
    assert "Warning: 'compact-ledger' is not on PATH" in captured.err

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    exe = fake_bin / "compact-ledger"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    assert main(["init", "--write"]) == 0
    assert "Warning" not in capsys.readouterr().err


def test_unparseable_transcript_error_names_the_real_problem(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("this is not a transcript\n", encoding="utf-8")
    assert main(["snapshot", "--transcript", str(bad), "--store-dir", str(tmp_path / "s")]) == 2
    assert "No transcript records could be parsed" in capsys.readouterr().err


def test_help_shows_quick_start_and_examples(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as top:
        main(["--help"])
    assert top.value.code == 0
    out = capsys.readouterr().out
    assert "quick start" in out and "exit codes" in out

    with pytest.raises(SystemExit):
        main(["pin", "--help"])
    pin_help = capsys.readouterr().out
    assert "examples:" in pin_help and ENV_CLAUDE_SESSION_ID in pin_help


# ---------------------------------------------------------------------------
# Claude Code plugin packaging
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifests_are_consistent_with_the_package() -> None:
    plugin = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    market = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text("utf-8"))["hooks"]

    assert plugin["name"] == "compact-ledger"
    assert plugin["version"] == __version__
    assert [p["name"] for p in market["plugins"]] == ["compact-ledger"]

    pre = hooks["PreCompact"][0]["hooks"][0]["command"]
    start = hooks["SessionStart"][0]
    assert pre.endswith("hook pre-compact")
    assert start["matcher"] == "compact|resume"
    assert start["hooks"][0]["command"].endswith("hook session-start")
    assert "${CLAUDE_PLUGIN_ROOT}/bin/compact-ledger" in pre

    skill = (REPO_ROOT / "skills" / "compact-ledger" / "SKILL.md").read_text("utf-8")
    assert skill.startswith("---\nname: compact-ledger\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_plugin_launcher_runs_bundled_source_and_hooks_fail_open(tmp_path: Path) -> None:
    launcher = REPO_ROOT / "bin" / "compact-ledger"
    assert os.access(launcher, os.X_OK)
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
    }
    env.pop("PYTHONPATH", None)

    version = subprocess.run(
        [str(launcher), "--version"], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    assert version.returncode == 0
    assert version.stdout.strip() == f"compact-ledger {__version__}"

    hook = subprocess.run(
        [str(launcher), "hook", "pre-compact"],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert hook.returncode == 0
    assert hook.stdout == ""

    # With no usable interpreter at all, hooks still exit 0 and other commands explain why.
    bare = {"PATH": "/nonexistent", "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}
    silent = subprocess.run(
        ["/bin/sh", str(launcher), "hook", "session-start"],
        input="{}",
        capture_output=True,
        text=True,
        env=bare,
        cwd=tmp_path,
    )
    assert silent.returncode == 0
    assert silent.stdout == ""
    loud = subprocess.run(
        ["/bin/sh", str(launcher), "status"], capture_output=True, text=True, env=bare, cwd=tmp_path
    )
    assert loud.returncode == 127
    assert "Python >= 3.11" in loud.stderr
