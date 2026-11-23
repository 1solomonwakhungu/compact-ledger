"""Tests for compact_ledger.hooks and hook/init CLI subcommands."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from compact_ledger.cli import main
from compact_ledger.hooks import (
    build_claude_code_hooks_config,
    handle_pre_compact,
    handle_session_start,
    init_claude_code_settings,
    merge_claude_code_settings,
    resolve_hook_store,
)
from compact_ledger.models import CompactionSnapshot
from compact_ledger.store import ENV_STORE_DIR, SessionStore


def _write_rich_claude_transcript(path: Path, *, session_id: str = "hook-sess-1") -> None:
    entries = [
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-02-01T10:00:00Z",
            "message": {"role": "user", "content": "Use June 30 for the billing cutoff."},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-02-01T10:00:10Z",
            "message": {"role": "user", "content": "ok thanks"},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-02-01T10:01:00Z",
            "message": {
                "role": "user",
                "content": "Actually use July 31 instead of June 30 for the billing cutoff.",
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-02-01T10:01:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_b1",
                        "name": "Bash",
                        "input": {"command": "pytest tests/test_billing.py"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-02-01T10:01:06Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_b1",
                        "is_error": True,
                        "content": (
                            "====================== test session starts =======================\n"
                            "============================ FAILURES ============================\n"
                            "___________________________ test_sum _____________________________\n"
                            ">       assert total() == 100\n"
                            "E       assert 400 == 100\n\n"
                            "tests/test_billing.py:12: AssertionError\n"
                            "==================== short test summary info =====================\n"
                            "FAILED tests/test_billing.py::test_sum - assert 400 == 100\n"
                            "======================= 1 failed in 0.08s ========================\n"
                        ),
                    }
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_resolve_hook_store_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    env_dir = tmp_path / "env-dir"
    payload_cwd = tmp_path / "proj-cwd"
    payload_cwd.mkdir()
    fallback_cwd = tmp_path / "fallback"
    fallback_cwd.mkdir()

    # 1. Explicit --store-dir wins over everything
    s1 = resolve_hook_store(
        store_dir=explicit,
        payload_cwd=str(payload_cwd),
        env={ENV_STORE_DIR: str(env_dir)},
        fallback_cwd=fallback_cwd,
    )
    assert s1.root_dir == explicit

    # 2. COMPACT_LEDGER_DIR wins over payload cwd and fallback
    s2 = resolve_hook_store(
        store_dir=None,
        payload_cwd=str(payload_cwd),
        env={ENV_STORE_DIR: str(env_dir)},
        fallback_cwd=fallback_cwd,
    )
    assert s2.root_dir == env_dir

    # 3. Payload cwd wins over fallback when valid directory
    s3 = resolve_hook_store(
        store_dir=None,
        payload_cwd=str(payload_cwd),
        env={},
        fallback_cwd=fallback_cwd,
    )
    assert s3.root_dir == payload_cwd / ".compact-ledger"

    # 4. Invalid / nonexistent payload cwd falls back cleanly
    s4 = resolve_hook_store(
        store_dir=None,
        payload_cwd=str(tmp_path / "missing-dir"),
        env={},
        fallback_cwd=fallback_cwd,
    )
    assert s4.root_dir == fallback_cwd / ".compact-ledger"


def test_pre_compact_and_session_start_roundtrip_and_cwd_resolution(tmp_path: Path) -> None:
    workspace = tmp_path / "my-repo"
    workspace.mkdir()
    transcript = workspace / "session.jsonl"
    _write_rich_claude_transcript(transcript, session_id="s-hook-1")

    # Pre-existing manual pin in workspace store
    store = SessionStore(base_dir=workspace)
    store.add_manual_pin("s-hook-1", "Never drop production tables.", tags=["db"], is_negative=True)

    # PreCompact uses relative transcript_path + payload cwd
    pre_payload = json.dumps(
        {
            "session_id": "s-hook-1",
            "transcript_path": "session.jsonl",
            "cwd": str(workspace),
            "trigger": "auto",
        }
    )
    pre_res = handle_pre_compact(pre_payload, env={})
    assert pre_res.exit_code == 0
    assert pre_res.stdout == ""

    saved = store.load_snapshot("s-hook-1")
    assert saved is not None
    assert saved.metadata.get("trigger") == "auto"
    assert any(p.source == "manual" and "production tables" in p.text for p in saved.pins)
    assert any(p.source == "override" and "July 31" in p.text for p in saved.pins)
    assert len(saved.trajectories) == 1

    # SessionStart with source="compact" and source="resume" (case-insensitive)
    for src in ("compact", "resume", "COMPACT", "Resume"):
        start_res = handle_session_start(
            json.dumps({"session_id": "s-hook-1", "source": src, "cwd": str(workspace)}),
            max_chars=2000,
            env={},
        )
        assert start_res.exit_code == 0
        payload = json.loads(start_res.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert len(ctx) <= 2000
        assert "Never drop production tables." in ctx
        assert "July 31" in ctx
        assert "tests/test_billing.py::test_sum" in ctx


def test_session_start_gating_startup_clear_empty_and_always(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin("s-gate", "Keep runtime deps at zero.")
    store.save_snapshot(CompactionSnapshot(session_id="s-empty"))

    # startup / clear / missing source -> empty stdout, exit 0 (even with strict=True)
    for src in ("startup", "clear", None, "other"):
        body: dict[str, object] = {"session_id": "s-gate"}
        if src is not None:
            body["source"] = src
        res = handle_session_start(json.dumps(body), store_dir=store_dir, strict=True, env={})
        assert res.exit_code == 0
        assert res.stdout == ""

    # Missing snapshot -> empty stdout, exit 0
    res_missing = handle_session_start(
        json.dumps({"session_id": "no-such-session", "source": "compact"}),
        store_dir=store_dir,
        strict=True,
        env={},
    )
    assert res_missing.exit_code == 0
    assert res_missing.stdout == ""

    # Empty snapshot -> empty stdout, exit 0
    res_empty = handle_session_start(
        json.dumps({"session_id": "s-empty", "source": "compact"}),
        store_dir=store_dir,
        strict=True,
        env={},
    )
    assert res_empty.exit_code == 0
    assert res_empty.stdout == ""

    # --always injects even when source is "startup"
    res_always = handle_session_start(
        json.dumps({"session_id": "s-gate", "source": "startup"}),
        store_dir=store_dir,
        always=True,
        env={},
    )
    assert res_always.exit_code == 0
    out = json.loads(res_always.stdout)
    assert "Keep runtime deps at zero." in out["hookSpecificOutput"]["additionalContext"]


def test_hooks_fail_open_vs_strict_on_bad_inputs_and_corrupt_store(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    store = SessionStore(store_dir=store_dir)
    corrupt_path = store.session_path("s-corrupt")
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{corrupt-json", encoding="utf-8")

    bad_payloads: list[str | bytes] = [
        "",
        "   ",
        "{not-json",
        "[]",
        '"just a string"',
        b"\xff\xfe\x00\x01",
        json.dumps({"session_id": "s1"}),
        json.dumps({"transcript_path": str(tmp_path / "missing.jsonl")}),
        json.dumps({"session_id": "s1", "transcript_path": str(tmp_path / "missing.jsonl")}),
    ]
    for bad in bad_payloads:
        r_open = handle_pre_compact(bad, store_dir=store_dir, strict=False, env={})
        assert r_open.exit_code == 0
        assert r_open.stdout == ""

        r_strict = handle_pre_compact(bad, store_dir=store_dir, strict=True, env={})
        assert r_strict.exit_code == 1
        assert r_strict.stdout == ""
        assert r_strict.stderr != ""

    # SessionStart on corrupt snapshot, bad JSON, invalid UTF-8, invalid max_chars
    ss_bad_cases: list[tuple[str | bytes, int]] = [
        ("", 6000),
        ("{bad", 6000),
        (b"\xff\xfe", 6000),
        (json.dumps({"source": "compact"}), 6000),
        (json.dumps({"session_id": "s-corrupt", "source": "compact"}), 6000),
        (json.dumps({"session_id": "s-corrupt", "source": "compact"}), 0),
    ]
    for payload, max_c in ss_bad_cases:
        r_open = handle_session_start(
            payload, store_dir=store_dir, max_chars=max_c, strict=False, env={}
        )
        assert r_open.exit_code == 0
        assert r_open.stdout == ""

        r_strict = handle_session_start(
            payload, store_dir=store_dir, max_chars=max_c, strict=True, env={}
        )
        assert r_strict.exit_code == 1
        assert r_strict.stdout == ""

    # PreCompact recovers cleanly when existing snapshot is corrupt (quarantines & overwrites)
    transcript = tmp_path / "valid.jsonl"
    _write_rich_claude_transcript(transcript, session_id="s-corrupt")
    r_recover = handle_pre_compact(
        json.dumps({"session_id": "s-corrupt", "transcript_path": str(transcript)}),
        store_dir=store_dir,
        strict=True,
        env={},
    )
    assert r_recover.exit_code == 0
    recovered = store.load_snapshot("s-corrupt")
    assert recovered is not None
    assert any("July 31" in p.text for p in recovered.pins)


def test_hook_cli_subcommands_via_subprocess(tmp_path: Path) -> None:
    store_dir = tmp_path / "cli-hook-store"
    transcript = tmp_path / "sess.jsonl"
    _write_rich_claude_transcript(transcript, session_id="sub-1")

    # 1. PreCompact via CLI subprocess
    pre_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "compact_ledger",
            "hook",
            "pre-compact",
            "--store-dir",
            str(store_dir),
        ],
        input=json.dumps({"session_id": "sub-1", "transcript_path": str(transcript)}).encode(
            "utf-8"
        ),
        capture_output=True,
        check=False,
    )
    assert pre_proc.returncode == 0
    assert pre_proc.stdout == b""

    # 2. SessionStart via CLI subprocess
    ss_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "compact_ledger",
            "hook",
            "session-start",
            "--store-dir",
            str(store_dir),
            "--max-chars",
            "1500",
        ],
        input=json.dumps({"session_id": "sub-1", "source": "compact"}).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert ss_proc.returncode == 0
    parsed = json.loads(ss_proc.stdout.decode("utf-8"))
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "July 31" in ctx
    assert len(ctx) <= 1500

    # 3. Invalid binary stdin fails open by default and fails non-zero with --strict
    bad_open = subprocess.run(
        [
            sys.executable,
            "-m",
            "compact_ledger",
            "hook",
            "pre-compact",
            "--store-dir",
            str(store_dir),
        ],
        input=b"\xff\xfe\xfd",
        capture_output=True,
        check=False,
    )
    assert bad_open.returncode == 0
    assert bad_open.stdout == b""

    bad_strict = subprocess.run(
        [
            sys.executable,
            "-m",
            "compact_ledger",
            "hook",
            "pre-compact",
            "--store-dir",
            str(store_dir),
            "--strict",
        ],
        input=b"\xff\xfe\xfd",
        capture_output=True,
        check=False,
    )
    assert bad_strict.returncode == 1
    assert bad_strict.stdout == b""


def test_init_claude_code_print_write_merge_and_idempotency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"

    # 1. Print mode (no --write) prints valid JSON and does not create files
    rc = main(["init", "--claude-code", "--settings-path", str(settings_path)])
    assert rc == 0
    assert not settings_path.exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed == build_claude_code_hooks_config()
    assert (
        printed["hooks"]["PreCompact"][0]["hooks"][0]["command"]
        == "compact-ledger hook pre-compact"
    )
    assert printed["hooks"]["SessionStart"][0]["matcher"] == "compact|resume"
    assert (
        printed["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        == "compact-ledger hook session-start"
    )

    # 2. Seed existing settings with unrelated keys and other hooks
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "model": "claude-sonnet",
        "permissions": {"allow": ["Bash(pytest:*)"]},
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo edited"}]}
            ],
            "PreCompact": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo custom-pre"}]}
            ],
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}
            ],
        },
    }
    settings_path.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")

    # 3. Print mode shows merged output without modifying file on disk
    rc = main(["init", "--claude-code", "--settings-path", str(settings_path)])
    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["model"] == "claude-sonnet"
    assert len(preview["hooks"]["PreCompact"]) == 2
    assert json.loads(settings_path.read_text(encoding="utf-8")) == seed

    # 4. --write merges atomically and preserves all existing keys & hooks
    rc = main(["init", "--claude-code", "--write", "--settings-path", str(settings_path)])
    assert rc == 0
    assert "Updated Claude Code hook settings" in capsys.readouterr().out
    written_1 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written_1["model"] == "claude-sonnet"
    assert written_1["permissions"] == {"allow": ["Bash(pytest:*)"]}
    assert written_1["hooks"]["PostToolUse"] == seed["hooks"]["PostToolUse"]
    assert len(written_1["hooks"]["PreCompact"]) == 2
    assert len(written_1["hooks"]["SessionStart"]) == 2

    # 5. Double --write is strictly idempotent (zero duplicates)
    rc = main(["init", "--claude-code", "--write", "--settings-path", str(settings_path)])
    assert rc == 0
    capsys.readouterr()
    written_2 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert written_2 == written_1

    # 6. Changing --command updates in place without duplicating
    rc = main(
        [
            "init",
            "--claude-code",
            "--write",
            "--settings-path",
            str(settings_path),
            "--command",
            "uvx compact-ledger",
        ]
    )
    assert rc == 0
    capsys.readouterr()
    written_3 = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(written_3["hooks"]["PreCompact"]) == 2
    assert len(written_3["hooks"]["SessionStart"]) == 2
    assert (
        written_3["hooks"]["PreCompact"][1]["hooks"][0]["command"]
        == "uvx compact-ledger hook pre-compact"
    )
    assert (
        written_3["hooks"]["SessionStart"][1]["hooks"][0]["command"]
        == "uvx compact-ledger hook session-start"
    )

    # 7. Custom wrapper --command is also idempotent on double --write
    custom_path = tmp_path / "custom-settings.json"
    for _ in range(2):
        rc = main(
            [
                "init",
                "--claude-code",
                "--write",
                "--settings-path",
                str(custom_path),
                "--command",
                "/opt/bin/cl-wrap",
            ]
        )
        assert rc == 0
        capsys.readouterr()
    custom_cfg = json.loads(custom_path.read_text(encoding="utf-8"))
    assert len(custom_cfg["hooks"]["PreCompact"]) == 1
    assert len(custom_cfg["hooks"]["SessionStart"]) == 1


def test_init_claude_code_refuses_corrupt_or_invalid_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Missing --claude-code flag exits 2
    assert main(["init"]) == 2
    assert "--claude-code" in capsys.readouterr().err

    bad_contents = [
        "{not valid json",
        "",
        "[]",
        '"a string"',
        json.dumps({"hooks": "not-an-object"}),
        json.dumps({"hooks": {"PreCompact": "not-a-list"}}),
    ]
    for idx, content in enumerate(bad_contents):
        target = tmp_path / f"bad_{idx}.json"
        target.write_text(content, encoding="utf-8")
        rc = main(["init", "--claude-code", "--write", "--settings-path", str(target)])
        assert rc == 1
        assert "Error:" in capsys.readouterr().err
        # Original file MUST remain completely untouched
        assert target.read_text(encoding="utf-8") == content

    # Invalid --command rejected cleanly
    with pytest.raises(ValueError, match="non-empty"):
        init_claude_code_settings(settings_path=tmp_path / "x.json", command="   ")
    with pytest.raises(ValueError, match="single-line"):
        merge_claude_code_settings({}, command="compact-ledger\nrm -rf /")


def test_hook_handles_text_io_stream_directly(tmp_path: Path) -> None:
    store_dir = tmp_path / "stream-store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin("stream-s1", "Stream pin works.")

    stream = io.StringIO(json.dumps({"session_id": "stream-s1", "source": "compact"}))
    res = handle_session_start(stream, store_dir=store_dir, env={})
    assert res.exit_code == 0
    assert "Stream pin works." in json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_fail_open_on_bad_flags_broken_pipe_and_closed_fds(tmp_path: Path) -> None:
    import os

    store_dir = tmp_path / "fd-store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin("fd-s1", "Keep hooks fail-open under all I/O faults.")

    # 1. Bad CLI flags exit 0 without --strict and exit 2 with --strict
    bad_flag_invocations = [
        ["hook", "pre-compact", "--bogus"],
        ["hook", "session-start", "--max-chars", "abc"],
        ["hook", "session-start", "--max-chars"],
        ["hook", "pre-compact", "extra-positional"],
        ["--store-dir", str(store_dir), "hook", "session-start", "--unknown-flag"],
    ]
    for argv in bad_flag_invocations:
        proc_open = subprocess.run(
            [sys.executable, "-m", "compact_ledger", *argv],
            input=b"{}",
            capture_output=True,
            check=False,
        )
        assert proc_open.returncode == 0, f"Expected fail-open exit 0 for {argv}: {proc_open}"
        assert proc_open.stdout == b""

        proc_strict = subprocess.run(
            [sys.executable, "-m", "compact_ledger", *argv, "--strict"],
            input=b"{}",
            capture_output=True,
            check=False,
        )
        assert proc_strict.returncode == 2, f"Expected strict exit 2 for {argv}: {proc_strict}"

    # 2. Broken stdout pipe (reader closes immediately) must exit 0, NOT 120
    payload = json.dumps({"session_id": "fd-s1", "source": "compact"}).encode("utf-8")
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        broken_pipe_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "compact_ledger",
                "hook",
                "session-start",
                "--store-dir",
                str(store_dir),
            ],
            input=payload,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        os.close(write_fd)
    assert broken_pipe_proc.returncode == 0, broken_pipe_proc.stderr

    # 3. Closed stdout (>&-) on session-start exits 0 without traceback
    closed_out = subprocess.run(
        f'"{sys.executable}" -m compact_ledger hook session-start --store-dir "{store_dir}" >&-',
        shell=True,
        input=payload,
        capture_output=True,
        check=False,
    )
    assert closed_out.returncode == 0, closed_out.stderr
    assert b"Traceback" not in closed_out.stderr

    # 4. Closed stderr (2>&-) on malformed stdin and bad flags exits 0 for both hooks
    for sub in ("pre-compact", "session-start"):
        closed_err = subprocess.run(
            f'"{sys.executable}" -m compact_ledger hook {sub} --store-dir "{store_dir}" 2>&-',
            shell=True,
            input=b"{broken-json",
            capture_output=True,
            check=False,
        )
        assert closed_err.returncode == 0
        assert closed_err.stdout == b""

        closed_err_bad_flag = subprocess.run(
            f'"{sys.executable}" -m compact_ledger hook {sub} --bogus 2>&-',
            shell=True,
            input=b"{}",
            capture_output=True,
            check=False,
        )
        assert closed_err_bad_flag.returncode == 0
        assert closed_err_bad_flag.stdout == b""

        closed_in = subprocess.run(
            f'"{sys.executable}" -m compact_ledger hook {sub} --store-dir "{store_dir}" <&-',
            shell=True,
            capture_output=True,
            check=False,
        )
        assert closed_in.returncode == 0
        assert closed_in.stdout == b""


def test_init_claude_code_refuses_symlinks_preserves_mode_and_rejects_nan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import stat

    # 1. Valid symlinked settings.json must be refused without touching target or link
    real_target = tmp_path / "real-settings.json"
    real_target.write_text('{"model": "original"}\n', encoding="utf-8")
    link_path = tmp_path / "settings-link.json"
    link_path.symlink_to(real_target)

    rc = main(["init", "--claude-code", "--write", "--settings-path", str(link_path)])
    assert rc == 1
    assert "symlink" in capsys.readouterr().err.lower()
    assert link_path.is_symlink()
    assert json.loads(real_target.read_text(encoding="utf-8")) == {"model": "original"}

    # 2. Broken symlink must also be refused without replacing it
    broken_link = tmp_path / "broken-settings.json"
    broken_link.symlink_to(tmp_path / "does-not-exist.json")
    rc = main(["init", "--claude-code", "--write", "--settings-path", str(broken_link)])
    assert rc == 1
    assert "symlink" in capsys.readouterr().err.lower()
    assert broken_link.is_symlink()

    # 3. Symlinked parent directory refused
    real_dir = tmp_path / "real-claude"
    real_dir.mkdir()
    link_dir = tmp_path / "link-claude"
    link_dir.symlink_to(real_dir)
    rc = main(
        ["init", "--claude-code", "--write", "--settings-path", str(link_dir / "settings.json")]
    )
    assert rc == 1
    assert "symlink" in capsys.readouterr().err.lower()

    # 4. Existing file permissions (0o644) are preserved on write
    regular = tmp_path / "regular-settings.json"
    regular.write_text('{"model": "keep"}\n', encoding="utf-8")
    regular.chmod(0o644)
    rc = main(["init", "--claude-code", "--write", "--settings-path", str(regular)])
    assert rc == 0
    capsys.readouterr()
    assert stat.S_IMODE(regular.stat().st_mode) == 0o644

    # 5. Non-standard JSON values like 1e999 (Infinity) are refused instead of emitting invalid JSON
    inf_file = tmp_path / "inf-settings.json"
    inf_file.write_text('{"limit": 1e999}\n', encoding="utf-8")
    rc = main(["init", "--claude-code", "--write", "--settings-path", str(inf_file)])
    assert rc == 1
    assert "Error:" in capsys.readouterr().err
    assert inf_file.read_text(encoding="utf-8") == '{"limit": 1e999}\n'


def test_init_claude_code_merge_preserves_custom_flags_matchers_and_unrelated_hooks(
    tmp_path: Path,
) -> None:
    # 1. Duplicate compact-ledger hook sharing entry with unrelated hook must NOT mutate matcher
    seed_shared = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "compact-ledger hook session-start --max-chars 3000 --strict"
                            ),
                        }
                    ],
                },
                {
                    "matcher": "compact",
                    "hooks": [
                        {"type": "command", "command": "compact-ledger hook session-start"},
                        {"type": "command", "command": "echo unrelated-hook"},
                    ],
                },
            ],
            "PreCompact": [
                {
                    "matcher": "auto",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "compact-ledger hook pre-compact --store-dir /tmp/custom",
                        }
                    ],
                }
            ],
        }
    }
    merged = merge_claude_code_settings(seed_shared, command="uvx compact-ledger")

    # First SessionStart keeps custom matcher "compact" AND preserves --max-chars 3000 --strict
    ss_entries = merged["hooks"]["SessionStart"]
    assert len(ss_entries) == 2
    assert ss_entries[0]["matcher"] == "compact"
    assert (
        ss_entries[0]["hooks"][0]["command"]
        == "uvx compact-ledger hook session-start --max-chars 3000 --strict"
    )
    # Second entry (same matcher "compact") drops duplicate compact-ledger hook, keeps unrelated
    assert ss_entries[1]["matcher"] == "compact"
    assert ss_entries[1]["hooks"] == [{"type": "command", "command": "echo unrelated-hook"}]

    # PreCompact keeps custom matcher "auto" and --store-dir flag
    pc_entries = merged["hooks"]["PreCompact"]
    assert len(pc_entries) == 1
    assert pc_entries[0]["matcher"] == "auto"
    assert (
        pc_entries[0]["hooks"][0]["command"]
        == "uvx compact-ledger hook pre-compact --store-dir /tmp/custom"
    )

    # 2. Startup entry containing compact-ledger --always + unrelated hook is untouched
    #    and a compact|resume entry is added cleanly (idempotent on second merge)
    seed_startup = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "compact-ledger hook session-start --always",
                        },
                        {"type": "command", "command": "echo unrelated-startup-hook"},
                    ],
                }
            ]
        }
    }
    m1 = merge_claude_code_settings(seed_startup)
    m2 = merge_claude_code_settings(m1)
    assert m1 == m2
    assert m1["hooks"]["SessionStart"][0] == seed_startup["hooks"]["SessionStart"][0]
    assert len(m1["hooks"]["SessionStart"]) == 2
    assert m1["hooks"]["SessionStart"][1]["matcher"] == "compact|resume"


def test_hook_tight_budget_bom_whitespace_store_and_task_notification_filter(
    tmp_path: Path,
) -> None:
    # 1. Whitespace-only store_dir rejected; whitespace-only COMPACT_LEDGER_DIR ignored
    with pytest.raises(ValueError, match="non-empty"):
        SessionStore(store_dir="   ")
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    resolved = resolve_hook_store(
        store_dir=None,
        payload_cwd=None,
        env={ENV_STORE_DIR: "   "},
        fallback_cwd=fallback,
    )
    assert resolved.root_dir == fallback / ".compact-ledger"

    # 2. Tight max_chars in SessionStart genuinely truncates long ledger while keeping manual pin
    store_dir = tmp_path / "budget-store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin("b-sess", "PIN_MUST_SURVIVE")
    transcript = tmp_path / "t.jsonl"
    _write_rich_claude_transcript(transcript, session_id="b-sess")
    pre_res = handle_pre_compact(
        b"\xef\xbb\xbf"
        + json.dumps({"session_id": "b-sess", "transcript_path": str(transcript)}).encode("utf-8"),
        store_dir=store_dir,
        strict=True,
        env={},
    )
    assert pre_res.exit_code == 0

    full_res = handle_session_start(
        json.dumps({"session_id": "b-sess", "source": "compact"}),
        store_dir=store_dir,
        max_chars=6000,
        env={},
    )
    tight_res = handle_session_start(
        json.dumps({"session_id": "b-sess", "source": "compact"}),
        store_dir=store_dir,
        max_chars=180,
        env={},
    )
    full_ctx = json.loads(full_res.stdout)["hookSpecificOutput"]["additionalContext"]
    tight_ctx = json.loads(tight_res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(tight_ctx) <= 180 < len(full_ctx)
    assert "PIN_MUST_SURVIVE" in tight_ctx


def test_init_merge_preserves_hand_edited_shell_commands_and_split_matchers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. Every hand-edited shell form stays byte-for-byte untouched on default re-run
    hand_edited_commands = [
        "compact-ledger hook pre-compact || true",
        "compact-ledger hook pre-compact --strict 2>>/tmp/cl.log",
        'compact-ledger hook pre-compact --store-dir "$CLAUDE_PROJECT_DIR/.compact-ledger"',
        "COMPACT_LEDGER_DIR=/data/cl compact-ledger hook pre-compact",
        "cd /repo && compact-ledger hook pre-compact",
    ]
    for raw_cmd in hand_edited_commands:
        seed = {
            "hooks": {
                "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": raw_cmd}]}]
            }
        }
        merged = merge_claude_code_settings(seed)
        assert merged["hooks"]["PreCompact"][0]["hooks"][0]["command"] == raw_cmd

    # Prefix upgrade splices ONLY plain prefix, leaving trailing shell syntax intact
    # and leaving env-prefixed / shell-wrapped commands untouched
    for raw_cmd, expected in [
        (
            "compact-ledger hook pre-compact || true",
            "uvx compact-ledger hook pre-compact || true",
        ),
        (
            "compact-ledger hook pre-compact --strict 2>>/tmp/cl.log",
            "uvx compact-ledger hook pre-compact --strict 2>>/tmp/cl.log",
        ),
        (
            'compact-ledger hook pre-compact --store-dir "$CLAUDE_PROJECT_DIR/.compact-ledger"',
            'uvx compact-ledger hook pre-compact --store-dir "$CLAUDE_PROJECT_DIR/.compact-ledger"',
        ),
        (
            "COMPACT_LEDGER_DIR=/data/cl compact-ledger hook pre-compact",
            "COMPACT_LEDGER_DIR=/data/cl compact-ledger hook pre-compact",
        ),
        (
            "cd /repo && compact-ledger hook pre-compact",
            "cd /repo && compact-ledger hook pre-compact",
        ),
    ]:
        seed = {
            "hooks": {
                "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": raw_cmd}]}]
            }
        }
        upgraded = merge_claude_code_settings(seed, command="uvx compact-ledger")
        assert upgraded["hooks"]["PreCompact"][0]["hooks"][0]["command"] == expected

    # 2. Separate compact and resume SessionStart entries are BOTH preserved
    split_seed = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "^compact$",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "compact-ledger hook session-start --max-chars 4000",
                        }
                    ],
                },
                {
                    "matcher": "(resume)",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "compact-ledger hook session-start --max-chars 6000",
                        }
                    ],
                },
            ]
        }
    }
    merged_split = merge_claude_code_settings(split_seed)
    assert merged_split["hooks"]["SessionStart"] == split_seed["hooks"]["SessionStart"]
    # Regex matcher (compact|resume) is also recognized without adding a duplicate entry
    regex_seed = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "(compact|resume)",
                    "hooks": [{"type": "command", "command": "compact-ledger hook session-start"}],
                }
            ]
        }
    }
    merged_regex = merge_claude_code_settings(regex_seed)
    assert len(merged_regex["hooks"]["SessionStart"]) == 1

    # 3. Print-only mode (without --write) works on a valid symlink without modifying anything
    real_cfg = tmp_path / "real.json"
    real_cfg.write_text('{"model": "keep"}\n', encoding="utf-8")
    link_cfg = tmp_path / "link.json"
    link_cfg.symlink_to(real_cfg)
    rc = main(["init", "--claude-code", "--settings-path", str(link_cfg)])
    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["model"] == "keep"
    assert "PreCompact" in preview["hooks"]
    assert json.loads(real_cfg.read_text(encoding="utf-8")) == {"model": "keep"}


def test_hook_broken_stderr_pipe_with_bad_flag_and_abbreviations() -> None:
    import os

    # 1. Broken stderr pipe + bad flag must exit 0 (NOT 120!)
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "compact_ledger", "hook", "pre-compact", "--bogus"],
            input=b"{}",
            stdout=subprocess.PIPE,
            stderr=write_fd,
            check=False,
        )
    finally:
        os.close(write_fd)
    assert proc.returncode == 0
    assert proc.stdout == b""

    # 2. Abbreviated flags (--store-di, --stric) are disabled via allow_abbrev=False
    abbrev_open = subprocess.run(
        [sys.executable, "-m", "compact_ledger", "hook", "pre-compact", "--stric"],
        input=b"{broken",
        capture_output=True,
        check=False,
    )
    assert abbrev_open.returncode == 0
    assert abbrev_open.stdout == b""
