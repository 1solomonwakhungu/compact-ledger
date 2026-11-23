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

_TRAJECTORY_PRIORITY: dict[str, int] = {
    "OSCILLATING": 0,
    "STALLED": 1,
    "PROGRESSING": 2,
    "SINGLE_FAILURE": 3,
    "RESOLVED": 4,
}


# ---------------------------------------------------------------------------
# Shared text & ordering helpers
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


def _partition_pins(
    pins: Sequence[PinEntry],
) -> tuple[list[PinEntry], list[PinEntry], list[PinEntry], list[PinEntry], dict[str, PinEntry]]:
    """Partition pins into active manual, active overrides, active auto, and superseded.

    Returns ``(manual, overrides, auto, superseded, pins_by_id)`` where:
    - ``manual`` preserves original order.
    - ``overrides`` and ``auto`` are ordered newest first.
    - Superseded pins are NEVER included in the active lists.
    """
    pins_by_id: dict[str, PinEntry] = {p.pin_id: p for p in pins}

    superseded_ids: set[str] = set()
    for p in pins:
        if p.superseded_by is not None and p.superseded_by != p.pin_id:
            superseded_ids.add(p.pin_id)
        if p.supersedes is not None and p.supersedes != p.pin_id:
            superseded_ids.add(p.supersedes)

    # Guard against cycles of any length (e.g., A -> B -> A or A -> B -> C -> A):
    # if following superseded_by pointers loops back to a visited pin, keep the
    # newest pin in that cycle active so malformed cyclic inputs don't vanish.
    visited_global: set[str] = set()
    for p in pins:
        if p.pin_id not in superseded_ids or p.pin_id in visited_global:
            continue
        path: list[str] = []
        path_index: dict[str, int] = {}
        cur: str | None = p.pin_id
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
                superseded_ids.discard(newest_id)
                break
            path_index[cur] = len(path)
            path.append(cur)
            nxt = pins_by_id[cur].superseded_by
            cur = nxt if nxt != cur else None
        visited_global.update(path)

    manual: list[PinEntry] = []
    overrides_indexed: list[tuple[int, int, PinEntry]] = []
    auto_indexed: list[tuple[int, int, PinEntry]] = []
    superseded: list[PinEntry] = []

    for idx, p in enumerate(pins):
        if p.pin_id in superseded_ids:
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


def _unresolved_raw_attempts(attempts: Sequence[TestAttempt]) -> list[TestAttempt]:
    """Return unresolved failing/error raw attempts (newest first)."""
    # Determine latest outcome per test_id so already-passing tests are excluded
    latest_by_test: dict[str, tuple[int, int, TestAttempt]] = {}
    for idx, att in enumerate(attempts):
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


def _format_manual_pin(pin: PinEntry) -> str:
    prefixes: list[str] = ["[MANUAL]"]
    if pin.is_negative:
        prefixes.append("[DO NOT]")
    if pin.tags:
        clean_tags = ", ".join(_flatten_single_line(t) for t in pin.tags if t.strip())
        if clean_tags:
            prefixes.append(f"[tags: {clean_tags}]")
    return f"{' '.join(prefixes)} {_flatten_single_line(pin.text)}"


def _format_override_pin(pin: PinEntry, pins_by_id: dict[str, PinEntry]) -> str:
    prefix = "[OVERRIDE] [DO NOT]" if pin.is_negative else "[OVERRIDE]"
    body = _flatten_single_line(pin.text)
    if pin.supersedes and pin.supersedes in pins_by_id:
        old_text = _flatten_single_line(pins_by_id[pin.supersedes].text)
        if old_text:
            snippet = _truncate_text(old_text, _MAX_SUPERSEDED_SNIPPET_CHARS)
            return f"{prefix} {body} (supersedes: {snippet})"
    return f"{prefix} {body}"


