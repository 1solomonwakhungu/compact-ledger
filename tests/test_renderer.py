"""Unit and adversarial tests for compact_ledger.renderer."""

from __future__ import annotations

import json
import time

import pytest

from compact_ledger.models import (
    CompactionSnapshot,
    EditThrashEntry,
    PinEntry,
    TestAttempt,
    TestTrajectory,
    UserTurn,
)
from compact_ledger.pins import extract_pins
from compact_ledger.renderer import DEFAULT_MAX_CHARS, render_snapshot


def _rich_snapshot() -> CompactionSnapshot:
    old_pin = PinEntry(
        pin_id="pin-old",
        text="Use June 30 as the cutoff date.",
        source="auto",
        turn_index=1,
        superseded_by="pin-ovr",
    )
    override_pin = PinEntry(
        pin_id="pin-ovr",
        text="Actually use July 31 instead of June 30.",
        source="override",
        turn_index=5,
        supersedes="pin-old",
    )
    manual_pin = PinEntry(
        pin_id="pin-manual",
        text="Never touch production credentials.",
        source="manual",
        is_negative=True,
        tags=["security"],
    )
    auto_old = PinEntry(
        pin_id="pin-auto-1",
        text="Keep runtime dependencies at zero.",
        source="auto",
        turn_index=2,
    )
    auto_new_neg = PinEntry(
        pin_id="pin-auto-2",
        text="Don't modify migrations/.",
        source="auto",
        turn_index=6,
        is_negative=True,
    )

    trajectories = [
        TestTrajectory(
            test_id="tests/test_single.py::test_one",
            runner="pytest",
            status="SINGLE_FAILURE",
            attempts=[
                TestAttempt(
                    test_id="tests/test_single.py::test_one",
                    runner="pytest",
                    expected="1",
                    actual="2",
                    assertion_loc="test_single.py:10",
                    turn_index=3,
                )
            ],
        ),
        TestTrajectory(
            test_id="tests/test_resolved.py::test_fixed",
            runner="pytest",
            status="RESOLVED",
            attempts=[
                TestAttempt(
                    test_id="tests/test_resolved.py::test_fixed",
                    runner="pytest",
                    outcome="passed",
                    turn_index=7,
                )
            ],
            summary="Passed on turn 7",
        ),
        TestTrajectory(
            test_id="tests/test_osc.py::test_flaky_math",
            runner="pytest",
            status="OSCILLATING",
            attempts=[
                TestAttempt(
                    test_id="tests/test_osc.py::test_flaky_math",
                    runner="pytest",
                    expected="100",
                    actual="400",
                    turn_index=2,
                ),
                TestAttempt(
                    test_id="tests/test_osc.py::test_flaky_math",
                    runner="pytest",
                    expected="100",
                    actual="200",
                    turn_index=4,
                ),
                TestAttempt(
                    test_id="tests/test_osc.py::test_flaky_math",
                    runner="pytest",
                    expected="100",
                    actual="400",
                    assertion_loc="calc.py:42",
                    turn_index=6,
                ),
            ],
        ),
        TestTrajectory(
            test_id="tests/test_stall.py::test_stuck",
            runner="pytest",
            status="STALLED",
            attempts=[
                TestAttempt(
                    test_id="tests/test_stall.py::test_stuck",
                    runner="pytest",
                    expected="200",
                    actual="500",
                    turn_index=5,
                )
            ],
            summary="Same 500 error across 3 edits",
        ),
        TestTrajectory(
            test_id="tests/test_prog.py::test_improving",
            runner="vitest",
            status="PROGRESSING",
            attempts=[
                TestAttempt(
                    test_id="tests/test_prog.py::test_improving",
                    runner="vitest",
                    expected="10",
                    actual="12",
                    turn_index=6,
                )
            ],
            summary="100 -> 40 -> 12 (expected 10)",
        ),
    ]

    thrash = [
        EditThrashEntry(
            path="src/parser.py",
            edit_count=4,
            failure_count=3,
            reverted=False,
            reason="3 failures across 4 edits",
        ),
        EditThrashEntry(
            path="src/calc.py",
            edit_count=5,
            failure_count=4,
            reverted=True,
            reason="Reverted hunk between identical failures",
        ),
    ]

    return CompactionSnapshot(
        session_id="sess-render-1",
        pins=[old_pin, auto_old, override_pin, manual_pin, auto_new_neg],
        trajectories=trajectories,
        thrash_entries=thrash,
        modified_files=["src/calc.py", "src/parser.py", "src/calc.py", "tests/test_osc.py"],
    )


