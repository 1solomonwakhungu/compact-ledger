"""Unit and adversarial tests for compact_ledger.pins."""

from __future__ import annotations

import time

import pytest

from compact_ledger.models import PinEntry, UserTurn, make_pin_id
from compact_ledger.pins import (
    extract_pins,
    is_chatter,
    is_constraint_or_directive,
    is_negative_constraint,
    is_override_turn,
    strip_fenced_code_blocks,
)


def _turns(*texts: str) -> list[UserTurn]:
    return [UserTurn(turn_index=idx + 1, text=t, seq=idx + 1) for idx, t in enumerate(texts)]


# ---------------------------------------------------------------------------
# Chatter filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t  ",
        "...",
        "👍",
        "ok",
        "Okay!",
        "thanks",
        "Thank you!",
        "continue",
        "Please continue.",
        "looks good",
        "LGTM",
        "go ahead",
        "yes",
        "yep",
        "sure",
        "sounds good",
        "nice",
        "great",
        "proceed",
        "Great, thanks! Please continue.",
        "Looks good, go ahead and ship it!",
        "Thanks, that actually looks great!",
        "Awesome, that actually worked!",
        "ok, sounds good, proceed",
    ],
)
def test_is_chatter_true_for_low_signal_messages(text: str) -> None:
    assert is_chatter(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Ok, don't touch auth.py.",
        "Thanks! Always format dates as YYYY-MM-DD.",
        "Yes, actually use July 31 instead of June 30.",
        "Continue, but never use third-party dependencies.",
        "Sounds good, keep the diff under 100 lines.",
        "Use June 30 as the cutoff date.",
        "```python\nx = 1\n```",
    ],
)
def test_is_chatter_false_for_substantive_messages(text: str) -> None:
    assert is_chatter(text) is False


# ---------------------------------------------------------------------------
# Negative constraints & directive detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Don't touch auth.py.",
        "Don\u2019t touch migrations/.",
        "Do not modify the database schema.",
        "Never use third-party dependencies.",
        "Must not break backward compatibility.",
        "Avoid mutating the input list.",
        "Avoid external packages.",
        "Zero external runtime dependencies.",
        "Leave legacy_api.py untouched.",
        "Can you make sure we don't change the public CLI flags?",
        "Stop editing src/store.py.",
        "No breaking changes to the schema.",
        "Implement this without touching config.py.",
    ],
)
def test_negative_constraint_detection(text: str) -> None:
    assert is_negative_constraint(text) is True
    assert is_constraint_or_directive(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Why don't we check the logs?",
        "I don't know why the test failed.",
        "I don't think that's the cause.",
        "I've never seen that error before.",
        "Never mind the timeout, use 30s.",
        "That didn't work.",
        "Is it actually broken?",
        "Thanks, that actually looks great.",
    ],
)
def test_negative_constraint_avoids_false_positives(text: str) -> None:
    assert is_negative_constraint(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Must keep Python 3.11 compatibility.",
        "Only use Python stdlib.",
        "Always sort output keys deterministically.",
        "Keep memory usage under 50MB.",
        "Make sure all hooks fail open.",
        "Use June 30 as the cutoff date.",
        "Please format timestamps in UTC.",
        "Could you keep the response under 1500 tokens?",
    ],
)
def test_positive_constraint_and_directive_detection(text: str) -> None:
    assert is_constraint_or_directive(text) is True
    assert is_negative_constraint(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "What does this function return?",
        "Is it actually broken?",
        "Why did test_auth fail?",
        "Can you explain how the parser works?",
        "That actually worked!",
        "The CI build failed on line 42.",
        "I see the same stack trace locally.",
        "Why did you use tabs instead of spaces?",
        "Should we switch from Redis to Valkey?",
    ],
)
def test_non_directive_questions_and_observations_rejected(text: str) -> None:
    assert is_constraint_or_directive(text) is False
    assert is_override_turn(text) is False