def _format_constraint_pin(pin: PinEntry) -> str:
    tag = "[DO NOT]" if pin.is_negative else "[CONSTRAINT]"
    body = _flatten_single_line(pin.text)
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
        # Keep last 5 observations concise
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
    raw_attempts = _unresolved_raw_attempts(snapshot.attempts) if not snapshot.trajectories else []
    modified_files = _dedupe_files(snapshot.modified_files)

    # Build prioritized sections: (title, items, allow_truncate_last_item)
    tier1_items: list[str] = [
        *(_format_manual_pin(p) for p in manual),
        *(_format_override_pin(p, pins_by_id) for p in overrides),
    ]
    if manual and overrides:
        tier1_title = "Manual Pins & Active Overrides"
    elif manual:
        tier1_title = "Manual Pins"
    else:
        tier1_title = "Active Overrides"

    tier2_items = [_format_constraint_pin(p) for p in auto]

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

    # Choose compact header if full header would crowd out the first non-empty section
    first_non_empty = next(((t, items) for t, items, _ in sections if items), None)
    header = full_header
    if first_non_empty is not None:
        first_title, first_items = first_non_empty
        min_needed_with_full = len(full_header) + len(f"\n\n### {first_title}\n- ") + 16
        min_needed_with_compact = len(compact_header) + len(f"\n\n### {first_title}\n- ") + 16
        if min_needed_with_full > max_chars and min_needed_with_compact <= max_chars:
            header = compact_header

    if len(header) > max_chars:
        # For tiny budgets (e.g. 20 chars), still try to preserve header cleanly
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

        # Strict priority enforcement: once a higher-priority section overflows,
        # never leak lower-priority sections into leftover crumbs.
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
    return sanitize_unicode(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def _sanitize_pin_dict(pin: PinEntry) -> dict[str, Any]:
    d = pin.to_dict()
    d["pin_id"] = sanitize_unicode(d["pin_id"])
    d["text"] = sanitize_unicode(d["text"])
    if d["timestamp"] is not None:
        d["timestamp"] = sanitize_unicode(d["timestamp"])
    d["tags"] = [sanitize_unicode(t) for t in d["tags"]]
    return d


def _truncate_pin_for_json_budget(pin_dict: dict[str, Any], max_item_chars: int) -> str | None:
    """Truncate a pin dict's ``text`` field so its compact JSON fits in ``max_item_chars``."""
    base = dict(pin_dict)
    base["text"] = ""
    empty_len = len(_compact_json(base))
    if empty_len + 4 > max_item_chars:
        return None

    original_text = sanitize_unicode(str(pin_dict.get("text", "")))
    lo, hi = 1, len(original_text)
    best: str | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = dict(pin_dict)
        candidate["text"] = _truncate_text(original_text, mid)
        serialized = _compact_json(candidate)
        if len(serialized) <= max_item_chars:
            best = serialized
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
    raw_attempts = _unresolved_raw_attempts(snapshot.attempts) if not snapshot.trajectories else []
    modified_files = _dedupe_files(snapshot.modified_files)

    manual_dicts = [_sanitize_pin_dict(p) for p in manual]
    override_dicts = [_sanitize_pin_dict(p) for p in overrides]
    auto_dicts = [_sanitize_pin_dict(p) for p in auto]
    superseded_dicts = [_sanitize_pin_dict(p) for p in superseded]

    ordered_pins: list[tuple[bool, dict[str, Any]]] = [
        *((True, p) for p in manual_dicts),
        *((True, p) for p in override_dicts),
        *((True, p) for p in auto_dicts),
    ]
    tier1_count = len(manual_dicts) + len(override_dicts)

    traj_dicts = [t.to_dict() for t in unresolved_traj]
    resolved_traj_dicts = [t.to_dict() for t in resolved_traj]
    thrash_dicts = [e.to_dict() for e in thrash]
    attempt_dicts = [a.to_dict() for a in raw_attempts]

    clean_session_id = sanitize_unicode(snapshot.session_id)
    clean_created_at = sanitize_unicode(snapshot.created_at)
    clean_transcript_path = (
        sanitize_unicode(snapshot.transcript_path) if snapshot.transcript_path is not None else None
    )

    # Fast path for small snapshots that fit comfortably in pretty JSON
    total_items = (
        len(ordered_pins)
        + len(superseded_dicts)
        + len(traj_dicts)
        + len(resolved_traj_dicts)
        + len(thrash_dicts)
        + len(attempt_dicts)
        + len(modified_files)
    )
    if total_items <= 200:
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
            "metadata": dict(snapshot.metadata),
        }
        if superseded_dicts:
            full_payload["superseded_pins"] = superseded_dicts
        pretty = sanitize_unicode(json.dumps(full_payload, indent=2, ensure_ascii=False))
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
            "metadata": {},
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

    def _try_envelope(template: dict[str, Any]) -> tuple[str, int, int, int, int] | None:
        env: dict[str, Any] = {
            k: ([] if isinstance(v, list) else ({} if isinstance(v, dict) else v))
            for k, v in template.items()
        }
        used = len(_compact_json(env))
        if used > max_chars:
            return None

        def _fill_list(key: str, items: Sequence[tuple[bool, Any]]) -> bool:
            """Fill ``env[key]`` incrementally; return True if all items fit without overflow."""
            nonlocal used
            if key not in env or not items:
                return True
            bucket: list[Any] = env[key]
            for allow_truncate, item in items:
                sep = 1 if bucket else 0
                serialized = _compact_json(item)
                needed = sep + len(serialized)
                if used + needed <= max_chars:
                    bucket.append(item)
                    used += needed
                elif allow_truncate and isinstance(item, dict) and "text" in item:
                    remaining = max_chars - used - sep
                    truncated_json = _truncate_pin_for_json_budget(item, remaining)
                    if truncated_json is not None:
                        bucket.append(json.loads(truncated_json))
                        used += sep + len(truncated_json)
                    return False
                else:
                    return False
            return True

        # Strict priority order: stop filling lower tiers as soon as a higher tier overflows.
        all_fit = _fill_list("pins", ordered_pins)
        if all_fit:
            all_fit = _fill_list("trajectories", [(False, t) for t in traj_dicts])
        if all_fit:
            all_fit = _fill_list("thrash_entries", [(False, e) for e in thrash_dicts])
        if all_fit:
            all_fit = _fill_list("attempts", [(False, a) for a in attempt_dicts])
        if all_fit:
            all_fit = _fill_list("modified_files", [(False, f) for f in modified_files])
        if all_fit and resolved_traj_dicts:
            all_fit = _fill_list("trajectories", [(False, t) for t in resolved_traj_dicts])
        if all_fit and superseded_dicts:
            _fill_list("superseded_pins", [(False, p) for p in superseded_dicts])

        rendered = _compact_json(env)
        if len(rendered) > max_chars:
            return None

        included_pins = env.get("pins", [])
        included_tier1 = sum(1 for p in included_pins if p.get("source") in ("manual", "override"))
        included_tier3 = (
            len(env.get("trajectories", []))
            + len(env.get("thrash_entries", []))
            + len(env.get("attempts", []))
        )
        included_files = len(env.get("modified_files", []))
        return rendered, included_tier1, len(included_pins), included_tier3, included_files

    best_result: str | None = None
    best_score: tuple[int, int, int, int] = (-1, -1, -1, -1)
    want_pins = len(ordered_pins) > 0
    want_tier3 = bool(traj_dicts or thrash_dicts or attempt_dicts)
    want_files = bool(modified_files)

    for env_template in envelopes:
        attempt = _try_envelope(env_template)
        if attempt is None:
            continue
        rendered_candidate, included_tier1, pin_count, tier3_count, file_count = attempt
        if not want_pins:
            if (not want_tier3 or tier3_count > 0) and (not want_files or file_count > 0):
                return rendered_candidate
        elif included_tier1 >= tier1_count and pin_count > 0:
            return rendered_candidate
        score = (included_tier1, pin_count, tier3_count, file_count)
        if score > best_score:
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