def test_markdown_full_render_priority_order_and_formatting() -> None:
    snap = _rich_snapshot()
    md = render_snapshot(snap, max_chars=DEFAULT_MAX_CHARS, output_format="markdown")

    assert len(md) <= DEFAULT_MAX_CHARS
    assert "## Compact Ledger (Session: sess-render-1)" in md

    # Tier 1: Manual pins & active overrides
    assert "[MANUAL] [DO NOT] [tags: security] Never touch production credentials." in md
    assert (
        "[OVERRIDE] Actually use July 31 instead of June 30. "
        "(supersedes: Use June 30 as the cutoff date.)" in md
    )

    # Superseded pin must NOT appear as an active constraint bullet
    assert "- [CONSTRAINT] [turn 1] Use June 30 as the cutoff date." not in md

    # Tier 2: Active constraints newest first, negative tagged [DO NOT]
    assert "[DO NOT] [turn 6] Don't modify migrations/." in md
    assert "[CONSTRAINT] [turn 2] Keep runtime dependencies at zero." in md
    assert md.index("Don't modify migrations/.") < md.index("Keep runtime dependencies at zero.")

    # Tier 3: Trajectories sorted OSCILLATING > STALLED > PROGRESSING > SINGLE_FAILURE
    idx_osc = md.index("[OSCILLATING] tests/test_osc.py::test_flaky_math")
    idx_stall = md.index("[STALLED] tests/test_stall.py::test_stuck")
    idx_prog = md.index("[PROGRESSING] tests/test_prog.py::test_improving")
    idx_single = md.index("[SINGLE_FAILURE] tests/test_single.py::test_one")
    assert idx_osc < idx_stall < idx_prog < idx_single
    assert "actual: 400 -> 200 -> 400" in md

    # Thrash warnings: reverted entry prioritized before non-reverted
    idx_revert = md.index("[THRASH/REVERT] src/calc.py")
    idx_thrash = md.index("[THRASH] src/parser.py")
    assert idx_revert < idx_thrash

    # Tier 4: Modified files deduplicated, appearing after Tier 3 and before Resolved
    assert md.index("### Modified Files") > idx_thrash
    assert md.count("- src/calc.py") == 1
    assert md.index("### Resolved Tests") > md.index("### Modified Files")
    assert "[RESOLVED] tests/test_resolved.py::test_fixed" in md

    # Strict cross-tier ordering
    assert md.index("Never touch production credentials.") < md.index("Actually use July 31")
    assert md.index("Actually use July 31") < md.index("Don't modify migrations/.")
    assert md.index("Keep runtime dependencies at zero.") < idx_osc


def test_strict_priority_pins_win_over_ledger_and_files_under_tight_budget() -> None:
    snap = _rich_snapshot()
    tight_md = render_snapshot(snap, max_chars=260, output_format="markdown")
    assert len(tight_md) <= 260
    assert "Never touch production credentials." in tight_md
    assert "Actually use July 31" in tight_md
    # Lower tiers MUST NOT appear when higher tiers hit the budget
    assert "### Active Test Failures" not in tight_md
    assert "### Modified Files" not in tight_md
    assert "### Resolved Tests" not in tight_md

    tight_json_raw = render_snapshot(snap, max_chars=600, output_format="json")
    assert len(tight_json_raw) <= 600
    tight_json = json.loads(tight_json_raw)
    sources = [p["source"] for p in tight_json["pins"]]
    assert sources[0] == "manual"
    assert "override" in sources
    assert all(p["pin_id"] != "pin-old" for p in tight_json["pins"])


def test_raw_attempts_fallback_when_trajectories_empty() -> None:
    snap = CompactionSnapshot(
        session_id="sess-raw-attempts",
        attempts=[
            TestAttempt(
                test_id="tests/test_a.py::test_pass_later",
                runner="pytest",
                outcome="failed",
                expected="1",
                actual="0",
                turn_index=1,
            ),
            TestAttempt(
                test_id="tests/test_a.py::test_pass_later",
                runner="pytest",
                outcome="passed",
                turn_index=2,
            ),
            TestAttempt(
                test_id="tests/test_b.py::test_still_broken",
                runner="pytest",
                outcome="failed",
                assertion_loc="b.py:19",
                expected="200",
                actual="500",
                turn_index=3,
            ),
            TestAttempt(
                test_id="tests/test_c.py::test_err",
                runner="go test",
                outcome="error",
                message="nil pointer dereference",
                turn_index=4,
            ),
        ],
    )
    md = render_snapshot(snap, output_format="markdown")
    assert "test_pass_later" not in md
    assert "[ERROR] tests/test_c.py::test_err (go test): nil pointer dereference" in md
    assert "[FAIL] tests/test_b.py::test_still_broken (pytest) @ b.py:19" in md
    # Newest first
    assert md.index("test_err") < md.index("test_still_broken")

    data = json.loads(render_snapshot(snap, output_format="json"))
    assert [a["test_id"] for a in data["attempts"]] == [
        "tests/test_c.py::test_err",
        "tests/test_b.py::test_still_broken",
    ]


