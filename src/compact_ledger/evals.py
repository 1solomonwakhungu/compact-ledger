"""Seeded 20-case deterministic evaluation suite for compact-ledger.

Every case constructs realistic multi-turn transcripts (Claude Code, Codex, or
generic JSONL) and runs them through the real end-to-end parsing, pin/override
extraction, attempt ledger, edit-thrash, budget renderer, and Claude Code hook
pipelines inside isolated temporary directories.

Packaged inside ``compact_ledger`` so ``compact-ledger eval`` works from any
working directory (repo root, ``uvx``, or ``pip`` install).
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compact_ledger.hooks import (
    create_and_save_snapshot,
    handle_pre_compact,
    handle_session_start,
)
from compact_ledger.models import CompactionSnapshot
from compact_ledger.renderer import render_snapshot
from compact_ledger.store import SessionStore

__all__ = [
    "EvalCase",
    "EvalCaseResult",
    "EvalReport",
    "SEEDED_EVAL_CASES",
    "format_eval_report_json",
    "format_eval_report_text",
    "run_eval_suite",
]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """Definition of one deterministic evaluation case."""

    case_id: str
    category: str
    description: str
    runner: Callable[[Path], None]


@dataclass(slots=True)
class EvalCaseResult:
    """Outcome of running a single evaluation case."""

    case_id: str
    category: str
    description: str
    passed: bool
    elapsed_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "description": self.description,
            "passed": self.passed,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "error": self.error,
        }


@dataclass(slots=True)
class EvalReport:
    """Aggregate report across all evaluation cases."""

    results: list[EvalCaseResult]
    elapsed_ms: float

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100.0) if self.total else 0.0

    def category_summary(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for r in self.results:
            bucket = summary.setdefault(r.category, {"total": 0, "passed": 0, "failed": 0})
            bucket["total"] += 1
            if r.passed:
                bucket["passed"] += 1
            else:
                bucket["failed"] += 1
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 2),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "categories": self.category_summary(),
            "cases": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Transcript fixture builders (real Claude Code, Codex, and Generic JSONL)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def _cc_user(session_id: str, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _cc_bash(
    session_id: str,
    call_id: str,
    command: str,
    output: str,
    *,
    is_error: bool = False,
    ts_call: str = "2026-02-01T10:00:05Z",
    ts_res: str = "2026-02-01T10:00:06Z",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": ts_call,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": ts_res,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "is_error": is_error,
                        "content": output,
                    }
                ],
            },
        },
    ]


def _cc_edit(
    session_id: str,
    call_id: str,
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    ts_call: str = "2026-02-01T10:00:03Z",
    ts_res: str = "2026-02-01T10:00:04Z",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": ts_call,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": old_string,
                            "new_string": new_string,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": ts_res,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "is_error": False,
                        "content": f"Updated {file_path}",
                    }
                ],
            },
        },
    ]


def _pytest_fail_output(
    node_id: str,
    file_line: str,
    actual: str,
    expected: str,
    *,
    test_func: str | None = None,
) -> str:
    func_name = test_func or node_id.split("::")[-1]
    return (
        "============================= test session starts ==============================\n"
        f"{node_id} F\n\n"
        "=================================== FAILURES ===================================\n"
        f"_________________________________ {func_name} _________________________________\n\n"
        f"    def {func_name.split('[')[0]}():\n"
        f">       assert compute() == {expected}\n"
        f"E       assert {actual} == {expected}\n\n"
        f"{file_line}: AssertionError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {node_id} - assert {actual} == {expected}\n"
        "============================== 1 failed in 0.14s ===============================\n"
    )


def _run_pipeline(
    tmp: Path,
    records: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    manual_pins: list[tuple[str, list[str], bool]] | None = None,
) -> tuple[CompactionSnapshot, str, dict[str, Any]]:
    store = SessionStore(store_dir=tmp / ".compact-ledger")
    transcript = _write_jsonl(tmp / "transcript.jsonl", records)
    effective_sid = session_id or "eval-session"
    for text, tags, is_neg in manual_pins or []:
        store.add_manual_pin(effective_sid, text, tags=tags, is_negative=is_neg)
    saved, _ = create_and_save_snapshot(transcript, store, session_id=session_id)
    md = render_snapshot(saved, max_chars=6000, output_format="markdown")
    js = json.loads(render_snapshot(saved, max_chars=6000, output_format="json"))
    return saved, md, js


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# Category 1: Verbatim Constraints & Chatter Filtering (Cases 01–03)
# ---------------------------------------------------------------------------


def _case_01_negative_constraints_claude_code(tmp: Path) -> None:
    sid = "eval-01"
    records = [
        _cc_user(sid, "Never use mock DB in integration tests.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "ok", "2026-02-01T10:01:00Z"),
        _cc_user(
            sid,
            "Don't touch database migrations under migrations/.",
            "2026-02-01T10:02:00Z",
        ),
        _cc_user(sid, "sounds good, thanks!", "2026-02-01T10:03:00Z"),
        _cc_user(sid, "Must keep Python 3.11 compatibility.", "2026-02-01T10:04:00Z"),
        _cc_user(sid, "continue", "2026-02-01T10:05:00Z"),
    ]
    snap, md, js = _run_pipeline(tmp, records, session_id=sid)

    texts = [p.text for p in snap.pins]
    _require(len(snap.pins) == 3, f"Expected 3 constraints (chatter filtered), got {texts}")
    _require(
        "Never use mock DB in integration tests." in md,
        "Negative constraint missing from rendered markdown",
    )
    _require(
        "Don't touch database migrations under migrations/." in md,
        "Second negative constraint missing from rendered markdown",
    )
    _require(
        "Must keep Python 3.11 compatibility." in md,
        "Positive constraint missing from rendered markdown",
    )
    _require(md.count("[DO NOT]") == 2, f"Expected 2 [DO NOT] tags, got:\n{md}")
    _require(
        all(c not in texts for c in ("ok", "sounds good, thanks!", "continue")),
        "Chatter leaked into extracted pins",
    )
    _require(len(js["pins"]) == 3, f"Expected 3 JSON pins, got {len(js['pins'])}")


def _case_02_codex_transcript_constraints_and_edits(tmp: Path) -> None:
    sid = "codex-eval-02"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/billing/tax.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-TAX_RATE = 0.05\n"
        "+TAX_RATE = 0.08\n"
        "*** End Patch\n"
    )
    records: list[dict[str, Any]] = [
        {
            "type": "session_meta",
            "timestamp": "2026-02-01T11:00:00Z",
            "payload": {"id": sid, "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-02-01T11:00:05Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Always round currency to 2 decimal places using Decimal.",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-02-01T11:00:10Z",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "call_id": "call_patch_1",
                "arguments": json.dumps({"patch": patch}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-02-01T11:00:11Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_patch_1",
                "output": "Success. Updated 1 file.",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-02-01T11:00:15Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Never use float for money math."}],
            },
        },
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=None)
    _require(snap.session_id == sid, f"Expected session_id {sid!r}, got {snap.session_id!r}")
    _require(
        snap.metadata.get("transcript_format") == "codex",
        f"Expected codex format, got {snap.metadata.get('transcript_format')!r}",
    )
    _require("src/billing/tax.py" in snap.modified_files, "Codex apply_patch file not tracked")
    _require(
        "Always round currency to 2 decimal places using Decimal." in md,
        "First Codex constraint missing",
    )
    _require("Never use float for money math." in md, "Second Codex negative constraint missing")


def _case_03_manual_pin_priority_over_auto_and_ledger(tmp: Path) -> None:
    sid = "eval-03"
    records = [
        _cc_user(sid, "Keep API latency under 50ms.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "Always validate webhook signatures.", "2026-02-01T10:01:00Z"),
        *_cc_bash(
            sid,
            "toolu_1",
            "pytest tests/test_api.py",
            _pytest_fail_output(
                "tests/test_api.py::test_latency",
                "tests/test_api.py:18",
                "120",
                "50",
            ),
            is_error=True,
        ),
    ]
    snap, md, _ = _run_pipeline(
        tmp,
        records,
        session_id=sid,
        manual_pins=[("Never deploy without canary approval.", ["release"], True)],
    )
    _require(
        any(p.source == "manual" and "canary approval" in p.text for p in snap.pins),
        "Manual pin lost after snapshot",
    )
    _require(
        md.index("Never deploy without canary approval.")
        < md.index("Always validate webhook signatures.")
        < md.index("tests/test_api.py::test_latency"),
        f"Expected Manual -> Constraints -> Test Failures ordering, got:\n{md}",
    )
    tight = render_snapshot(snap, max_chars=140, output_format="markdown")
    _require(len(tight) <= 140, f"Tight budget exceeded: {len(tight)} > 140")
    _require("Never deploy without canary approval." in tight, "Manual pin dropped at 140 chars")


# ---------------------------------------------------------------------------
# Category 2: Superseding Corrections & False-Positive Guards (Cases 04–08)
# ---------------------------------------------------------------------------


def _case_04_superseding_date_correction(tmp: Path) -> None:
    sid = "eval-04"
    records = [
        _cc_user(sid, "Use June 30 as the billing cutoff date.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "ok looks good", "2026-02-01T10:01:00Z"),
        _cc_user(
            sid,
            "Actually use July 31 instead of June 30 for the billing cutoff.",
            "2026-02-01T10:02:00Z",
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    old_pin = next((p for p in snap.pins if "June 30" in p.text and p.source == "auto"), None)
    ovr_pin = next((p for p in snap.pins if p.source == "override"), None)
    _require(old_pin is not None and ovr_pin is not None, f"Missing override linkage: {snap.pins}")
    assert old_pin is not None and ovr_pin is not None
    _require(ovr_pin.supersedes == old_pin.pin_id, "Override does not point to old pin")
    _require(old_pin.superseded_by == ovr_pin.pin_id, "Old pin not marked superseded_by override")
    _require("### Active Overrides" in md, f"Missing Active Overrides section:\n{md}")
    _require("Actually use July 31 instead of June 30" in md, "Override text missing")
    _require("(supersedes:" in md and "June 30" in md, "Superseded context missing in override")
    _require(
        "### User Constraints" not in md,
        "Superseded rule leaked into active User Constraints",
    )


def _case_05_ignore_earlier_and_switch_from_x_to_y(tmp: Path) -> None:
    sid = "eval-05"
    records = [
        _cc_user(sid, "Use Redis for session caching.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "Keep API timeout at 30 seconds.", "2026-02-01T10:01:00Z"),
        _cc_user(
            sid,
            "Ignore what I said earlier about Redis; switch from Redis to SQLite WAL "
            "for session caching.",
            "2026-02-01T10:02:00Z",
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    redis_pin = next((p for p in snap.pins if p.text == "Use Redis for session caching."), None)
    timeout_pin = next((p for p in snap.pins if p.text == "Keep API timeout at 30 seconds."), None)
    sqlite_ovr = next((p for p in snap.pins if p.source == "override"), None)
    _require(
        redis_pin is not None and timeout_pin is not None and sqlite_ovr is not None,
        f"Unexpected pins: {snap.pins}",
    )
    assert redis_pin is not None and timeout_pin is not None and sqlite_ovr is not None
    _require(sqlite_ovr.supersedes == redis_pin.pin_id, "Override should supersede Redis pin")
    _require(timeout_pin.superseded_by is None, "Unrelated timeout pin was falsely superseded")
    _require("SQLite WAL" in md and "Keep API timeout at 30 seconds." in md, md)


def _case_06_three_step_override_chain_a_b_c(tmp: Path) -> None:
    sid = "eval-06"
    records = [
        _cc_user(sid, "Use us-east-1 for staging deployments.", "2026-02-01T10:00:00Z"),
        _cc_user(
            sid,
            "Actually switch from us-east-1 to eu-west-1 for staging deployments.",
            "2026-02-01T10:01:00Z",
        ),
        _cc_user(
            sid,
            "Correction: use ap-southeast-1 instead of eu-west-1 for staging deployments.",
            "2026-02-01T10:02:00Z",
        ),
    ]
    snap, md, js = _run_pipeline(tmp, records, session_id=sid)
    pin_a = next((p for p in snap.pins if p.text.startswith("Use us-east-1")), None)
    pin_b = next((p for p in snap.pins if "to eu-west-1" in p.text), None)
    pin_c = next((p for p in snap.pins if "ap-southeast-1" in p.text), None)
    _require(
        pin_a is not None and pin_b is not None and pin_c is not None,
        f"Expected 3-step chain A->B->C, got {snap.pins}",
    )
    assert pin_a is not None and pin_b is not None and pin_c is not None
    _require(pin_b.supersedes == pin_a.pin_id and pin_a.superseded_by == pin_b.pin_id, "A->B link")
    _require(pin_c.supersedes == pin_b.pin_id and pin_b.superseded_by == pin_c.pin_id, "B->C link")
    _require(pin_c.superseded_by is None, "C must be the sole active head")
    _require(len(js["pins"]) == 1 and "ap-southeast-1" in js["pins"][0]["text"], str(js["pins"]))
    _require("ap-southeast-1" in md, "Final override C missing from markdown")


def _case_07_false_positive_guard_casual_actually(tmp: Path) -> None:
    sid = "eval-07"
    records = [
        _cc_user(sid, "Never commit secrets to git.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "That actually looks good, thanks.", "2026-02-01T10:01:00Z"),
        _cc_user(
            sid,
            "Wait, actually can you explain how the retry loop works?",
            "2026-02-01T10:02:00Z",
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    _require(
        all(p.source != "override" for p in snap.pins),
        f"False-positive override created: {snap.pins}",
    )
    _require(len(snap.pins) == 1, f"Expected only 1 active pin, got {snap.pins}")
    _require(snap.pins[0].superseded_by is None, "Original rule was falsely superseded")
    _require("Never commit secrets to git." in md, "Original rule missing")


def _case_08_unrelated_topic_isolation(tmp: Path) -> None:
    sid = "eval-08"
    records = [
        _cc_user(sid, "Never touch the billing module.", "2026-02-01T10:00:00Z"),
        _cc_user(sid, "Use port 8080 for the metrics server.", "2026-02-01T10:01:00Z"),
        _cc_user(
            sid,
            "Actually use port 9090 instead of 8080 for the metrics server.",
            "2026-02-01T10:02:00Z",
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    billing_pin = next((p for p in snap.pins if "billing module" in p.text), None)
    port_old = next((p for p in snap.pins if "port 8080" in p.text), None)
    port_new = next((p for p in snap.pins if "port 9090" in p.text), None)
    _require(
        billing_pin is not None and port_old is not None and port_new is not None,
        f"Unexpected pins: {snap.pins}",
    )
    assert billing_pin is not None and port_old is not None and port_new is not None
    _require(billing_pin.superseded_by is None, "Unrelated billing pin was falsely superseded")
    _require(port_new.supersedes == port_old.pin_id, "Port override failed to target port 8080")
    _require("Never touch the billing module." in md and "port 9090" in md, md)


# ---------------------------------------------------------------------------
# Category 3: Test Attempt Trajectories (Cases 09–14)
# ---------------------------------------------------------------------------


def _case_09_pytest_progressing_trajectory(tmp: Path) -> None:
    sid = "eval-09"
    node = "tests/test_pricing.py::test_discount_total"
    records = [
        _cc_user(sid, "Fix the discount calculation bug.", "2026-02-01T10:00:00Z"),
        *_cc_bash(
            sid,
            "t1",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:24", "400", "100"),
            is_error=True,
        ),
        *_cc_edit(sid, "e1", "src/pricing.py", "return base * 4", "return base * 2"),
        *_cc_bash(
            sid,
            "t2",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:24", "200", "100"),
            is_error=True,
        ),
        *_cc_edit(sid, "e2", "src/pricing.py", "return base * 2", "return base * 1.5"),
        *_cc_bash(
            sid,
            "t3",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:24", "150", "100"),
            is_error=True,
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    traj = next((t for t in snap.trajectories if t.test_id == node), None)
    _require(traj is not None, f"Missing trajectory for {node}: {snap.trajectories}")
    assert traj is not None
    _require(traj.status == "PROGRESSING", f"Expected PROGRESSING, got {traj.status}")
    _require([a.actual for a in traj.attempts] == ["400", "200", "150"], str(traj.attempts))
    _require("[PROGRESSING]" in md and "400 -> 200 -> 150" in md, md)


def _case_10_pytest_oscillating_trajectory(tmp: Path) -> None:
    sid = "eval-10"
    node = "tests/test_pricing.py::test_bundle_price"
    records = [
        _cc_user(sid, "Make bundle pricing return 100.", "2026-02-01T10:00:00Z"),
        *_cc_bash(
            sid,
            "t1",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:42", "400", "100"),
            is_error=True,
        ),
        *_cc_bash(
            sid,
            "t2",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:42", "200", "100"),
            is_error=True,
        ),
        *_cc_bash(
            sid,
            "t3",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:42", "400", "100"),
            is_error=True,
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    traj = next((t for t in snap.trajectories if t.test_id == node), None)
    _require(traj is not None and traj.status == "OSCILLATING", f"Got {snap.trajectories}")
    _require("[OSCILLATING]" in md and "400 -> 200 -> 400" in md, md)


def _case_11_pytest_stalled_and_raises_parametrized(tmp: Path) -> None:
    sid = "eval-11"
    node = "tests/test_auth.py::test_token_expiry[expired-jwt]"
    out = (
        "============================= test session starts ==============================\n"
        f"{node} F\n\n"
        "=================================== FAILURES ===================================\n"
        "_______________________ test_token_expiry[expired-jwt] _________________________\n\n"
        "    def test_token_expiry(token):\n"
        ">       with pytest.raises(ValueError):\n"
        "E       Failed: DID NOT RAISE <class 'ValueError'>\n\n"
        "tests/test_auth.py:33: Failed\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {node} - Failed: DID NOT RAISE <class 'ValueError'>\n"
        "============================== 1 failed in 0.09s ===============================\n"
    )
    records = [
        _cc_user(sid, "Ensure expired JWTs raise ValueError.", "2026-02-01T10:00:00Z"),
        *_cc_bash(sid, "t1", "pytest tests/test_auth.py", out, is_error=True),
        *_cc_bash(sid, "t2", "pytest tests/test_auth.py", out, is_error=True),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    traj = next((t for t in snap.trajectories if t.test_id == node), None)
    _require(traj is not None, f"Parametrized pytest.raises test missed: {snap.trajectories}")
    assert traj is not None
    _require(traj.status == "STALLED", f"Expected STALLED, got {traj.status}")
    _require("[STALLED]" in md and "test_token_expiry[expired-jwt]" in md, md)


def _case_12_pytest_scoped_vs_unscoped_resolution(tmp: Path) -> None:
    sid = "eval-12"
    tax_node = "tests/test_billing.py::test_tax"
    inv_node = "tests/test_billing.py::test_invoice"
    records = [
        _cc_user(sid, "Fix billing tests without breaking users.", "2026-02-01T10:00:00Z"),
        *_cc_bash(
            sid,
            "t1",
            "pytest tests/test_billing.py",
            (
                "============================= test session starts ==============================\n"
                "=================================== FAILURES ===================================\n"
                "__________________________________ test_tax ____________________________________\n"
                ">       assert calc_tax() == 10\n"
                "E       assert 12 == 10\n\n"
                "tests/test_billing.py:12: AssertionError\n"
                "________________________________ test_invoice __________________________________\n"
                ">       assert calc_invoice() == 50\n"
                "E       assert 55 == 50\n\n"
                "tests/test_billing.py:25: AssertionError\n"
                "=========================== short test summary info ============================\n"
                f"FAILED {tax_node} - assert 12 == 10\n"
                f"FAILED {inv_node} - assert 55 == 50\n"
                "============================== 2 failed in 0.10s ===============================\n"
            ),
            is_error=True,
        ),
        # Unrelated file pass MUST NOT falsely resolve test_tax or test_invoice!
        *_cc_bash(
            sid,
            "t2",
            "pytest tests/test_users.py",
            "============================== 3 passed in 0.04s ===============================\n",
            is_error=False,
        ),
        # Scoped pass for test_invoice ONLY resolves test_invoice, leaving test_tax unresolved!
        *_cc_bash(
            sid,
            "t3",
            f"pytest {inv_node}",
            "============================== 1 passed in 0.03s ===============================\n",
            is_error=False,
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    by_id = {t.test_id: t for t in snap.trajectories}
    _require(tax_node in by_id and inv_node in by_id, f"Missing trajectories: {by_id}")
    _require(
        by_id[tax_node].status == "SINGLE_FAILURE",
        f"Unrelated pass falsely resolved {tax_node}: {by_id[tax_node].status}",
    )
    _require(
        by_id[inv_node].status == "RESOLVED",
        f"Scoped pass failed to resolve {inv_node}: {by_id[inv_node].status}",
    )
    _require(tax_node in md and "[SINGLE_FAILURE]" in md, md)


def _case_13_vitest_and_jest_ansi_trajectories(tmp: Path) -> None:
    sid = "eval-13"
    jest_fail_1 = (
        "\x1b[31mFAIL\x1b[39m src/auth.test.ts\n"
        "  \x1b[31m● Auth API › rejects expired token\x1b[39m\n\n"
        "    expect(received).toBe(expected)\n\n"
        "    Expected: \x1b[32m200\x1b[39m\n"
        "    Received: \x1b[31m404\x1b[39m\n\n"
        "      at Object.<anonymous> (src/auth.test.ts:19:21)\n"
    )
    jest_fail_2 = (
        "\x1b[31mFAIL\x1b[39m src/auth.test.ts\n"
        "  \x1b[31m● Auth API › rejects expired token\x1b[39m\n\n"
        "    expect(received).toBe(expected)\n\n"
        "    Expected: \x1b[32m200\x1b[39m\n"
        "    Received: \x1b[31m403\x1b[39m\n\n"
        "      at Object.<anonymous> (src/auth.test.ts:19:21)\n"
    )
    vitest_fail = (
        "\x1b[31mFAIL\x1b[39m src/cart.test.ts > Cart > calculates subtotal\n"
        "AssertionError: expected 95 to be 100\n"
        "Expected: 100\n"
        "Received: 95\n"
        " ❯ src/cart.test.ts:14:22\n"
    )
    vitest_pass = " Test Files  1 passed (1)\n      Tests  1 passed (1)\n"
    records = [
        _cc_user(sid, "Keep Jest and Vitest suites green.", "2026-02-01T10:00:00Z"),
        *_cc_bash(sid, "j1", "npx jest src/auth.test.ts", jest_fail_1, is_error=True),
        *_cc_bash(sid, "j2", "npx jest src/auth.test.ts", jest_fail_2, is_error=True),
        *_cc_bash(sid, "v1", "npx vitest run src/cart.test.ts", vitest_fail, is_error=True),
        *_cc_bash(sid, "v2", "npx vitest run src/cart.test.ts", vitest_pass, is_error=False),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    jest_traj = next((t for t in snap.trajectories if t.runner == "jest"), None)
    vitest_traj = next((t for t in snap.trajectories if t.runner == "vitest"), None)
    _require(jest_traj is not None, f"Missing jest trajectory: {snap.trajectories}")
    _require(vitest_traj is not None, f"Missing vitest trajectory: {snap.trajectories}")
    assert jest_traj is not None and vitest_traj is not None
    _require(jest_traj.status == "PROGRESSING", f"Expected jest PROGRESSING, got {jest_traj}")
    _require([a.actual for a in jest_traj.attempts] == ["404", "403"], str(jest_traj.attempts))
    _require(vitest_traj.status == "RESOLVED", f"Expected vitest RESOLVED, got {vitest_traj}")
    _require("\x1b[" not in md, "ANSI escape codes leaked into rendered markdown")


def _case_14_go_test_and_generic_fallback_trajectories(tmp: Path) -> None:
    sid = "eval-14"
    go_fail_1 = (
        "=== RUN   TestCalculateTax\n"
        "=== RUN   TestCalculateTax/ny_rate\n"
        "    tax_test.go:28: got 42, want 40\n"
        "--- FAIL: TestCalculateTax (0.00s)\n"
        "    --- FAIL: TestCalculateTax/ny_rate (0.00s)\n"
        "FAIL\tgithub.com/acme/billing/pkg/tax\t0.012s\n"
    )
    go_fail_2 = (
        "=== RUN   TestCalculateTax\n"
        "=== RUN   TestCalculateTax/ny_rate\n"
        "    tax_test.go:28: got 41, want 40\n"
        "--- FAIL: TestCalculateTax (0.00s)\n"
        "    --- FAIL: TestCalculateTax/ny_rate (0.00s)\n"
        "FAIL\tgithub.com/acme/billing/pkg/tax\t0.011s\n"
    )
    # Generic JSONL transcript format
    records: list[dict[str, Any]] = [
        {"role": "user", "session_id": sid, "content": "Always run go test and mypy cleanly."},
        {
            "role": "tool",
            "tool_name": "bash",
            "command": "go test ./pkg/tax/...",
            "exit_code": 1,
            "output": go_fail_1,
        },
        {
            "role": "tool",
            "tool_name": "bash",
            "command": "go test ./pkg/tax/...",
            "exit_code": 1,
            "output": go_fail_2,
        },
        {
            "role": "tool",
            "tool_name": "bash",
            "command": "mypy src/app.py",
            "exit_code": 1,
            "output": "src/app.py:12: error: Incompatible return value type",
        },
        {
            "role": "tool",
            "tool_name": "bash",
            "command": "mypy src/app.py",
            "exit_code": 0,
            "output": "Success: no issues found in 1 source file",
        },
        {
            "role": "tool",
            "tool_name": "bash",
            "command": "cargo clippy -- -D warnings",
            "exit_code": 101,
            "output": "error: unused variable: `rate`",
        },
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=None)
    _require(snap.metadata.get("transcript_format") == "generic", str(snap.metadata))
    go_sub = next(
        (t for t in snap.trajectories if t.runner == "go" and "ny_rate" in t.test_id),
        None,
    )
    _require(go_sub is not None and go_sub.status == "PROGRESSING", f"Go traj: {snap.trajectories}")
    assert go_sub is not None
    _require([a.actual for a in go_sub.attempts] == ["42", "41"], str(go_sub.attempts))

    mypy_traj = next(
        (t for t in snap.trajectories if t.runner == "generic" and "mypy" in t.test_id),
        None,
    )
    clippy_traj = next(
        (t for t in snap.trajectories if t.runner == "generic" and "clippy" in t.test_id),
        None,
    )
    _require(mypy_traj is not None and mypy_traj.status == "RESOLVED", f"mypy: {mypy_traj}")
    _require(
        clippy_traj is not None and clippy_traj.status == "SINGLE_FAILURE",
        f"clippy: {clippy_traj}",
    )
    _require("TestCalculateTax/ny_rate" in md and "cargo clippy" in md, md)


# ---------------------------------------------------------------------------
# Category 4: Edit-Thrash & Revert Detection (Cases 15–16)
# ---------------------------------------------------------------------------


def _case_15_edit_thrash_hunk_revert_and_stalled_loop(tmp: Path) -> None:
    sid = "eval-15"
    node = "tests/test_pricing.py::test_total"
    records = [
        _cc_user(sid, "Fix total pricing calculation.", "2026-02-01T10:00:00Z"),
        *_cc_edit(sid, "e1", "src/pricing.py", "return total * 2", "return total * 1.5"),
        *_cc_bash(
            sid,
            "t1",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:10", "150", "100"),
            is_error=True,
        ),
        # Revert back to prior hunk!
        *_cc_edit(sid, "e2", "src/pricing.py", "return total * 1.5", "return total * 2"),
        *_cc_bash(
            sid,
            "t2",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:10", "200", "100"),
            is_error=True,
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    _require(len(snap.thrash_entries) == 1, f"Expected 1 thrash entry, got {snap.thrash_entries}")
    entry = snap.thrash_entries[0]
    _require(entry.path == "src/pricing.py" and entry.reverted is True, str(entry))
    _require("[THRASH/REVERT]" in md and "src/pricing.py" in md, md)


def _case_16_clean_edit_then_pass_no_false_thrash(tmp: Path) -> None:
    sid = "eval-16"
    node = "tests/test_pricing.py::test_total"
    records = [
        _cc_user(sid, "Fix total pricing calculation.", "2026-02-01T10:00:00Z"),
        *_cc_bash(
            sid,
            "t1",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:10", "200", "100"),
            is_error=True,
        ),
        *_cc_edit(sid, "e1", "src/pricing.py", "return total * 2", "return total * 1.5"),
        *_cc_bash(
            sid,
            "t2",
            "pytest tests/test_pricing.py",
            _pytest_fail_output(node, "tests/test_pricing.py:10", "150", "100"),
            is_error=True,
        ),
        *_cc_edit(sid, "e2", "src/pricing.py", "return total * 1.5", "return total"),
        *_cc_bash(
            sid,
            "t3",
            "pytest tests/test_pricing.py",
            "============================== 1 passed in 0.05s ===============================\n",
            is_error=False,
        ),
    ]
    snap, md, _ = _run_pipeline(tmp, records, session_id=sid)
    _require(snap.thrash_entries == [], f"False-positive thrash detected: {snap.thrash_entries}")
    traj = next((t for t in snap.trajectories if t.test_id == node), None)
    _require(traj is not None and traj.status == "RESOLVED", f"Expected RESOLVED, got {traj}")
    _require("[THRASH" not in md, f"Unexpected thrash warning in markdown:\n{md}")


# ---------------------------------------------------------------------------
# Category 5: Strict Budget Enforcement (Cases 17–18)
# ---------------------------------------------------------------------------


def _case_17_tight_budget_pins_survive_400_1200_6000(tmp: Path) -> None:
    sid = "eval-17"
    records: list[dict[str, Any]] = [
        _cc_user(sid, "Use June 30 as release deadline.", "2026-02-01T10:00:00Z"),
        _cc_user(
            sid,
            "Actually use July 31 instead of June 30 as release deadline.",
            "2026-02-01T10:01:00Z",
        ),
        _cc_user(sid, "Never drop production tables.", "2026-02-01T10:02:00Z"),
    ]
    for i in range(15):
        records.extend(
            _cc_edit(sid, f"ed_{i}", f"src/module_{i:02d}.py", f"v = {i}", f"v = {i + 1}")
        )
    for i in range(5):
        node = f"tests/test_mod_{i}.py::test_case_{i}"
        records.extend(
            _cc_bash(
                sid,
                f"b_{i}",
                f"pytest tests/test_mod_{i}.py",
                _pytest_fail_output(node, f"tests/test_mod_{i}.py:10", str(500 + i), "200"),
                is_error=True,
            )
        )

    snap, _, _ = _run_pipeline(
        tmp,
        records,
        session_id=sid,
        manual_pins=[("CRITICAL_PIN: Preserve backward-compatible API.", ["api"], False)],
    )

    for budget in (400, 1200, 6000):
        md = render_snapshot(snap, max_chars=budget, output_format="markdown")
        js_raw = render_snapshot(snap, max_chars=budget, output_format="json")
        _require(len(md) <= budget, f"Markdown exceeded budget {budget}: {len(md)}")
        _require(len(js_raw) <= budget, f"JSON exceeded budget {budget}: {len(js_raw)}")
        js = json.loads(js_raw)
        _require("CRITICAL_PIN: Preserve backward-compatible API." in md, f"Pin lost at {budget}")
        _require(
            any("CRITICAL_PIN" in p["text"] for p in js.get("pins", [])),
            f"JSON pin lost at {budget}: {js}",
        )
        if budget == 400:
            _require("src/module_14.py" not in md, "Low-priority files should be truncated at 400")
            _require("July 31" in md, "Active override should survive at 400 chars")


def _case_18_long_override_does_not_starve_negative_constraints(tmp: Path) -> None:
    sid = "eval-18"
    long_detail = " ".join(f"detail_{i}" for i in range(250))
    records = [
        _cc_user(sid, "Use v1 response schema.", "2026-02-01T10:00:00Z"),
        _cc_user(
            sid,
            f"Actually use v2 response schema instead of v1 response schema: {long_detail}",
            "2026-02-01T10:01:00Z",
        ),
        _cc_user(sid, "Never drop the users table.", "2026-02-01T10:02:00Z"),
    ]
    snap, _, _ = _run_pipeline(tmp, records, session_id=sid)
    md = render_snapshot(snap, max_chars=1800, output_format="markdown")
    js_raw = render_snapshot(snap, max_chars=1800, output_format="json")
    _require(len(md) <= 1800, f"Markdown exceeded 1800: {len(md)}")
    _require(len(js_raw) <= 1800, f"JSON exceeded 1800: {len(js_raw)}")
    _require(
        "Never drop the users table." in md,
        f"Long override starved Tier 2 negative constraint in markdown:\n{md}",
    )
    js = json.loads(js_raw)
    _require(
        any("Never drop the users table." in p["text"] for p in js.get("pins", [])),
        f"Long override starved Tier 2 negative constraint in JSON: {js}",
    )


# ---------------------------------------------------------------------------
# Category 6: End-to-End Claude Code Hooks & Gating (Cases 19–20)
# ---------------------------------------------------------------------------


def _case_19_hook_pre_compact_to_session_start_roundtrip(tmp: Path) -> None:
    sid = "hook-roundtrip-19"
    workspace = tmp / "project"
    workspace.mkdir()
    store = SessionStore(base_dir=workspace)
    store.add_manual_pin(sid, "Never weaken TLS verification.", tags=["security"], is_negative=True)

    node = "tests/test_tls.py::test_cert_chain"
    transcript = _write_jsonl(
        tmp / "claude_session.jsonl",
        [
            _cc_user(sid, "Use TLS 1.2 minimum.", "2026-02-01T10:00:00Z"),
            _cc_user(
                sid,
                "Actually use TLS 1.3 instead of TLS 1.2 minimum.",
                "2026-02-01T10:01:00Z",
            ),
            *_cc_bash(
                sid,
                "b1",
                "pytest tests/test_tls.py",
                _pytest_fail_output(node, "tests/test_tls.py:15", "400", "200"),
                is_error=True,
            ),
            *_cc_bash(
                sid,
                "b2",
                "pytest tests/test_tls.py",
                _pytest_fail_output(node, "tests/test_tls.py:15", "301", "200"),
                is_error=True,
            ),
        ],
    )

    pre_payload = json.dumps(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "cwd": str(workspace),
            "trigger": "auto",
        }
    )
    pre_res = handle_pre_compact(pre_payload, env={})
    _require(pre_res.exit_code == 0 and pre_res.stdout == "", f"PreCompact failed: {pre_res}")

    for source in ("compact", "RESUME"):
        start_payload = json.dumps(
            {
                "session_id": sid,
                "source": source,
                "cwd": str(workspace),
            }
        )
        start_res = handle_session_start(start_payload, max_chars=2500, env={})
        _require(start_res.exit_code == 0, f"SessionStart({source}) failed: {start_res}")
        parsed_out = json.loads(start_res.stdout)
        hook_out = parsed_out["hookSpecificOutput"]
        _require(hook_out["hookEventName"] == "SessionStart", str(parsed_out))
        ctx = hook_out["additionalContext"]
        _require(len(ctx) <= 2500, f"additionalContext exceeded max_chars: {len(ctx)}")
        _require("Never weaken TLS verification." in ctx, f"Manual pin missing:\n{ctx}")
        _require("TLS 1.3" in ctx and "[OVERRIDE]" in ctx, f"Override missing:\n{ctx}")
        _require("[PROGRESSING]" in ctx and "400 -> 301" in ctx, f"Trajectory missing:\n{ctx}")


def _case_20_hook_session_start_gating_and_fail_open(tmp: Path) -> None:
    sid = "hook-gating-20"
    store_dir = tmp / "store"
    store = SessionStore(store_dir=store_dir)
    store.add_manual_pin(sid, "Keep hook latency under 50ms.")

    # Non-compact/non-resume sources must produce empty stdout and exit 0
    for source in ("startup", "clear", "unknown", ""):
        res = handle_session_start(
            json.dumps({"session_id": sid, "source": source}),
            store_dir=store_dir,
            env={},
        )
        _require(
            res.exit_code == 0 and res.stdout == "",
            f"Expected silent no-op for source={source!r}, got {res}",
        )

    # --always overrides source gating
    res_always = handle_session_start(
        json.dumps({"session_id": sid, "source": "startup"}),
        store_dir=store_dir,
        always=True,
        env={},
    )
    _require(res_always.exit_code == 0 and res_always.stdout != "", str(res_always))
    ctx = json.loads(res_always.stdout)["hookSpecificOutput"]["additionalContext"]
    _require("Keep hook latency under 50ms." in ctx, ctx)

    # Fail-open vs --strict on malformed stdin and missing files
    bad_inputs = ["", "{not-json", "[]", json.dumps({"session_id": sid})]
    for bad in bad_inputs:
        r_open = handle_pre_compact(bad, store_dir=store_dir, strict=False, env={})
        _require(r_open.exit_code == 0 and r_open.stdout == "", f"PreCompact not fail-open: {bad}")
        r_strict = handle_pre_compact(bad, store_dir=store_dir, strict=True, env={})
        _require(r_strict.exit_code != 0, f"PreCompact strict should fail on: {bad}")

    r_ss_open = handle_session_start("{broken", store_dir=store_dir, strict=False, env={})
    _require(r_ss_open.exit_code == 0 and r_ss_open.stdout == "", str(r_ss_open))
    r_ss_strict = handle_session_start("{broken", store_dir=store_dir, strict=True, env={})
    _require(r_ss_strict.exit_code != 0, str(r_ss_strict))


# ---------------------------------------------------------------------------
# Registry of all 20 seeded evaluation cases
# ---------------------------------------------------------------------------

SEEDED_EVAL_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        "01_negative_constraints_claude_code",
        "constraints",
        "Positive & negative constraints survive verbatim while chatter is filtered (Claude Code)",
        _case_01_negative_constraints_claude_code,
    ),
    EvalCase(
        "02_codex_transcript_constraints_and_edits",
        "constraints",
        "Codex JSONL transcript constraints and apply_patch file edits survive end-to-end",
        _case_02_codex_transcript_constraints_and_edits,
    ),
    EvalCase(
        "03_manual_pin_priority_over_auto_and_ledger",
        "constraints",
        "Manual pins outrank auto constraints and test trajectories and survive tight caps",
        _case_03_manual_pin_priority_over_auto_and_ledger,
    ),
    EvalCase(
        "04_superseding_date_correction",
        "overrides",
        "Superseding date correction links to superseded pin and replaces it in active output",
        _case_04_superseding_date_correction,
    ),
    EvalCase(
        "05_ignore_earlier_and_switch_from_x_to_y",
        "overrides",
        "'Ignore earlier / switch from X to Y' supersedes target without touching peers",
        _case_05_ignore_earlier_and_switch_from_x_to_y,
    ),
    EvalCase(
        "06_three_step_override_chain_a_b_c",
        "overrides",
        "3-step override chain A -> B -> C links sequentially and renders only C as active",
        _case_06_three_step_override_chain_a_b_c,
    ),
    EvalCase(
        "07_false_positive_guard_casual_actually",
        "overrides",
        "Casual 'actually' chatter and questions do NOT create false-positive overrides",
        _case_07_false_positive_guard_casual_actually,
    ),
    EvalCase(
        "08_unrelated_topic_isolation",
        "overrides",
        "Topic-scoped override supersedes only its matching rule and leaves unrelated rules intact",
        _case_08_unrelated_topic_isolation,
    ),
    EvalCase(
        "09_pytest_progressing_trajectory",
        "trajectories",
        "pytest trajectory classifies distinct moving values (400 -> 200 -> 150) PROGRESSING",
        _case_09_pytest_progressing_trajectory,
    ),
    EvalCase(
        "10_pytest_oscillating_trajectory",
        "trajectories",
        "pytest trajectory classifies reverted value cycle (400 -> 200 -> 400) OSCILLATING",
        _case_10_pytest_oscillating_trajectory,
    ),
    EvalCase(
        "11_pytest_stalled_and_raises_parametrized",
        "trajectories",
        "pytest parametrized pytest.raises failure repeating identically classifies STALLED",
        _case_11_pytest_stalled_and_raises_parametrized,
    ),
    EvalCase(
        "12_pytest_scoped_vs_unscoped_resolution",
        "trajectories",
        "Unrelated pytest file pass does NOT falsely resolve failure; scoped pass resolves target",
        _case_12_pytest_scoped_vs_unscoped_resolution,
    ),
    EvalCase(
        "13_vitest_and_jest_ansi_trajectories",
        "trajectories",
        "ANSI-colored Jest & Vitest outputs strip escapes and track Expected/Received trajectories",
        _case_13_vitest_and_jest_ansi_trajectories,
    ),
    EvalCase(
        "14_go_test_and_generic_fallback_trajectories",
        "trajectories",
        "go test subtest got/want progression and generic command failure/resolution fallback",
        _case_14_go_test_and_generic_fallback_trajectories,
    ),
    EvalCase(
        "15_edit_thrash_hunk_revert_and_stalled_loop",
        "thrash",
        "Reverting a prior hunk while failures persist triggers [THRASH/REVERT] warning",
        _case_15_edit_thrash_hunk_revert_and_stalled_loop,
    ),
    EvalCase(
        "16_clean_edit_then_pass_no_false_thrash",
        "thrash",
        "Clean iterative edits ending in a passing test run produce zero false thrash warnings",
        _case_16_clean_edit_then_pass_no_false_thrash,
    ),
    EvalCase(
        "17_tight_budget_pins_survive_400_1200_6000",
        "budget",
        "Strict <= max_chars compliance at 400/1200/6000 with pins prioritized over attempts/files",
        _case_17_tight_budget_pins_survive_400_1200_6000,
    ),
    EvalCase(
        "18_long_override_does_not_starve_negative_constraints",
        "budget",
        "Oversized override is capped cleanly so Tier 2 negative constraints still survive",
        _case_18_long_override_does_not_starve_negative_constraints,
    ),
    EvalCase(
        "19_hook_pre_compact_to_session_start_roundtrip",
        "hooks",
        "End-to-end Claude Code PreCompact -> SessionStart JSON round-trip (compact & resume)",
        _case_19_hook_pre_compact_to_session_start_roundtrip,
    ),
    EvalCase(
        "20_hook_session_start_gating_and_fail_open",
        "hooks",
        "SessionStart source gating (startup/clear/--always) and fail-open vs --strict safety",
        _case_20_hook_session_start_gating_and_fail_open,
    ),
)


def run_eval_suite(cases: tuple[EvalCase, ...] = SEEDED_EVAL_CASES) -> EvalReport:
    """Execute all seeded evaluation cases in isolated temp directories."""
    suite_start = time.perf_counter()
    results: list[EvalCaseResult] = []
    with tempfile.TemporaryDirectory(prefix="compact-ledger-eval-") as root:
        root_path = Path(root)
        for case in cases:
            case_dir = root_path / case.case_id
            case_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            try:
                case.runner(case_dir)
                elapsed = (time.perf_counter() - t0) * 1000.0
                results.append(
                    EvalCaseResult(
                        case_id=case.case_id,
                        category=case.category,
                        description=case.description,
                        passed=True,
                        elapsed_ms=elapsed,
                    )
                )
            except Exception as exc:
                elapsed = (time.perf_counter() - t0) * 1000.0
                results.append(
                    EvalCaseResult(
                        case_id=case.case_id,
                        category=case.category,
                        description=case.description,
                        passed=False,
                        elapsed_ms=elapsed,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    total_ms = (time.perf_counter() - suite_start) * 1000.0
    return EvalReport(results=results, elapsed_ms=total_ms)


def format_eval_report_text(report: EvalReport, *, verbose: bool = False) -> str:
    """Format an ``EvalReport`` as a concise human-readable terminal report."""
    lines: list[str] = [f"compact-ledger eval ({report.total} seeded cases)"]
    lines.append("-" * 72)
    for res in report.results:
        status = "PASS" if res.passed else "FAIL"
        lines.append(f"[{status}] {res.case_id:<52} ({res.category:<12}) {res.elapsed_ms:5.2f} ms")
        if verbose:
            lines.append(f"       {res.description}")
        if not res.passed and res.error:
            lines.append(f"       Error: {res.error}")

    lines.append("-" * 72)
    lines.append("Category breakdown:")
    for cat, counts in report.category_summary().items():
        lines.append(f"  - {cat:<14} {counts['passed']}/{counts['total']} passed")
    lines.append("-" * 72)
    lines.append(
        f"Total: {report.passed}/{report.total} passed "
        f"({report.pass_rate:.1f}%) in {report.elapsed_ms:.1f} ms"
    )
    return "\n".join(lines)


def format_eval_report_json(report: EvalReport) -> str:
    """Format an ``EvalReport`` as indented JSON."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