# ---------------------------------------------------------------------------
# Superseding corrections & multi-step override chains
# ---------------------------------------------------------------------------


def test_single_step_override_instead_of() -> None:
    turns = _turns(
        "Use June 30 as the cutoff date.",
        "ok",
        "Actually use July 31 instead of June 30.",
    )
    pins = extract_pins(turns)
    assert len(pins) == 2

    old_pin, new_pin = pins
    assert old_pin.source == "auto"
    assert old_pin.superseded_by == new_pin.pin_id
    assert new_pin.source == "override"
    assert new_pin.supersedes == old_pin.pin_id
    assert new_pin.superseded_by is None


def test_multistep_override_chain_with_inherited_topic() -> None:
    turns = _turns(
        "Use June 30 as the cutoff date.",
        "Actually use July 15 instead of June 30.",
        "Correction: switch from July 15 to July 31.",
        "Update: make the cutoff date August 15.",
    )
    pins = extract_pins(turns)
    assert len(pins) == 4

    p1, p2, p3, p4 = pins
    assert p1.superseded_by == p2.pin_id
    assert p2.supersedes == p1.pin_id
    assert p2.superseded_by == p3.pin_id
    assert p3.supersedes == p2.pin_id
    assert p3.superseded_by == p4.pin_id
    assert p4.supersedes == p3.pin_id
    assert p4.superseded_by is None
    assert [p.source for p in pins] == ["auto", "override", "override", "override"]

    active = [p for p in pins if p.superseded_by is None]
    assert [p.pin_id for p in active] == [p4.pin_id]


def test_ignore_and_forget_earlier_about_topic_isolation() -> None:
    turns = _turns(
        "Never touch src/auth.py.",
        "Always format dates as YYYY-MM-DD.",
        "Ignore what I said earlier about auth.",
        "Keep runtime dependencies at zero.",
        "Forget my previous instruction about dates, use Unix epochs instead.",
    )
    pins = extract_pins(turns)
    by_text = {p.text: p for p in pins}

    auth_pin = by_text["Never touch src/auth.py."]
    date_pin = by_text["Always format dates as YYYY-MM-DD."]
    ignore_auth = by_text["Ignore what I said earlier about auth."]
    deps_pin = by_text["Keep runtime dependencies at zero."]
    forget_dates = by_text["Forget my previous instruction about dates, use Unix epochs instead."]

    assert auth_pin.superseded_by == ignore_auth.pin_id
    assert ignore_auth.supersedes == auth_pin.pin_id
    assert ignore_auth.source == "override"

    assert date_pin.superseded_by == forget_dates.pin_id
    assert forget_dates.supersedes == date_pin.pin_id
    assert forget_dates.source == "override"

    # Unrelated pin remains completely untouched and active
    assert deps_pin.superseded_by is None
    assert deps_pin.source == "auto"


@pytest.mark.parametrize(
    ("initial", "override"),
    [
        ("Use SQLite for local storage.", "Instead of SQLite, use PostgreSQL."),
        ("Use Redis for session caching.", "Switch from Redis to Valkey."),
        ("Set the retry limit to 3.", "Change the retry limit from 3 to 5."),
        ("Use June 30 cutoff.", "Change June 30 to July 31."),
        ("Run tests with pytest.", "Replace pytest with unittest."),
        ("Keep max_chars at 4000.", "Correction: keep max_chars at 6000."),
        ("Output format must be markdown.", "Update: output format must be JSON."),
        ("Use JSON for config files.", "Use YAML rather than JSON."),
        ("Use June 30 as the deadline.", "Use July 31, not June 30."),
        ("Enable response caching by default.", "Never mind the caching, disable it."),
        ("Remove the legacy endpoint.", "On second thought, keep the legacy endpoint."),
        ("Use June 30.", "Actually, use July 31."),
        ("Use tabs for indentation.", "No, actually use 4 spaces for indentation."),
    ],
)
def test_override_phrase_variants(initial: str, override: str) -> None:
    pins = extract_pins(_turns(initial, override))
    assert len(pins) == 2, f"Expected 2 pins for ({initial!r}, {override!r}), got {pins}"
    old_pin, new_pin = pins
    assert new_pin.source == "override"
    assert new_pin.supersedes == old_pin.pin_id
    assert old_pin.superseded_by == new_pin.pin_id