@pytest.mark.parametrize("budget", [0, 1, 2, 10, 20, 50, 80, 150, 250, 600, 1500, 6000])
def test_exact_budget_compliance_markdown_and_json(budget: int) -> None:
    snap = _rich_snapshot()

    md = render_snapshot(snap, max_chars=budget, output_format="markdown")
    assert len(md) <= budget, f"markdown exceeded budget={budget}: len={len(md)}"
    md.encode("utf-8")

    js = render_snapshot(snap, max_chars=budget, output_format="json")
    assert len(js) <= budget, f"json exceeded budget={budget}: len={len(js)}"
    js.encode("utf-8")
    if budget >= 2:
        parsed = json.loads(js)
        assert isinstance(parsed, dict)
    else:
        assert js == ""


def test_long_manual_pin_truncated_instead_of_dropped_under_tight_budget() -> None:
    snap = CompactionSnapshot(
        session_id="s-long",
        pins=[
            PinEntry(
                pin_id="p-man",
                text="CRITICAL_RULE " + ("x" * 700),
                source="manual",
            ),
            PinEntry(pin_id="p-auto", text="Secondary auto rule", source="auto", turn_index=1),
        ],
        modified_files=["src/a.py"],
    )
    md = render_snapshot(snap, max_chars=160, output_format="markdown")
    assert len(md) <= 160
    assert "CRITICAL_RULE" in md
    assert "Secondary auto rule" not in md
    assert "src/a.py" not in md

    js_raw = render_snapshot(snap, max_chars=300, output_format="json")
    assert len(js_raw) <= 300
    js_obj = json.loads(js_raw)
    assert len(js_obj["pins"]) == 1
    assert js_obj["pins"][0]["source"] == "manual"
    assert js_obj["pins"][0]["text"].startswith("CRITICAL_RULE")


def test_markdown_heading_spoofing_surrogates_cycles_and_determinism() -> None:
    # Cyclic pins A <-> B + lone surrogates + markdown heading spoofing
    snap = CompactionSnapshot(
        session_id="sess-\ud800\n### Fake",
        pins=[
            PinEntry(
                pin_id="p-a",
                text="Rule A\n\n### Manual Pins\n- Fake rule \udfff",
                source="auto",
                turn_index=1,
                supersedes="p-b",
                superseded_by="p-b",
            ),
            PinEntry(
                pin_id="p-b",
                text="Rule B \ud800",
                source="override",
                turn_index=2,
                supersedes="p-a",
                superseded_by="p-a",
            ),
        ],
        modified_files=["src/\ud800bad.py"],
    )
    md1 = render_snapshot(snap, output_format="markdown")
    md2 = render_snapshot(snap, output_format="markdown")
    assert md1 == md2
    md1.encode("utf-8")
    # Spoofed heading must be flattened
    heading_lines = [line for line in md1.splitlines() if line.startswith("### ")]
    assert "Fake rule" not in heading_lines
    assert "### Manual Pins" not in heading_lines
    # Newer pin in malformed cycle stays active
    assert "Rule B" in md1

    js1 = render_snapshot(snap, output_format="json")
    js2 = render_snapshot(snap, output_format="json")
    assert js1 == js2
    js1.encode("utf-8")
    parsed = json.loads(js1)
    assert any(p["pin_id"] == "p-b" for p in parsed["pins"])


