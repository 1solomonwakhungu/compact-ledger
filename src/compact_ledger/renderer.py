"""Strict token/character budget renderer for CompactionSnapshot.

Enforces deterministic priority ordering and exact ``len(result) <= max_chars``
budget compliance across both Markdown and JSON output formats:

1. Manual pins & active superseding corrections (showing what was superseded)
2. Remaining active verbatim user constraints (newest first; negative rules tagged)
3. Unresolved failing test trajectories / attempts & edit-thrash warnings
4. Modified files summary
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any, Literal

from compact_ledger.models import (
    CompactionSnapshot,
    EditThrashEntry,
    PinEntry,
    TestAttempt,
    TestTrajectory,
    sanitize_unicode,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "render_snapshot",
]

DEFAULT_MAX_CHARS = 6000
_MAX_SUPERSEDED_SNIPPET_CHARS = 120
# Minimum floor before reserving space for other active pins under overflow (preserves
# strict monotonicity as max_chars grows while preventing one 8KB log from starving Tier 2).
_STARVATION_FLOOR_CHARS = 1200
_MAX_METADATA_DEPTH = 32

_TRAJECTORY_PRIORITY: dict[str, int] = {
    "OSCILLATING": 0,
    "STALLED": 1,
    "PROGRESSING": 2,
    "SINGLE_FAILURE": 3,
    "RESOLVED": 4,
}


# ---------------------------------------------------------------------------
# Shared text, JSON-safety & ordering helpers
# ---------------------------------------------------------------------------


def _flatten_single_line(text: str) -> str:
    """Flatten embedded line breaks so user text cannot spoof Markdown headings."""
    return " ".join(sanitize_unicode(text).split())


def _truncate_text(text: str, max_len: int) -> str:
    """Truncate ``text`` cleanly to at most ``max_len`` characters."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _sanitize_json_value(value: Any, *, _depth: int = 0) -> Any:
    """Sanitize arbitrary values into strict RFC-8259 JSON-safe primitives.

    Guards against lone UTF-16 surrogates, non-finite floats (NaN/Inf -> None),
    non-JSON containers (set/frozenset/bytes/arbitrary objects), and deep nesting
    (never raises RecursionError or TypeError).
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return sanitize_unicode(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes | bytearray | memoryview):
        return sanitize_unicode(bytes(value).decode("utf-8", errors="replace"))
    if _depth >= _MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, dict):
        return {
            sanitize_unicode(str(k)): _sanitize_json_value(v, _depth=_depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_json_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        items = [_sanitize_json_value(item, _depth=_depth + 1) for item in value]
        return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    return sanitize_unicode(str(value))


def _compute_monotonic_pin_cap(
    pin_text_lengths: Sequence[int],
    max_chars: int,
    *,
    per_pin_overhead: int,
) -> int | None:
    """Return a monotonically non-decreasing per-pin cap ONLY when pins overflow ``max_chars``.

    - If all pins fit verbatim in ``max_chars`` -> returns ``None`` (zero verbatim loss).
    - If pins overflow AND ``max_chars > _STARVATION_FLOOR_CHARS`` AND there are multiple
      active pins -> reserves just enough space for the other short pins (capped at
      ``max_chars - _STARVATION_FLOOR_CHARS``) so ``cap(B) = B - min(other, max(0, B - 1200))``
      is mathematically guaranteed to be monotonically non-decreasing for all ``B >= 0``.
    """
    if len(pin_text_lengths) <= 1 or max_chars <= _STARVATION_FLOOR_CHARS:
        return None
    total_estimated = sum(length + per_pin_overhead for length in pin_text_lengths)
    if total_estimated + 120 <= max_chars:
        return None

    # Reserve space for the non-longest pins (capped per pin at 240 chars so only short
    # rules are guaranteed room without starving a primary long rule).
    max_len = max(pin_text_lengths)
    skipped_max = False
    other_needed = 0
    for length in pin_text_lengths:
        if not skipped_max and length == max_len:
            skipped_max = True
            continue
        other_needed += min(length, 240) + per_pin_overhead

    reserve = min(other_needed, max(0, max_chars - _STARVATION_FLOOR_CHARS))
    cap = max_chars - reserve - per_pin_overhead - 80
    if cap < _STARVATION_FLOOR_CHARS:
        cap = _STARVATION_FLOOR_CHARS
    return cap if cap < max_len else None


def _partition_pins(
    pins: Sequence[PinEntry],
) -> tuple[list[PinEntry], list[PinEntry], list[PinEntry], list[PinEntry], dict[str, PinEntry]]:
    """Partition pins into active manual, active overrides, active auto, and superseded."""
    pins_by_id: dict[str, PinEntry] = {}
    id_to_indices: dict[str, list[int]] = {}
    for idx, p in enumerate(pins):
        id_to_indices.setdefault(p.pin_id, []).append(idx)
        if p.pin_id not in pins_by_id:
            pins_by_id[p.pin_id] = p

    superseded_by_map: dict[str, str] = {}
    superseded_indices: set[int] = set()

    for idx, p in enumerate(pins):
        if (
            p.superseded_by is not None
            and p.superseded_by != p.pin_id
            and p.superseded_by in pins_by_id
        ):
            superseded_indices.add(idx)
            superseded_by_map.setdefault(p.pin_id, p.superseded_by)
        if p.supersedes is not None and p.supersedes != p.pin_id and p.supersedes in pins_by_id:
            for target_idx in id_to_indices[p.supersedes]:
                superseded_indices.add(target_idx)
            superseded_by_map.setdefault(p.supersedes, p.pin_id)

    visited_global: set[str] = set()
    for start_id in superseded_by_map:
        if start_id in visited_global:
            continue
        path: list[str] = []
        path_index: dict[str, int] = {}
        cur: str | None = start_id
        while cur is not None and cur in pins_by_id and cur not in visited_global:
            if cur in path_index:
                cycle_ids = path[path_index[cur] :]
                newest_id = max(
                    cycle_ids,
                    key=lambda pid: (
                        pins_by_id[pid].turn_index
                        if pins_by_id[pid].turn_index is not None
                        else -1,
                        pid,
                    ),
                )
                for keep_idx in id_to_indices[newest_id]:
                    superseded_indices.discard(keep_idx)
                break
            path_index[cur] = len(path)
            path.append(cur)
            nxt = superseded_by_map.get(cur)
            cur = nxt if nxt != cur else None
        visited_global.update(path)

    manual: list[PinEntry] = []
    overrides_indexed: list[tuple[int, int, PinEntry]] = []
    auto_indexed: list[tuple[int, int, PinEntry]] = []
    superseded: list[PinEntry] = []

    for idx, p in enumerate(pins):
        if idx in superseded_indices:
            superseded.append(p)
            continue
        if p.source == "manual":
            manual.append(p)
        elif p.source == "override":
            sort_turn = p.turn_index if p.turn_index is not None else idx
            overrides_indexed.append((sort_turn, idx, p))
        else:
            sort_turn = p.turn_index if p.turn_index is not None else idx
            auto_indexed.append((sort_turn, idx, p))

    overrides_indexed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    auto_indexed.sort(key=lambda item: (item[0], item[1]), reverse=True)

    return (
        manual,
        [p for _, _, p in overrides_indexed],
        [p for _, _, p in auto_indexed],
        superseded,
        pins_by_id,
    )


def _order_trajectories(
    trajectories: Sequence[TestTrajectory],
) -> tuple[list[TestTrajectory], list[TestTrajectory]]:
    """Split trajectories into prioritized unresolved and resolved lists."""
    unresolved: list[TestTrajectory] = []
    resolved: list[TestTrajectory] = []

    for traj in trajectories:
        if traj.status == "RESOLVED":
            resolved.append(traj)
        else:
            unresolved.append(traj)

    def _latest_turn(t: TestTrajectory) -> int:
        return max((a.turn_index for a in t.attempts), default=-1)

    unresolved.sort(
        key=lambda t: (
            _TRAJECTORY_PRIORITY.get(t.status, 99),
            -_latest_turn(t),
            t.test_id,
        )
    )
    resolved.sort(key=lambda t: (-_latest_turn(t), t.test_id))
    return unresolved, resolved


def _order_thrash_entries(entries: Sequence[EditThrashEntry]) -> list[EditThrashEntry]:
    return sorted(
        entries,
        key=lambda e: (not e.reverted, -e.failure_count, -e.edit_count, e.path),
    )


def _unresolved_raw_attempts(
    attempts: Sequence[TestAttempt],
    covered_test_ids: frozenset[str] = frozenset(),
) -> list[TestAttempt]:
    """Return unresolved failing/error raw attempts not already covered by trajectories."""
    latest_by_test: dict[str, tuple[int, int, TestAttempt]] = {}
    for idx, att in enumerate(attempts):
        if att.test_id in covered_test_ids:
            continue
        key = (att.turn_index, idx)
        prev = latest_by_test.get(att.test_id)
        if prev is None or key >= (prev[0], prev[1]):
            latest_by_test[att.test_id] = (att.turn_index, idx, att)

    unresolved_ids = {tid for tid, (_, _, att) in latest_by_test.items() if att.outcome != "passed"}
    failing = [
        (att.turn_index, idx, att)
        for idx, att in enumerate(attempts)
        if att.test_id in unresolved_ids and att.outcome != "passed"
    ]
    failing.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [att for _, _, att in failing]


def _dedupe_files(files: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in files:
        flat = _flatten_single_line(path)
        if flat and flat not in seen:
            seen.add(flat)
            out.append(flat)
    return out


# ---------------------------------------------------------------------------
# Markdown item formatters
# ---------------------------------------------------------------------------


def _cap_pin_body(body: str, per_pin_cap: int | None) -> str:
    if per_pin_cap is None or len(body) <= per_pin_cap:
        return body
    return _truncate_text(body, per_pin_cap)


def _sanitize_tag(tag: str) -> str:
    """Prevent bracket characters inside a tag from forging fake [OVERRIDE]/[MANUAL] prefixes."""
    return _flatten_single_line(tag).replace("[", "(").replace("]", ")")


def _format_manual_pin(pin: PinEntry, per_pin_cap: int | None = None) -> str:
    prefixes: list[str] = ["[MANUAL]"]
    if pin.is_negative:
        prefixes.append("[DO NOT]")
    if pin.tags:
        clean_tags = ", ".join(_sanitize_tag(t) for t in pin.tags if t.strip())
        if clean_tags:
            prefixes.append(f"[tags: {clean_tags}]")
    body = _cap_pin_body(_flatten_single_line(pin.text), per_pin_cap)
    return f"{' '.join(prefixes)} {body}"


def _format_override_pin(
    pin: PinEntry,
    pins_by_id: dict[str, PinEntry],
    per_pin_cap: int | None = None,
) -> str:
    prefix = "[OVERRIDE] [DO NOT]" if pin.is_negative else "[OVERRIDE]"
    body = _cap_pin_body(_flatten_single_line(pin.text), per_pin_cap)
    if pin.supersedes and pin.supersedes in pins_by_id:
        old_text = _flatten_single_line(pins_by_id[pin.supersedes].text)
        if old_text:
            snippet = _truncate_text(old_text, _MAX_SUPERSEDED_SNIPPET_CHARS)
            return f"{prefix} {body} (supersedes: {snippet})"
    return f"{prefix} {body}"


def _format_constraint_pin(pin: PinEntry, per_pin_cap: int | None = None) -> str:
    tag = "[DO NOT]" if pin.is_negative else "[CONSTRAINT]"
    body = _cap_pin_body(_flatten_single_line(pin.text), per_pin_cap)
    if pin.turn_index is not None:
        return f"{tag} [turn {pin.turn_index}] {body}"
    return f"{tag} {body}"


def _format_trajectory(traj: TestTrajectory) -> str:
    test_id = _flatten_single_line(traj.test_id)
    runner = _flatten_single_line(traj.runner)
    prefix = f"[{traj.status}] {test_id} ({runner})"
    summary = _flatten_single_line(traj.summary)
    if summary:
        return f"{prefix}: {summary}"

    if not traj.attempts:
        return prefix

    actuals = [
        _flatten_single_line(a.actual) for a in traj.attempts if a.actual is not None and a.actual
    ]
    latest = traj.attempts[-1]
    parts: list[str] = [f"{len(traj.attempts)} attempt{'s' if len(traj.attempts) != 1 else ''}"]
    if actuals:
        trail = " -> ".join(actuals[-5:])
        parts.append(f"actual: {trail}")
    if latest.expected:
        parts.append(f"expected: {_flatten_single_line(latest.expected)}")
    if latest.assertion_loc:
        parts.append(f"@ {_flatten_single_line(latest.assertion_loc)}")
    elif latest.message and not actuals:
        parts.append(_truncate_text(_flatten_single_line(latest.message), 120))
    return f"{prefix}: {', '.join(parts)}"


def _format_thrash_entry(entry: EditThrashEntry) -> str:
    tag = "[THRASH/REVERT]" if entry.reverted else "[THRASH]"
    path = _flatten_single_line(entry.path)
    counts = f"{entry.edit_count} edits, {entry.failure_count} failures"
    reason = _flatten_single_line(entry.reason)
    if reason:
        return f"{tag} {path} ({counts}): {reason}"
    return f"{tag} {path} ({counts})"


def _format_raw_attempt(att: TestAttempt) -> str:
    label = "[ERROR]" if att.outcome == "error" else "[FAIL]"
    test_id = _flatten_single_line(att.test_id)
    runner = _flatten_single_line(att.runner)
    loc = f" @ {_flatten_single_line(att.assertion_loc)}" if att.assertion_loc else ""
    details: list[str] = []
    if att.expected is not None and att.expected != "":
        details.append(f"expected={_flatten_single_line(att.expected)}")
    if att.actual is not None and att.actual != "":
        details.append(f"actual={_flatten_single_line(att.actual)}")
    if not details and att.message:
        details.append(_truncate_text(_flatten_single_line(att.message), 120))
    detail_str = f": {', '.join(details)}" if details else ""
    return f"{label} {test_id} ({runner}){loc}{detail_str}"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_markdown(snapshot: CompactionSnapshot, max_chars: int) -> str:
    if max_chars <= 0:
        return ""

    manual, overrides, auto, _superseded, pins_by_id = _partition_pins(snapshot.pins)
    unresolved_traj, resolved_traj = _order_trajectories(snapshot.trajectories)
    thrash = _order_thrash_entries(snapshot.thrash_entries)
    covered_test_ids = frozenset(t.test_id for t in snapshot.trajectories)
    raw_attempts = _unresolved_raw_attempts(snapshot.attempts, covered_test_ids)
    modified_files = _dedupe_files(snapshot.modified_files)

    # Monotonic starvation cap: ONLY applied when pins would otherwise overflow max_chars.
    all_active_pins = [*manual, *overrides, *auto]
    per_pin_cap = _compute_monotonic_pin_cap(
        [len(_flatten_single_line(p.text)) for p in all_active_pins],
        max_chars,
        per_pin_overhead=45,
    )

    tier1_items: list[str] = [
        *(_format_manual_pin(p, per_pin_cap) for p in manual),
        *(_format_override_pin(p, pins_by_id, per_pin_cap) for p in overrides),
    ]
    if manual and overrides:
        tier1_title = "Manual Pins & Active Overrides"
    elif manual:
        tier1_title = "Manual Pins"
    else:
        tier1_title = "Active Overrides"

    tier2_items = [_format_constraint_pin(p, per_pin_cap) for p in auto]

    tier3_items: list[str] = [
        *(_format_trajectory(t) for t in unresolved_traj),
        *(_format_thrash_entry(e) for e in thrash),
        *(_format_raw_attempt(a) for a in raw_attempts),
    ]

    tier4_items = modified_files
    resolved_items = [_format_trajectory(t) for t in resolved_traj]

    sections: list[tuple[str, list[str], bool]] = [
        (tier1_title, tier1_items, True),
        ("User Constraints", tier2_items, True),
        ("Active Test Failures & Thrash Warnings", tier3_items, True),
        ("Modified Files", tier4_items, False),
        ("Resolved Tests", resolved_items, False),
    ]

    session_label = _flatten_single_line(snapshot.session_id)
    full_header = f"## Compact Ledger (Session: {session_label})"
    compact_header = "## Compact Ledger"

    # Monotonic header selection: switch to full_header ONLY once the full first item
    # also fits alongside full_header, so increasing max_chars never shrinks the first pin.
    first_non_empty = next(((t, items) for t, items, _ in sections if items), None)
    header = full_header
    if len(full_header) > max_chars and len(compact_header) <= max_chars:
        header = compact_header
    elif first_non_empty is not None:
        first_title, first_items = first_non_empty
        first_item_len = len(_flatten_single_line(first_items[0]))
        full_with_first = len(full_header) + len(f"\n\n### {first_title}\n- ") + first_item_len
        if full_with_first > max_chars and len(compact_header) <= max_chars:
            header = compact_header

    if len(header) > max_chars:
        return _truncate_text(header, max_chars)

    parts: list[str] = [header]
    used = len(header)

    for title, items, allow_truncate in sections:
        if not items:
            continue
        section_header = f"\n\n### {title}"
        added_header = False
        overflowed = False

        for item in items:
            flat_item = _flatten_single_line(item)
            entry = f"\n- {flat_item}"
            needed = len(entry) + (len(section_header) if not added_header else 0)
            if used + needed <= max_chars:
                if not added_header:
                    parts.append(section_header)
                    used += len(section_header)
                    added_header = True
                parts.append(entry)
                used += len(entry)
            else:
                overflowed = True
                if allow_truncate:
                    overhead = (len(section_header) if not added_header else 0) + 3  # "\n- "
                    remaining = max_chars - used - overhead
                    if remaining >= 12:
                        if not added_header:
                            parts.append(section_header)
                            used += len(section_header)
                            added_header = True
                        truncated = _truncate_text(flat_item, remaining)
                        entry = f"\n- {truncated}"
                        parts.append(entry)
                        used += len(entry)
                break

        if overflowed:
            break

    result = sanitize_unicode("".join(parts))
    if len(result) > max_chars:
        return _truncate_text(result, max_chars)
    return result


# ---------------------------------------------------------------------------
# JSON renderer
# ---------------------------------------------------------------------------


def _compact_json(obj: Any) -> str:
    return sanitize_unicode(
        json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    )


def _sanitize_pin_dict(pin: PinEntry, per_pin_cap: int | None = None) -> dict[str, Any]:
    d = pin.to_dict()
    d["pin_id"] = sanitize_unicode(d["pin_id"])
    text = sanitize_unicode(d["text"])
    if per_pin_cap is not None and len(text) > per_pin_cap:
        text = _truncate_text(text, per_pin_cap)
    d["text"] = text
    if d["timestamp"] is not None:
        d["timestamp"] = sanitize_unicode(d["timestamp"])
    d["tags"] = [sanitize_unicode(t) for t in d["tags"]]
    return d


def _truncate_pin_for_json_budget(
    pin_dict: dict[str, Any], max_item_chars: int
) -> tuple[str, int] | None:
    """Truncate a pin dict's ``text`` so its compact JSON fits in ``max_item_chars``.

    Returns ``(serialized_json, preserved_text_len)`` or ``None`` if it cannot fit.
    """
    base = dict(pin_dict)
    base["text"] = ""
    empty_len = len(_compact_json(base))
    if empty_len + 4 > max_item_chars:
        return None

    original_text = sanitize_unicode(str(pin_dict.get("text", "")))
    lo, hi = 1, len(original_text)
    best: tuple[str, int] | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = dict(pin_dict)
        trunc_text = _truncate_text(original_text, mid)
        candidate["text"] = trunc_text
        serialized = _compact_json(candidate)
        if len(serialized) <= max_item_chars:
            best = (serialized, len(trunc_text))
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _render_json(snapshot: CompactionSnapshot, max_chars: int) -> str:
    if max_chars < 2:
        return ""

    manual, overrides, auto, superseded, _pins_by_id = _partition_pins(snapshot.pins)
    unresolved_traj, resolved_traj = _order_trajectories(snapshot.trajectories)
    thrash = _order_thrash_entries(snapshot.thrash_entries)
    covered_test_ids = frozenset(t.test_id for t in snapshot.trajectories)
    raw_attempts = _unresolved_raw_attempts(snapshot.attempts, covered_test_ids)
    modified_files = _dedupe_files(snapshot.modified_files)

    # Monotonic starvation cap: ONLY applied when pins would otherwise overflow max_chars.
    all_active_pins = [*manual, *overrides, *auto]
    per_pin_cap = _compute_monotonic_pin_cap(
        [len(sanitize_unicode(p.text)) for p in all_active_pins],
        max_chars,
        per_pin_overhead=180,
    )

    manual_dicts = [_sanitize_pin_dict(p, per_pin_cap) for p in manual]
    override_dicts = [_sanitize_pin_dict(p, per_pin_cap) for p in overrides]
    auto_dicts = [_sanitize_pin_dict(p, per_pin_cap) for p in auto]
    superseded_dicts = [_sanitize_pin_dict(p, _MAX_SUPERSEDED_SNIPPET_CHARS) for p in superseded]

    ordered_pins: list[tuple[bool, dict[str, Any]]] = [
        *((True, p) for p in manual_dicts),
        *((True, p) for p in override_dicts),
        *((True, p) for p in auto_dicts),
    ]

    traj_dicts = [_sanitize_json_value(t.to_dict()) for t in unresolved_traj]
    resolved_traj_dicts = [_sanitize_json_value(t.to_dict()) for t in resolved_traj]
    thrash_dicts = [_sanitize_json_value(e.to_dict()) for e in thrash]
    attempt_dicts = [_sanitize_json_value(a.to_dict()) for a in raw_attempts]

    clean_session_id = sanitize_unicode(snapshot.session_id)
    clean_created_at = sanitize_unicode(snapshot.created_at)
    clean_transcript_path = (
        sanitize_unicode(snapshot.transcript_path) if snapshot.transcript_path is not None else None
    )
    clean_metadata = _sanitize_json_value(dict(snapshot.metadata))

    # Fast path for snapshots that plausibly fit in pretty JSON (no arbitrary 200-item cliff).
    total_items = (
        len(ordered_pins)
        + len(superseded_dicts)
        + len(traj_dicts)
        + len(resolved_traj_dicts)
        + len(thrash_dicts)
        + len(attempt_dicts)
        + len(modified_files)
    )
    if total_items * 50 <= max_chars:
        full_payload: dict[str, Any] = {
            "schema_version": snapshot.schema_version,
            "session_id": clean_session_id,
            "created_at": clean_created_at,
            "transcript_path": clean_transcript_path,
            "pins": [p for _, p in ordered_pins],
            "trajectories": traj_dicts + resolved_traj_dicts,
            "thrash_entries": thrash_dicts,
            "attempts": attempt_dicts,
            "modified_files": modified_files,
            "metadata": clean_metadata,
        }
        if superseded_dicts:
            full_payload["superseded_pins"] = superseded_dicts
        pretty = sanitize_unicode(
            json.dumps(full_payload, indent=2, ensure_ascii=False, allow_nan=False)
        )
        if len(pretty) <= max_chars:
            return pretty

    envelopes: list[dict[str, Any]] = [
        {
            "schema_version": snapshot.schema_version,
            "session_id": clean_session_id,
            "created_at": clean_created_at,
            "transcript_path": clean_transcript_path,
            "pins": [],
            "trajectories": [],
            "thrash_entries": [],
            "attempts": [],
            "modified_files": [],
            "metadata": clean_metadata,
            **({"superseded_pins": []} if superseded_dicts else {}),
        },
        {
            "schema_version": snapshot.schema_version,
            "session_id": clean_session_id,
            "created_at": clean_created_at,
            "pins": [],
            "trajectories": [],
            "thrash_entries": [],
            "attempts": [],
            "modified_files": [],
        },
        {
            "session_id": clean_session_id,
            "pins": [],
            "trajectories": [],
            "thrash_entries": [],
            "modified_files": [],
        },
        {
            "session_id": clean_session_id,
            "pins": [],
        },
        {
            "pins": [],
        },
    ]

    Score = tuple[int, int, int, int, int, int, int, int, int]

    def _try_envelope(env_idx: int, template: dict[str, Any]) -> tuple[str, Score] | None:
        env: dict[str, Any] = {
            k: ([] if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v))
            for k, v in template.items()
        }
        used = len(_compact_json(env))
        if used > max_chars:
            return None

        full_tier1 = 0
        included_tier1 = 0
        tier1_chars = 0
        full_pins = 0
        included_pins = 0
        pin_chars = 0

        def _fill_pins(items: Sequence[tuple[bool, dict[str, Any]]]) -> bool:
            nonlocal used, full_tier1, included_tier1, tier1_chars
            nonlocal full_pins, included_pins, pin_chars
            if "pins" not in env or not items:
                return True
            bucket: list[Any] = env["pins"]
            for allow_truncate, item in items:
                sep = 1 if bucket else 0
                serialized = _compact_json(item)
                needed = sep + len(serialized)
                is_tier1 = item.get("source") in ("manual", "override")
                if used + needed <= max_chars:
                    bucket.append(item)
                    used += needed
                    text_len = len(str(item.get("text", "")))
                    full_pins += 1
                    included_pins += 1
                    pin_chars += text_len
                    if is_tier1:
                        full_tier1 += 1
                        included_tier1 += 1
                        tier1_chars += text_len
                elif allow_truncate and "text" in item:
                    remaining = max_chars - used - sep
                    truncated = _truncate_pin_for_json_budget(item, remaining)
                    if truncated is not None:
                        truncated_json, preserved_len = truncated
                        bucket.append(json.loads(truncated_json))
                        used += sep + len(truncated_json)
                        included_pins += 1
                        pin_chars += preserved_len
                        if is_tier1:
                            included_tier1 += 1
                            tier1_chars += preserved_len
                    return False
                else:
                    return False
            return True

        def _fill_list(key: str, items: Sequence[Any]) -> bool:
            nonlocal used
            if key not in env or not items:
                return True
            bucket: list[Any] = env[key]
            for item in items:
                sep = 1 if bucket else 0
                serialized = _compact_json(item)
                needed = sep + len(serialized)
                if used + needed <= max_chars:
                    bucket.append(item)
                    used += needed
                else:
                    return False
            return True

        all_fit = _fill_pins(ordered_pins)
        if all_fit:
            all_fit = _fill_list("trajectories", traj_dicts)
        if all_fit:
            all_fit = _fill_list("thrash_entries", thrash_dicts)
        if all_fit:
            all_fit = _fill_list("attempts", attempt_dicts)
        if all_fit:
            all_fit = _fill_list("modified_files", modified_files)
        if all_fit and resolved_traj_dicts:
            all_fit = _fill_list("trajectories", resolved_traj_dicts)
        if all_fit and superseded_dicts:
            _fill_list("superseded_pins", superseded_dicts)

        rendered = _compact_json(env)
        if len(rendered) > max_chars:
            return None

        tier3_count = (
            len(env.get("trajectories", []))
            + len(env.get("thrash_entries", []))
            + len(env.get("attempts", []))
        )
        file_count = len(env.get("modified_files", []))
        score: Score = (
            full_tier1,
            included_tier1,
            tier1_chars,
            full_pins,
            included_pins,
            pin_chars,
            tier3_count,
            file_count,
            -env_idx,
        )
        return rendered, score

    best_result: str | None = None
    best_score: Score | None = None

    for idx, env_template in enumerate(envelopes):
        attempt = _try_envelope(idx, env_template)
        if attempt is None:
            continue
        rendered_candidate, score = attempt
        if best_score is None or score > best_score:
            best_score = score
            best_result = rendered_candidate

    return best_result if best_result is not None else "{}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_snapshot(
    snapshot: CompactionSnapshot,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    output_format: Literal["markdown", "json"] = "markdown",
) -> str:
    """Render ``snapshot`` within an exact ``max_chars`` character budget.

    Parameters
    ----------
    snapshot:
        The ``CompactionSnapshot`` to render.
    max_chars:
        Hard maximum character length of the returned string (must be ``>= 0``).
        The returned string ALWAYS satisfies ``len(result) <= max_chars``.
    output_format:
        ``"markdown"`` (default) or ``"json"``. For ``"json"``, returns valid
        JSON whenever ``max_chars >= 2`` (and ``""`` for ``max_chars < 2``).
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError(f"max_chars must be a non-negative integer, got {max_chars!r}")
    if output_format == "markdown":
        return _render_markdown(snapshot, max_chars)
    if output_format == "json":
        return _render_json(snapshot, max_chars)
    raise ValueError(
        f"Unsupported output_format: {output_format!r} (expected 'markdown' or 'json')"
    )
