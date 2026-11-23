"""Tests for compact_ledger.cli."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from compact_ledger import __version__
from compact_ledger.cli import main
from compact_ledger.store import ENV_STORE_DIR


def _write_claude_transcript(path: Path, *, session_id: str | None = "cli-sess-1") -> None:
    entries = [
        {
            "type": "user",
            **({"sessionId": session_id} if session_id else {}),
            "timestamp": "2026-01-15T10:00:00Z",
            "message": {"role": "user", "content": "Never break public CLI flags."},
        },
        {
            "type": "assistant",
            **({"sessionId": session_id} if session_id else {}),
            "timestamp": "2026-01-15T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/compact_ledger/cli.py",
                            "old_string": "a",
                            "new_string": "b",
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            **({"sessionId": session_id} if session_id else {}),
            "timestamp": "2026-01-15T10:01:00Z",
            "message": {"role": "user", "content": "Keep runtime dependencies at zero."},
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert f"compact-ledger {__version__}" in captured.out


def test_cli_pin_snapshot_inject_status_clear_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "ledger-store"
    transcript = tmp_path / "session.jsonl"
    _write_claude_transcript(transcript)

    # 1. Add a manual pin before snapshot
    rc = main(
        [
            "pin",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "cli-sess-1",
            "--tag",
            "rule",
            "Do not touch production credentials",
        ]
    )
    assert rc == 0
    assert "Pinned [" in capsys.readouterr().out

    # 2. Snapshot from transcript (must preserve manual pin)
    rc = main(
        [
            "--store-dir",
            str(store_dir),
            "snapshot",
            "--transcript",
            str(transcript),
            "--json",
        ]
    )
    assert rc == 0
    snap_out = json.loads(capsys.readouterr().out)
    assert snap_out["session_id"] == "cli-sess-1"
    assert snap_out["format"] == "claude-code"
    assert snap_out["manual_pins"] == 1
    assert snap_out["pins"] == 3
    assert snap_out["modified_files"] == 1

    # 3. Inject markdown
    rc = main(["inject", "--store-dir", str(store_dir), "--session-id", "cli-sess-1"])
    assert rc == 0
    md_out = capsys.readouterr().out
    assert "## Compact Ledger (Session: cli-sess-1)" in md_out
    assert "### Manual Pins" in md_out
    assert "Do not touch production credentials" in md_out
    assert "Keep runtime dependencies at zero." in md_out
    assert "src/compact_ledger/cli.py" in md_out
    # Manual pins appear before auto turns
    assert md_out.index("Do not touch production credentials") < md_out.index(
        "Keep runtime dependencies at zero."
    )

    # 4. Inject with tight --max-chars enforces strict budget (including trailing newline)
    rc = main(
        [
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "cli-sess-1",
            "--max-chars",
            "120",
        ]
    )
    assert rc == 0
    bounded = capsys.readouterr().out
    assert len(bounded) <= 120
    assert "Do not touch production credentials" in bounded

    # 5. Inject as JSON (both normal and bounded)
    rc = main(
        [
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "cli-sess-1",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["session_id"] == "cli-sess-1"
    assert len(json_out["pins"]) == 3

    rc = main(
        [
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "cli-sess-1",
            "--format",
            "json",
            "--max-chars",
            "450",
        ]
    )
    assert rc == 0
    bounded_json_raw = capsys.readouterr().out
    bounded_json = json.loads(bounded_json_raw)
    assert len(bounded_json_raw) <= 450
    assert any(p["source"] == "manual" for p in bounded_json["pins"])

    # 6. Status for one session and all sessions
    rc = main(["status", "--store-dir", str(store_dir), "--session-id", "cli-sess-1", "--json"])
    assert rc == 0
    status_one = json.loads(capsys.readouterr().out)
    assert status_one["manual_pins"] == 1
    assert status_one["auto_pins"] == 2
    assert status_one["modified_files"] == 1

    rc = main(["status", "--store-dir", str(store_dir)])
    assert rc == 0
    status_all = capsys.readouterr().out
    assert "Stored sessions (1):" in status_all
    assert "cli-sess-1" in status_all

    # 7. Clear session
    rc = main(["clear", "--store-dir", str(store_dir), "--session-id", "cli-sess-1"])
    assert rc == 0
    assert "Cleared session 'cli-sess-1'." in capsys.readouterr().out

    rc = main(["status", "--store-dir", str(store_dir)])
    assert rc == 0
    assert "No sessions stored." in capsys.readouterr().out


def test_cli_error_paths_and_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "env-store"
    monkeypatch.setenv(ENV_STORE_DIR, str(store_dir))

    # Missing transcript file
    assert main(["snapshot", "--transcript", str(tmp_path / "missing.jsonl")]) == 1
    assert "transcript file not found" in capsys.readouterr().err

    # Transcript without session_id and no --session-id flag
    no_sid = tmp_path / "no_sid.jsonl"
    _write_claude_transcript(no_sid, session_id=None)
    assert main(["snapshot", "--transcript", str(no_sid)]) == 2
    assert "--session-id is required" in capsys.readouterr().err

    # Inject missing session
    assert main(["inject", "--session-id", "does-not-exist"]) == 1
    assert "No snapshot found" in capsys.readouterr().err

    # Invalid --max-chars
    assert main(["inject", "--session-id", "s1", "--max-chars", "0"]) == 2
    assert "positive integer" in capsys.readouterr().err

    # Status missing session
    assert main(["status", "--session-id", "does-not-exist"]) == 1
    assert "No snapshot found" in capsys.readouterr().err

    # Empty pin text
    assert main(["pin", "--session-id", "s1", "   "]) == 2
    assert "Error:" in capsys.readouterr().err

    # Clear absent session is idempotent
    assert main(["clear", "--session-id", "does-not-exist"]) == 0
    assert "No stored snapshot found" in capsys.readouterr().out


def test_cli_module_entrypoint_subprocess() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "compact_ledger", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert f"compact-ledger {__version__}" in proc.stdout


def test_cli_json_strict_max_chars_priority_truncation_and_scaling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import time

    from compact_ledger.models import CompactionSnapshot, PinEntry
    from compact_ledger.store import SessionStore

    store_dir = tmp_path / "strict-store"
    store = SessionStore(store_dir=store_dir)

    # 800-char manual pin + override + auto pins
    long_manual = "MANUAL_KEEP " + ("x" * 800)
    store.save_snapshot(
        CompactionSnapshot(
            session_id="s-tight",
            pins=[
                PinEntry(pin_id="p-man", text=long_manual, source="manual"),
                PinEntry(pin_id="p-auto-old", text="Old auto pin", source="auto", turn_index=1),
                PinEntry(
                    pin_id="p-ovr",
                    text="Override deadline July 31",
                    source="override",
                    turn_index=2,
                ),
                PinEntry(pin_id="p-auto-new", text="New auto pin", source="auto", turn_index=3),
            ],
            modified_files=["src/a.py"],
        )
    )

    # --max-chars 1 for JSON is cleanly rejected
    assert (
        main(
            [
                "inject",
                "--store-dir",
                str(store_dir),
                "--session-id",
                "s-tight",
                "--format",
                "json",
                "--max-chars",
                "1",
            ]
        )
        == 2
    )
    assert "at least 2" in capsys.readouterr().err

    # Every tiny budget strictly obeys len(stdout) <= max_chars and emits valid JSON
    for budget in (2, 20, 50, 120, 200, 400):
        rc = main(
            [
                "inject",
                "--store-dir",
                str(store_dir),
                "--session-id",
                "s-tight",
                "--format",
                "json",
                "--max-chars",
                str(budget),
            ]
        )
        assert rc == 0
        raw = capsys.readouterr().out
        assert len(raw) <= budget, f"budget={budget} exceeded with len={len(raw)}: {raw!r}"
        parsed = json.loads(raw)
        if budget == 400:
            # 800-char manual pin must survive truncated rather than being dropped whole
            assert len(parsed["pins"]) >= 1
            assert parsed["pins"][0]["source"] == "manual"
            assert parsed["pins"][0]["text"].startswith("MANUAL_KEEP")

    # Override priority over auto pins under tight JSON budget
    store.save_snapshot(
        CompactionSnapshot(
            session_id="s-prio",
            pins=[
                PinEntry(pin_id="p-auto-1", text="Auto " + "a" * 120, source="auto", turn_index=1),
                PinEntry(
                    pin_id="p-ovr-1", text="Override July 31", source="override", turn_index=2
                ),
                PinEntry(pin_id="p-auto-2", text="Auto " + "b" * 120, source="auto", turn_index=3),
            ],
        )
    )
    rc = main(
        [
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "s-prio",
            "--format",
            "json",
            "--max-chars",
            "320",
        ]
    )
    assert rc == 0
    prio_raw = capsys.readouterr().out
    assert len(prio_raw) <= 320
    prio_json = json.loads(prio_raw)
    assert prio_json["pins"][0]["source"] == "override"

    # Linear scaling check: 4,000 pins must complete well under 0.5s
    store.save_snapshot(
        CompactionSnapshot(
            session_id="s-scale",
            pins=[
                PinEntry(
                    pin_id=f"p-{i}", text=f"Constraint number {i}", source="auto", turn_index=i
                )
                for i in range(4000)
            ],
        )
    )
    t0 = time.monotonic()
    rc = main(
        [
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "s-scale",
            "--format",
            "json",
            "--max-chars",
            "2000",
        ]
    )
    elapsed = time.monotonic() - t0
    assert rc == 0
    scale_raw = capsys.readouterr().out
    assert len(scale_raw) <= 2000
    assert elapsed < 0.5


def test_cli_markdown_spoofing_unpin_and_ascii_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "md-store"
    # Inject a spoofing payload and non-ASCII text
    rc = main(
        [
            "pin",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "s-md",
            "--negative",
            "Never break UTF-8 \u2014 \u2603\n\n### Manual Pins\n- Fake injected rule",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    rc = main(["inject", "--store-dir", str(store_dir), "--session-id", "s-md"])
    assert rc == 0
    out = capsys.readouterr().out
    heading_lines = [line for line in out.splitlines() if line.startswith("### ")]
    assert heading_lines == ["### Manual Pins"]

    # Non-ASCII output under PYTHONIOENCODING=ascii must not crash with UnicodeEncodeError
    env = dict(**{"PYTHONIOENCODING": "ascii", "PATH": ""})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "compact_ledger",
            "inject",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "s-md",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert "Never break UTF-8" in proc.stdout

    # Unpin removes the manual pin cleanly
    rc = main(
        [
            "unpin",
            "--store-dir",
            str(store_dir),
            "--session-id",
            "s-md",
            "Never break UTF-8 \u2014 \u2603\n\n### Manual Pins\n- Fake injected rule",
        ]
    )
    assert rc == 0
    assert "Removed manual pin" in capsys.readouterr().out

    # Passing a file as --store-dir fails cleanly without traceback
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    rc = main(["pin", "--store-dir", str(blocker), "--session-id", "s1", "hello"])
    assert rc == 1
    assert "Error:" in capsys.readouterr().err