def test_validation_errors() -> None:
    snap = CompactionSnapshot(session_id="s1")
    with pytest.raises(ValueError, match="non-negative"):
        render_snapshot(snap, max_chars=-1)
    with pytest.raises(ValueError, match="non-negative"):
        render_snapshot(snap, max_chars=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported output_format"):
        render_snapshot(snap, output_format="xml")  # type: ignore[arg-type]


def test_large_input_linear_performance() -> None:
    snap = CompactionSnapshot(
        session_id="s-scale",
        pins=[
            PinEntry(
                pin_id=f"p-{i}",
                text=f"Always enforce invariant number {i} across all handlers.",
                source="auto",
                turn_index=i,
            )
            for i in range(4000)
        ],
        trajectories=[
            TestTrajectory(
                test_id=f"tests/test_{i}.py::test_case",
                runner="pytest",
                status="STALLED",
                summary=f"Failed {i}",
            )
            for i in range(500)
        ],
        modified_files=[f"src/mod_{i}.py" for i in range(500)],
    )

    t0 = time.monotonic()
    md = render_snapshot(snap, max_chars=6000, output_format="markdown")
    js = render_snapshot(snap, max_chars=6000, output_format="json")
    elapsed = time.monotonic() - t0

    assert len(md) <= 6000
    assert len(js) <= 6000
    json.loads(js)
    assert elapsed < 0.5, f"Rendering 4000 pins took {elapsed:.3f}s (expected < 0.5s)"


def test_end_to_end_extract_pins_and_render_snapshot() -> None:
    turns = [
        UserTurn(turn_index=1, text="Use June 30 as the cutoff date.", seq=1),
        UserTurn(turn_index=2, text="Never touch auth.py.", seq=2),
        UserTurn(turn_index=3, text="ok, sounds good!", seq=3),
        UserTurn(turn_index=4, text="Actually use July 15 instead of June 30.", seq=4),
        UserTurn(turn_index=5, text="That actually worked!", seq=5),
        UserTurn(turn_index=6, text="Correction: switch from July 15 to July 31.", seq=6),
    ]
    manual = [
        PinEntry(
            pin_id="pin-man-1",
            text="Keep runtime dependencies at zero.",
            source="manual",
            tags=["arch"],
        )
    ]
    pins = extract_pins(turns, manual_pins=manual)
    snap = CompactionSnapshot(session_id="e2e-session", pins=pins, modified_files=["src/dates.py"])
    md = render_snapshot(snap, max_chars=2000, output_format="markdown")

    assert "[MANUAL] [tags: arch] Keep runtime dependencies at zero." in md
    assert "[OVERRIDE] Correction: switch from July 15 to July 31." in md
    assert "(supersedes: Actually use July 15 instead of June 30.)" in md
    assert "[DO NOT] [turn 2] Never touch auth.py." in md
    assert "ok, sounds good!" not in md
    assert "That actually worked!" not in md
    assert "- [CONSTRAINT] [turn 1] Use June 30 as the cutoff date." not in md


# ---------------------------------------------------------------------------
# Regression tests for Fable 5.2 adversarial review findings (B5, NB4-NB8)
# ---------------------------------------------------------------------------


def test_b5_json_pin_text_never_shrinks_as_budget_grows() -> None:
    rule_text = "Never modify the production billing schema without an approved migration."
    snap = CompactionSnapshot(
        session_id="550e8400-e29b-41d4-a716-446655440000",
        transcript_path="/Users/developer/workspaces/super-long-project-name/sessions/2026-11-22/claude-transcript-session-550e8400.jsonl",
        pins=[
            PinEntry(
                pin_id="pin-manual-1",
                text=rule_text,
                source="manual",
                is_negative=True,
            )
        ],
    )

    prev_len = 0
    for budget in (240, 280, 300, 340, 380, 420, 460, 500, 520, 600, 800):
        raw = render_snapshot(snap, max_chars=budget, output_format="json")
        assert len(raw) <= budget
        parsed = json.loads(raw)
        cur_len = len(parsed["pins"][0]["text"]) if parsed.get("pins") else 0
        assert cur_len >= prev_len, (
            f"Pin text shrank when budget grew to {budget}: {cur_len} < {prev_len} ({raw!r})"
        )
        prev_len = cur_len
        if budget >= 280:
            assert parsed["pins"][0]["text"] == rule_text


def test_long_override_does_not_starve_tier2_do_not_rules() -> None:
    snap = CompactionSnapshot(
        session_id="sess-starve",
        pins=[
            PinEntry(
                pin_id="pin-huge-ovr",
                text="Update: " + ("log line details " * 500),
                source="override",
                turn_index=10,
            ),
            *[
                PinEntry(
                    pin_id=f"pin-neg-{i}",
                    text=f"Never touch module_{i}.py.",
                    source="auto",
                    turn_index=i + 1,
                    is_negative=True,
                )
                for i in range(5)
            ],
        ],
    )
    md = render_snapshot(snap, max_chars=6000, output_format="markdown")
    assert len(md) <= 6000
    for i in range(5):
        assert f"Never touch module_{i}.py." in md

    js_raw = render_snapshot(snap, max_chars=6000, output_format="json")
    assert len(js_raw) <= 6000
    js = json.loads(js_raw)
    texts = [p["text"] for p in js["pins"]]
    for i in range(5):
        assert f"Never touch module_{i}.py." in texts


def test_malformed_links_never_vanish_pins() -> None:
    # 1. Cycle expressed ONLY via `supersedes` (superseded_by=None on both)
    cyc_snap = CompactionSnapshot(
        session_id="s-cyc",
        pins=[
            PinEntry(pin_id="a", text="Rule A", source="auto", turn_index=1, supersedes="b"),
            PinEntry(pin_id="b", text="Rule B", source="auto", turn_index=2, supersedes="a"),
        ],
    )
    cyc_md = render_snapshot(cyc_snap, output_format="markdown")
    assert "Rule B" in cyc_md

    # 2. Manual pin whose superseded_by points to a missing pin_id
    dangling_snap = CompactionSnapshot(
        session_id="s-dangling",
        pins=[
            PinEntry(
                pin_id="m1",
                text="Never delete prod tables.",
                source="manual",
                superseded_by="does-not-exist",
                is_negative=True,
            )
        ],
    )
    dangling_md = render_snapshot(dangling_snap, output_format="markdown")
    assert "Never delete prod tables." in dangling_md

    # 3. Duplicate pin_id where only one copy has superseded_by
    dup_snap = CompactionSnapshot(
        session_id="s-dup",
        pins=[
            PinEntry(
                pin_id="dup-1",
                text="Stale copy",
                source="auto",
                turn_index=1,
                superseded_by="real-ovr",
            ),
            PinEntry(
                pin_id="dup-1",
                text="Active sibling copy",
                source="auto",
                turn_index=2,
            ),
            PinEntry(pin_id="real-ovr", text="Override copy", source="override", turn_index=3),
        ],
    )
    dup_md = render_snapshot(dup_snap, output_format="markdown")
    assert "Active sibling copy" in dup_md
    assert "Override copy" in dup_md


def test_json_nan_safety_and_over_200_items_metadata_consistency() -> None:
    snap = CompactionSnapshot(
        session_id="s-meta",
        pins=[
            PinEntry(pin_id=f"p-{i}", text=f"Rule {i}", source="auto", turn_index=i)
            for i in range(205)
        ],
        metadata={"ratio": float("nan"), "inf": float("inf"), "ok": "yes"},
    )
    raw = render_snapshot(snap, max_chars=100_000, output_format="json")
    assert "NaN" not in raw
    assert "Infinity" not in raw
    parsed = json.loads(raw)
    assert parsed["metadata"] == {"ratio": None, "inf": None, "ok": "yes"}
    assert len(parsed["pins"]) == 205


def test_raw_failing_attempts_not_dropped_when_other_test_has_trajectory() -> None:
    snap = CompactionSnapshot(
        session_id="s-mixed-tests",
        trajectories=[
            TestTrajectory(
                test_id="tests/test_a.py::test_ok",
                runner="pytest",
                status="RESOLVED",
                summary="Passed",
            )
        ],
        attempts=[
            TestAttempt(
                test_id="tests/test_a.py::test_ok",
                runner="pytest",
                outcome="passed",
                turn_index=2,
            ),
            TestAttempt(
                test_id="tests/test_b.py::test_broken",
                runner="pytest",
                outcome="failed",
                expected="42",
                actual="0",
                turn_index=3,
            ),
        ],
    )
    md = render_snapshot(snap, output_format="markdown")
    assert "tests/test_b.py::test_broken" in md

    parsed = json.loads(render_snapshot(snap, output_format="json"))
    assert [a["test_id"] for a in parsed["attempts"]] == ["tests/test_b.py::test_broken"]


def test_markdown_tiny_budgets_use_compact_header_instead_of_clipping_full_header() -> None:
    snap = CompactionSnapshot(
        session_id="very-long-session-identifier-1234567890",
        pins=[PinEntry(pin_id="p1", text="Keep zero deps.", source="manual")],
    )
    for budget in (18, 25, 40, 50):
        md = render_snapshot(snap, max_chars=budget, output_format="markdown")
        assert len(md) <= budget
        assert md.startswith("## Compact Ledger")
        assert "(Ses" not in md