def test_scratch_that_unscoped_retraction_targets_latest_transcript_pin() -> None:
    manual = PinEntry(
        pin_id=make_pin_id("manual", "Never leak API keys."),
        text="Never leak API keys.",
        source="manual",
        is_negative=True,
    )
    turns = _turns(
        "Keep output sorted alphabetically.",
        "Use camelCase for all keys.",
        "Scratch that, use snake_case.",
    )
    pins = extract_pins(turns, manual_pins=[manual])
    by_text = {p.text: p for p in pins}

    assert by_text["Never leak API keys."].superseded_by is None
    assert by_text["Keep output sorted alphabetically."].superseded_by is None
    assert by_text["Use camelCase for all keys."].superseded_by == (
        by_text["Scratch that, use snake_case."].pin_id
    )
    assert by_text["Scratch that, use snake_case."].supersedes == (
        by_text["Use camelCase for all keys."].pin_id
    )


# ---------------------------------------------------------------------------
# Adversarial false-positive guards
# ---------------------------------------------------------------------------


def test_adversarial_casual_actually_never_triggers_override_or_pin() -> None:
    turns = _turns(
        "Never touch auth.py.",
        "That actually worked!",
        "Is it actually broken?",
        "Thanks, that actually looks great.",
        "Wait, it actually passed on the first try.",
        "Actually, what does this function return?",
    )
    pins = extract_pins(turns)
    assert len(pins) == 1
    assert pins[0].text == "Never touch auth.py."
    assert pins[0].superseded_by is None
    assert pins[0].source == "auto"


def test_unrelated_topic_isolation_prevents_blind_superseding() -> None:
    turns = _turns(
        "Never touch src/auth.py.",
        "Keep response payloads under 100KB.",
        "Actually use July 31 instead of June 30 for billing.",
        "Actually, also add a docstring for the CLI.",
    )
    pins = extract_pins(turns)
    by_text = {p.text: p for p in pins}

    assert by_text["Never touch src/auth.py."].superseded_by is None
    assert by_text["Keep response payloads under 100KB."].superseded_by is None
    # Hard override with no matching prior pin is still marked override with supersedes=None
    billing = by_text["Actually use July 31 instead of June 30 for billing."]
    assert billing.source == "override"
    assert billing.supersedes is None
    # Soft "actually" with no matching prior pin remains a normal auto pin
    docstring = by_text["Actually, also add a docstring for the CLI."]
    assert docstring.source == "auto"
    assert docstring.supersedes is None


def test_negated_replacement_verbs_do_not_falsely_supersede() -> None:
    turns = _turns(
        "Use June 30 as the cutoff date.",
        "Do not change June 30 to July 31.",
        "Never switch from Python to Go.",
    )
    pins = extract_pins(turns)
    by_text = {p.text: p for p in pins}

    assert by_text["Use June 30 as the cutoff date."].superseded_by is None
    neg_pin = by_text["Do not change June 30 to July 31."]
    assert neg_pin.is_negative is True
    assert neg_pin.source == "auto"
    assert neg_pin.supersedes is None


