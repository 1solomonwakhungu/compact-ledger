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

    # 4. Inject with tight --max-chars enforces strict budget and prioritizes manual pins
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
    bounded = capsys.readouterr().out.rstrip("\n")
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
    bounded_json_raw = capsys.readouterr().out.rstrip("\n")
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
        [sys.executable, "-m", "compact_ledger.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert f"compact-ledger {__version__}" in proc.stdout
