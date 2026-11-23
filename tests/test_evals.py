"""Tests for the seeded 20-case compact_ledger.evals suite and `eval` CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from compact_ledger.cli import main
from compact_ledger.evals import (
    SEEDED_EVAL_CASES,
    EvalCase,
    format_eval_report_json,
    format_eval_report_text,
    run_eval_suite,
)


def test_seeded_eval_suite_has_20_unique_passing_cases() -> None:
    assert len(SEEDED_EVAL_CASES) == 20
    ids = [c.case_id for c in SEEDED_EVAL_CASES]
    assert len(set(ids)) == 20

    report = run_eval_suite()
    assert report.total == 20
    assert report.passed == 20
    assert report.failed == 0
    assert report.pass_rate == 100.0
    assert report.elapsed_ms >= 0.0

    cats = report.category_summary()
    assert set(cats) == {"constraints", "overrides", "trajectories", "thrash", "budget", "hooks"}
    assert sum(c["total"] for c in cats.values()) == 20
    assert all(c["failed"] == 0 for c in cats.values())


def test_cli_eval_text_verbose_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["eval"])
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "compact-ledger eval (20 seeded cases)" in text_out
    assert "20/20 passed (100.0%)" in text_out
    assert "[FAIL]" not in text_out

    rc = main(["eval", "--verbose"])
    assert rc == 0
    verbose_out = capsys.readouterr().out
    assert "3-step override chain" in verbose_out
    assert "20/20 passed (100.0%)" in verbose_out

    rc = main(["eval", "--format", "json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 20
    assert parsed["passed"] == 20
    assert parsed["failed"] == 0
    assert parsed["pass_rate"] == 100.0
    assert len(parsed["cases"]) == 20
    assert all(c["passed"] is True and c["error"] is None for c in parsed["cases"])


def test_eval_cli_works_from_arbitrary_working_directory(tmp_path: Path) -> None:
    outside_dir = tmp_path / "unrelated-cwd"
    outside_dir.mkdir()

    proc = subprocess.run(
        [sys.executable, "-m", "compact_ledger", "eval", "--format", "json"],
        cwd=str(outside_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["total"] == 20
    assert parsed["passed"] == 20
    assert parsed["failed"] == 0


def test_repo_evals_script_runs_cleanly() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "evals" / "run_evals.py"
    assert script.is_file()

    proc = subprocess.run(
        [sys.executable, str(script), "--format", "json"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["total"] == 20
    assert parsed["passed"] == 20


def test_eval_suite_failure_reporting() -> None:
    def _failing_case(_tmp: Path) -> None:
        raise AssertionError("synthetic failure for reporting check")

    def _passing_case(_tmp: Path) -> None:
        return None

    custom = (
        EvalCase("ok_case", "synthetic", "Passes cleanly", _passing_case),
        EvalCase("bad_case", "synthetic", "Fails intentionally", _failing_case),
    )
    report = run_eval_suite(custom)
    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.pass_rate == 50.0

    text = format_eval_report_text(report, verbose=True)
    assert "[PASS] ok_case" in text
    assert "[FAIL] bad_case" in text
    assert "synthetic failure for reporting check" in text
    assert "1/2 passed (50.0%)" in text

    js = json.loads(format_eval_report_json(report))
    assert js["failed"] == 1
    assert js["cases"][1]["passed"] is False
    assert "synthetic failure" in js["cases"][1]["error"]
