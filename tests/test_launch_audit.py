"""Regression tests from the public-launch audit and hardening pass."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from compact_ledger.cli import main
from compact_ledger.doctor import run_doctor
from compact_ledger.hooks import (
    create_and_save_snapshot,
    handle_pre_compact,
    handle_session_start,
)
from compact_ledger.models import CompactionSnapshot, PinEntry, ToolExecution, UserTurn
from compact_ledger.parsers.runners import parse_runner_output
from compact_ledger.parsers.transcript import parse_transcript_lines
from compact_ledger.pins import extract_pins
from compact_ledger.store import SessionStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_store_rejects_intra_store_session_symlink_and_lock_symlink(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX symlink test")

    store = SessionStore(store_dir=tmp_path / "store")
    store.add_manual_pin("victim", "Keep victim data intact.")
    victim_path = store.session_path("victim")

    # Symlink inside sessions_dir pointing to another session file in the same directory
    attacker_link = store.sessions_dir / "attacker.json"
    attacker_link.symlink_to(victim_path)
    with pytest.raises(ValueError, match="symlinked session file"):
        store.session_path("attacker")
    with pytest.raises(ValueError, match="symlinked session file"):
        store.load_snapshot("attacker")
    with pytest.raises(ValueError, match="symlinked session file"):
        store.add_manual_pin("attacker", "Overwrite attempt")

    # Victim session must remain completely untouched
    victim_pins = store.list_manual_pins("victim")
    assert [p.text for p in victim_pins] == ["Keep victim data intact."]

    # Symlinked lock file is also rejected before opening
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("original", encoding="utf-8")
    lock_link = store.sessions_dir / ".locked-sess.lock"
    lock_link.symlink_to(decoy)
    with pytest.raises(ValueError, match="symlinked session lock file"):
        store.add_manual_pin("locked-sess", "Rule")
    assert decoy.read_text(encoding="utf-8") == "original"


def test_store_concurrent_pins_and_snapshots_preserve_all_manual_pins(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path / "concurrent")
    sid = "race-sess"

    def _pin_worker(idx: int) -> None:
        store.add_manual_pin(sid, f"Rule number {idx}", tags=[f"t{idx}"])

    def _snapshot_worker(idx: int) -> None:
        snap = CompactionSnapshot(
            session_id=sid,
            pins=[
                PinEntry(
                    pin_id=f"pin-auto-{idx}",
                    text=f"Auto rule {idx}",
                    source="auto",
                    turn_index=idx + 1,
                )
            ],
        )
        store.save_snapshot(snap)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_pin_worker, i) for i in range(12)]
        futures.extend(pool.submit(_snapshot_worker, i) for i in range(6))
        for fut in futures:
            fut.result()

    loaded = store.load_snapshot(sid)
    assert loaded is not None
    manual_texts = {p.text for p in loaded.pins if p.source == "manual"}
    assert manual_texts == {f"Rule number {i}" for i in range(12)}


def test_store_clear_session_is_non_mutating_when_absent(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path / "never-created")
    assert store.clear_session("ghost") is False
    assert not (tmp_path / "never-created").exists()


def test_retraction_plus_replacement_in_one_turn_stays_one_override_and_retires_ban() -> None:
    turns = [
        UserTurn(1, "Use pnpm for frontend.", seq=1),
        UserTurn(2, "Never use npm.", seq=2),
        UserTurn(3, "Ignore my previous rule about pnpm. Stick with npm.", seq=3),
    ]
    pins = extract_pins(turns)
    active = [p for p in pins if p.superseded_by is None]
    assert len(active) == 1
    assert active[0].source == "override"
    assert active[0].text == "Ignore my previous rule about pnpm. Stick with npm."
    assert active[0].supersedes is not None

    # Retraction + replacement followed by an unrelated rule splits into 2 groups cleanly
    turns2 = [
        UserTurn(1, "Use Postgres for storage.", seq=1),
        UserTurn(
            2,
            "Ignore what I said earlier about Postgres. Switch to SQLite. "
            "Never touch db/migrations/.",
            seq=2,
        ),
    ]
    pins2 = extract_pins(turns2)
    active2 = [p for p in pins2 if p.superseded_by is None]
    assert [p.text for p in active2] == [
        "Ignore what I said earlier about Postgres. Switch to SQLite.",
        "Never touch db/migrations/.",
    ]
    assert active2[0].source == "override"
    assert active2[1].is_negative is True


def test_unicode_constraints_overrides_and_store_roundtrip(tmp_path: Path) -> None:
    turns = [
        UserTurn(1, "Always keep UTF-8 labels like café and 日本語 intact.", seq=1),
        UserTurn(2, "Never touch données/migrations/.", seq=2),
    ]
    pins = extract_pins(turns)
    assert len(pins) == 2
    assert pins[1].is_negative is True

    store = SessionStore(store_dir=tmp_path / "unicode-store")
    store.save_snapshot(CompactionSnapshot(session_id="séssion-日本語", pins=pins))
    loaded = store.load_snapshot("séssion-日本語")
    assert loaded is not None
    assert [p.text for p in loaded.pins] == [p.text for p in pins]
    assert store.list_sessions() == ["séssion-日本語"]


def test_js_runner_strips_decimal_durations_from_inline_test_names() -> None:
    output = (
        "FAIL src/ui/Invoice.test.ts\n"
        "  × renders invoice total 1.25s\n"
        "  × formats currency 12.5ms\n"
        "  ✓ loads defaults (1.5 s)\n"
    )
    exe = ToolExecution(
        turn_index=1,
        tool_name="Bash",
        command="npx vitest run",
        output=output,
        exit_code=1,
        is_error=True,
    )
    attempts = parse_runner_output(exe)
    by_id = {a.test_id: a.outcome for a in attempts}
    assert by_id["src/ui/Invoice.test.ts > renders invoice total"] == "failed"
    assert by_id["src/ui/Invoice.test.ts > formats currency"] == "failed"
    assert by_id["src/ui/Invoice.test.ts > loads defaults"] == "passed"


def test_codex_command_exited_with_code_header_extracts_exit_code() -> None:
    records = [
        {"type": "session_meta", "payload": {"id": "codex-ec"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "make lint"}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Command exited with code 2\nOutput:\nError: lint failed\n",
            },
        },
    ]
    parsed = parse_transcript_lines([json.dumps(r) for r in records])
    assert len(parsed.tool_executions) == 1
    assert parsed.tool_executions[0].exit_code == 2
    assert parsed.tool_executions[0].is_error is True
    assert parsed.tool_executions[0].output == "Error: lint failed\n"


def test_pre_compact_from_subdirectory_relativizes_paths_to_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sub = repo / "packages" / "web"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()

    edited_file = str((repo / "src" / "app.py").resolve())
    transcript = repo / "t.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "sub-rel",
            "message": {"role": "user", "content": "Never break relative paths."},
        },
        {
            "type": "assistant",
            "sessionId": "sub-rel",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "Edit",
                        "input": {
                            "file_path": edited_file,
                            "old_string": "a",
                            "new_string": "b",
                        },
                    }
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    pre = handle_pre_compact(
        json.dumps({"session_id": "sub-rel", "transcript_path": str(transcript), "cwd": str(sub)}),
        env={},
    )
    assert pre.exit_code == 0

    start = handle_session_start(
        json.dumps({"session_id": "sub-rel", "source": "compact", "cwd": str(sub)}),
        env={},
    )
    assert start.exit_code == 0
    ctx = json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "- src/app.py" in ctx
    assert edited_file not in ctx


def test_doctor_does_not_double_count_mismatched_session_file(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    store = SessionStore(base_dir=project)
    store.add_manual_pin("real-sess", "Keep counts accurate.")

    # Stray file with mismatched stem referencing the real session_id
    (store.sessions_dir / "stray-copy.json").write_text(
        store.session_path("real-sess").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    report = run_doctor(cwd=project, env={"PATH": str(tmp_path / "empty")}, home_dir=tmp_path)
    store_check = next(c for c in report.checks if c.name == "store")
    assert store_check.status == "warn"
    assert store_check.details["session_count"] == 1
    assert store_check.details["total_pins"] == 1
    assert store_check.details["corrupt_files"] == 1


def test_cli_unpin_returns_exit_code_1_on_corrupt_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SessionStore(store_dir=tmp_path / "store")
    store.add_manual_pin("s1", "Rule")
    store.session_path("s1").write_text("{corrupt", encoding="utf-8")

    rc = main(["unpin", "--store-dir", str(store.root_dir), "--session-id", "s1", "Rule"])
    assert rc == 1
    assert "Failed to load snapshot" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
def test_plugin_launcher_handles_store_dir_before_hook_and_strict_flag(tmp_path: Path) -> None:
    launcher = REPO_ROOT / "bin" / "compact-ledger"
    bare = {"PATH": "/nonexistent", "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}

    fail_open = subprocess.run(
        ["/bin/sh", str(launcher), "--store-dir", str(tmp_path), "hook", "pre-compact"],
        input="{}",
        capture_output=True,
        text=True,
        env=bare,
        cwd=tmp_path,
    )
    assert fail_open.returncode == 0
    assert fail_open.stdout == ""

    strict = subprocess.run(
        ["/bin/sh", str(launcher), "hook", "pre-compact", "--strict"],
        input="{}",
        capture_output=True,
        text=True,
        env=bare,
        cwd=tmp_path,
    )
    assert strict.returncode == 127
    assert "Python >= 3.11" in strict.stderr


def test_standalone_eval_runner_script_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "evals" / "run_evals.py"), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report["total"] == 24
    assert report["passed"] == 24
    assert report["failed"] == 0


def test_custom_store_dir_dot_in_git_repo_does_not_create_repo_gitignore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)

    rc = main(["pin", "--store-dir", ".", "--session-id", "s1", "Never push to main."])
    assert rc == 0
    # Must NEVER create ./.gitignore ignoring the user's entire git repository
    assert not (repo / ".gitignore").exists()
    # Instead writes .gitignore inside sessions/
    assert (repo / "sessions" / ".gitignore").is_file()
    assert "*" in (repo / "sessions" / ".gitignore").read_text(encoding="utf-8")

    # Default .compact-ledger store still creates .compact-ledger/.gitignore
    default_store = SessionStore(base_dir=repo)
    default_store.add_manual_pin("s2", "Default store rule.")
    assert (repo / ".compact-ledger" / ".gitignore").is_file()
    assert not (repo / ".gitignore").exists()


def test_corrupt_quarantine_creates_unique_backups_and_cli_pin_warns_on_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin("s1", "First pin")
    snap_path = store.session_path("s1")

    # First corruption
    snap_path.write_text("{corrupt-1", encoding="utf-8")
    rc1 = main(["pin", "--store-dir", str(store_dir), "--session-id", "s1", "Recovered 1"])
    assert rc1 == 0
    err1 = capsys.readouterr().err
    assert "Warning:" in err1 and ".corrupt-" in err1

    # Second corruption must create a SECOND distinct backup without overwriting the first
    snap_path.write_text("{corrupt-2", encoding="utf-8")
    rc2 = main(["pin", "--store-dir", str(store_dir), "--session-id", "s1", "Recovered 2"])
    assert rc2 == 0
    err2 = capsys.readouterr().err
    assert "Warning:" in err2 and ".corrupt-" in err2

    backups = sorted(store.sessions_dir.glob(f"{snap_path.name}.corrupt-*"))
    assert len(backups) == 2
    contents = {b.read_text(encoding="utf-8") for b in backups}
    assert contents == {"{corrupt-1", "{corrupt-2"}


def test_clear_session_removes_snapshot_and_corrupt_backups(tmp_path: Path) -> None:
    store = SessionStore(store_dir=tmp_path / "store")
    store.add_manual_pin("s1", "Rule 1")
    snap_path = store.session_path("s1")
    snap_path.write_text("{bad-1", encoding="utf-8")
    store.add_manual_pin("s1", "Rule 2")
    snap_path.write_text("{bad-2", encoding="utf-8")
    store.add_manual_pin("s1", "Rule 3")

    assert len(list(store.sessions_dir.glob(f"{snap_path.name}.corrupt*"))) == 2
    assert store.clear_session("s1") is True
    assert not snap_path.exists()
    assert list(store.sessions_dir.glob(f"{snap_path.name}.corrupt*")) == []
    assert store.clear_session("s1") is False

    # Also returns True when only a legacy/timestamped .corrupt backup remains
    orphan = snap_path.with_name(f"{snap_path.name}.corrupt-orphan")
    orphan.write_text("{orphan", encoding="utf-8")
    assert store.clear_session("s1") is True
    assert not orphan.exists()


def test_pre_compact_does_not_resurrect_pin_removed_by_concurrent_unpin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(store_dir=tmp_path / "store")
    sid = "race-unpin"
    store.add_manual_pin(sid, "Temporary rule to unpin")
    store.add_manual_pin(sid, "Rule that stays")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": sid,
                "message": {"role": "user", "content": "Never touch db/migrations/."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from compact_ledger import hooks as hooks_mod

    orig_parse = hooks_mod.parse_transcript

    def _parse_with_concurrent_unpin(*args: object, **kwargs: object) -> object:
        result = orig_parse(*args, **kwargs)  # type: ignore[arg-type]
        # Simulate user running `compact-ledger unpin` while PreCompact is mid-parse
        assert store.remove_manual_pin(sid, "Temporary rule to unpin") is True
        return result

    monkeypatch.setattr(hooks_mod, "parse_transcript", _parse_with_concurrent_unpin)

    saved, _ = create_and_save_snapshot(transcript, store, session_id=sid)
    manual_texts = [p.text for p in saved.pins if p.source == "manual"]
    assert manual_texts == ["Rule that stays"]
    assert "Temporary rule to unpin" not in manual_texts


def test_cli_symlink_store_errors_return_exit_code_1_consistently(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX symlink test")

    real_dir = tmp_path / "real-store"
    real_dir.mkdir()
    sym_store = tmp_path / "sym-store"
    sym_store.symlink_to(real_dir)

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s1",
                "message": {"role": "user", "content": "Never push to main."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    commands = [
        ["status", "--store-dir", str(sym_store)],
        ["status", "--store-dir", str(sym_store), "--session-id", "s1"],
        ["pin", "--store-dir", str(sym_store), "--session-id", "s1", "Rule"],
        ["unpin", "--store-dir", str(sym_store), "--session-id", "s1", "Rule"],
        ["clear", "--store-dir", str(sym_store), "--session-id", "s1"],
        ["inject", "--store-dir", str(sym_store), "--session-id", "s1"],
        ["snapshot", "--store-dir", str(sym_store), "--transcript", str(transcript)],
    ]
    for argv in commands:
        rc = main(argv)
        err = capsys.readouterr().err
        assert rc == 1, f"Expected exit 1 for {argv!r}, got {rc} (stderr={err!r})"
        assert "symlinked" in err