def test_fenced_code_blocks_do_not_trigger_constraints_or_overrides() -> None:
    fenced_backticks = (
        "Here is an example from the issue tracker:\n"
        "```markdown\n"
        "Actually use July 31 instead of June 30.\n"
        "Never touch auth.py.\n"
        "```\n"
    )
    fenced_tildes = (
        "Logs from CI:\n"
        "~~~\n"
        "Correction: switch from June 30 to August 1.\n"
        "Do not modify database.py.\n"
        "~~~\n"
    )
    unclosed_fence = (
        "Pasted snippet:\n"
        "```python\n"
        "# Actually use September 1 instead of June 30\n"
        "# Don't touch anything\n"
    )
    assert strip_fenced_code_blocks(fenced_backticks).strip() == (
        "Here is an example from the issue tracker:"
    )
    assert strip_fenced_code_blocks(fenced_tildes).strip() == "Logs from CI:"
    assert strip_fenced_code_blocks(unclosed_fence).strip() == "Pasted snippet:"

    turns = _turns(
        "Use June 30 as the cutoff date.",
        fenced_backticks,
        fenced_tildes,
        unclosed_fence,
    )
    pins = extract_pins(turns)
    assert len(pins) == 1
    assert pins[0].text == "Use June 30 as the cutoff date."
    assert pins[0].superseded_by is None


def test_directive_with_fenced_code_block_still_extracted_verbatim() -> None:
    turn_text = "Never change this SQL schema:\n```sql\nCREATE TABLE t (id INT);\n```"
    pins = extract_pins(_turns(turn_text))
    assert len(pins) == 1
    assert pins[0].is_negative is True
    assert "CREATE TABLE t (id INT);" in pins[0].text


# ---------------------------------------------------------------------------
# Deduplication, manual pins, ordering, unicode & performance
# ---------------------------------------------------------------------------


def test_deduplication_and_manual_pin_persistence() -> None:
    manual_in = [
        PinEntry(
            pin_id=make_pin_id("manual", "Never touch production credentials."),
            text="Never touch production credentials.",
            source="manual",
            is_negative=True,
            tags=["security"],
        ),
        PinEntry(
            pin_id="dup-manual-2",
            text="  Never   touch   production   credentials.  ",
            source="manual",
            is_negative=True,
            tags=["ops"],
        ),
    ]
    turns = _turns(
        "Never touch production credentials.",
        "Keep runtime dependencies at zero.",
        "  Keep   runtime\tdependencies   at zero. ",
        "ok",
        "thanks!",
    )
    pins = extract_pins(turns, manual_pins=manual_in)

    # Caller's original manual PinEntry must not be mutated
    assert manual_in[0].tags == ["security"]

    assert len(pins) == 2
    assert pins[0].source == "manual"
    assert pins[0].tags == ["security", "ops"]
    assert pins[1].source == "auto"
    assert pins[1].text == "Keep runtime dependencies at zero."


def test_out_of_order_turns_and_lone_surrogates() -> None:
    turns = [
        UserTurn(turn_index=2, text="Actually use July 31 instead of June 30 \ud800", seq=2),
        UserTurn(turn_index=1, text="Use June 30 as the cutoff date \udfff", seq=1),
    ]
    pins = extract_pins(turns)
    assert len(pins) == 2
    assert pins[0].turn_index == 1
    assert pins[1].turn_index == 2
    assert pins[1].source == "override"
    assert pins[1].supersedes == pins[0].pin_id
    assert pins[0].superseded_by == pins[1].pin_id
    for pin in pins:
        pin.text.encode("utf-8")


def test_redos_and_linear_time_performance() -> None:
    adversarial_turns = [
        UserTurn(turn_index=1, text="actually " * 3000 + "worked!", seq=1),
        UserTurn(turn_index=2, text="instead of " * 2000 + "foo", seq=2),
        UserTurn(turn_index=3, text="don't " * 2000 + "know", seq=3),
        UserTurn(turn_index=4, text="change " + ("a " * 2000) + "to b", seq=4),
        UserTurn(turn_index=5, text="ok, " * 3000 + "thanks", seq=5),
        *[
            UserTurn(
                turn_index=10 + i,
                text=f"Always keep module_{i} deterministic.",
                seq=10 + i,
            )
            for i in range(500)
        ],
    ]
    t0 = time.monotonic()
    pins = extract_pins(adversarial_turns)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"extract_pins took {elapsed:.3f}s (expected < 1.0s)"
    assert len(pins) >= 500
