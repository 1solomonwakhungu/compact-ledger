# compact-ledger

**Deterministic `PreCompact` + `SessionStart` memory injector for AI coding agents.**

Long coding sessions hit context compaction right when precision matters most. Lossy summaries routinely drop or blur verbatim user rules, forget mid-session corrections (*"actually use July 31, not June 30"*), and erase the exact assertion values observed across failed test runs — causing the post-compaction agent to break constraints or repeat dead-end edits.

`compact-ledger` parses your session transcript right before compaction and deterministically re-injects a compact, strictly token-budgeted ledger on resume:

1. **Manual pins & superseding corrections (Tier 1)** — verbatim user overrides linked to what they superseded so the agent knows *both* the active rule and what is now stale.
2. **Active verbatim user constraints (Tier 2)** — positive and negative (`[DO NOT]`) directives, filtered of conversational chatter.
3. **Structured attempt & edit-thrash ledger (Tier 3)** — ordered failure trajectories across `pytest`, `vitest`, `jest`, `go test`, and generic commands (`PROGRESSING`, `OSCILLATING`, `STALLED`, `RESOLVED`) plus `[THRASH/REVERT]` warnings when edits loop or revert.
4. **Modified files summary (Tier 4)** — deduplicated list of files touched in the session.

- **Zero LLM calls** — 100% deterministic, local, reproducible output.
- **Zero third-party runtime dependencies** — Python `>=3.11` standard library only.
- **Fast (<50 ms)** — linear-time scanning and atomic JSON storage.
- **Fail-open hooks** — hook commands exit `0` without polluting stdout on malformed input or unexpected errors (unless `--strict` is passed) so a hook issue never disrupts a live agent session.

---

## Installation

Run without permanent installation using [`uvx`](https://docs.astral.sh/uv/):

```bash
uvx compact-ledger --version
```

Install globally as a CLI tool:

```bash
# Recommended (uv)
uv tool install compact-ledger

# Or via pip
pip install compact-ledger
```

Local development from source:

```bash
git clone https://github.com/1solomonwakhungu/compact-ledger.git
cd compact-ledger
uv sync --dev
uv run pytest
```

---

## Claude Code Setup

Generate and safely merge the `PreCompact` and `SessionStart` hooks into `.claude/settings.json`:

```bash
# Preview the generated/merged JSON without touching disk
compact-ledger init --claude-code

# Safely merge into .claude/settings.json (atomic write, preserves existing hooks, idempotent)
compact-ledger init --claude-code --write

# Optional: use uvx as the hook command prefix
compact-ledger init --claude-code --write --command "uvx compact-ledger"
```

This adds the following hook entries to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "compact-ledger hook pre-compact"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "compact|resume",
        "hooks": [
          {
            "type": "command",
            "command": "compact-ledger hook session-start"
          }
        ]
      }
    ]
  }
}
```

How it works at runtime:
- **`PreCompact`** reads Claude Code's stdin JSON (`session_id`, `transcript_path`, `cwd`, `trigger`), runs the full pin/override + test ledger pipeline, and atomically writes `.compact-ledger/sessions/<session_id>.json`.
- **`SessionStart`** reads stdin JSON (`session_id`, `source`, `cwd`) and — only when `source` is `"compact"` or `"resume"` (or `--always` is passed) — emits `hookSpecificOutput.additionalContext` with the bounded Markdown ledger.

Store location precedence for hooks: `--store-dir` > `COMPACT_LEDGER_DIR` env var > payload `cwd` (if valid existing directory) > current working directory.

---

## Before & After Compaction Example

### Before compaction (messy multi-turn session)
- User pinned manually: `Zero third-party runtime dependencies.` (tag: `arch`)
- Turn 1: *"Use June 30 as the billing cutoff date."*
- Turn 2: *"Never touch migrations under db/migrations/."*
- Turn 3: *"ok sounds good"* (chatter — filtered out)
- Turn 4: *"Actually use July 31 instead of June 30 for the billing cutoff."*
- Turns 5–7: `pytest tests/test_billing.py::test_proration` fails with `400 -> 200 -> 150` (expected `100`)
- Turns 8–10: `pytest tests/test_billing.py::test_tax_cap` fails with `400 -> 200 -> 400` (expected `100`) while `src/billing/tax.py` is edited and reverted back to its earlier hunk

### After compaction (exact `compact-ledger` injection)

```markdown
## Compact Ledger (Session: billing-refactor)

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. (supersedes: Use June 30 as the billing cutoff date.)

### User Constraints
- [DO NOT] [turn 2] Never touch migrations under db/migrations/.

### Active Test Failures & Thrash Warnings
- [OSCILLATING] tests/test_billing.py::test_tax_cap (pytest): OSCILLATING: tests/test_billing.py::test_tax_cap [400 -> 200 -> 400; expected 100]
- [PROGRESSING] tests/test_billing.py::test_proration (pytest): PROGRESSING: tests/test_billing.py::test_proration [400 -> 200 -> 150; expected 100]
- [THRASH/REVERT] src/billing/tax.py (2 edits, 3 failures): Reverted prior edit from turn 8 on src/billing/tax.py while failures persisted (2 edits, 3 failures)

### Modified Files
- src/billing/proration.py
- src/billing/tax.py
```

Under tight budgets (`--max-chars 400`, `1200`, `6000`), output length is strictly bounded (`len(output) <= max_chars`) and user pins/overrides always outrank test attempts and modified files.

---

## CLI Reference

All session commands accept `--store-dir <dir>` (or `COMPACT_LEDGER_DIR`) to override the default `.compact-ledger` directory.

| Command | Description |
| --- | --- |
| `compact-ledger init --claude-code [--write] [--settings-path <path>] [--command <cmd>]` | Print or atomically merge `PreCompact` & `SessionStart` hooks into `.claude/settings.json` (idempotent; refuses invalid JSON without corruption). |
| `compact-ledger pin --session-id <id> "<text>" [--tag <tag>...] [--negative]` | Add a high-priority manual pin that survives across snapshots and tight budgets. |
| `compact-ledger unpin --session-id <id> "<pin-id-or-text>"` | Remove a manual pin by pin ID or verbatim text. |
| `compact-ledger snapshot --transcript <path> [--session-id <id>] [--format auto\|claude-code\|codex\|generic] [--json]` | Parse a JSONL transcript, preserve manual pins, compute overrides/trajectories/thrash, and save an atomic snapshot. |
| `compact-ledger inject --session-id <id> [--max-chars 6000] [--format markdown\|json]` | Render the stored snapshot strictly within `--max-chars`. |
| `compact-ledger status [--session-id <id>] [--json]` | Show detailed stats for one session or list all stored sessions. |
| `compact-ledger clear --session-id <id>` | Delete the stored snapshot for a session. |
| `compact-ledger hook pre-compact [--store-dir <dir>] [--strict]` | Claude Code `PreCompact` stdin hook handler (fail-open unless `--strict`). |
| `compact-ledger hook session-start [--store-dir <dir>] [--max-chars 6000] [--always] [--strict]` | Claude Code `SessionStart` stdin hook handler (injects on `compact`/`resume` or `--always`; fail-open unless `--strict`). |
| `compact-ledger eval [--format text\|json] [--verbose]` | Run the packaged 20-case deterministic evaluation suite from any working directory. |

---

## Evaluation Suite (20 Seeded Cases)

`compact-ledger` ships with 20 realistic multi-turn evaluation cases covering Claude Code, Codex, and generic JSONL transcripts, override chains, false-positive guards, `pytest`/`vitest`/`jest`/`go test`/generic trajectories, edit-thrash detection, strict budget truncation, and full hook round-trips.

Run it anywhere with `compact-ledger eval` (or `uv run compact-ledger eval`):

```text
compact-ledger eval (20 seeded cases)
------------------------------------------------------------------------
[PASS] 01_negative_constraints_claude_code                  (constraints )  1.08 ms
[PASS] 02_codex_transcript_constraints_and_edits            (constraints )  0.77 ms
[PASS] 03_manual_pin_priority_over_auto_and_ledger          (constraints )  1.14 ms
[PASS] 04_superseding_date_correction                       (overrides   )  0.76 ms
[PASS] 05_ignore_earlier_and_switch_from_x_to_y             (overrides   )  0.72 ms
[PASS] 06_three_step_override_chain_a_b_c                   (overrides   )  0.86 ms
[PASS] 07_false_positive_guard_casual_actually              (overrides   )  0.68 ms
[PASS] 08_unrelated_topic_isolation                         (overrides   )  0.76 ms
[PASS] 09_pytest_progressing_trajectory                     (trajectories)  1.04 ms
[PASS] 10_pytest_oscillating_trajectory                     (trajectories)  0.78 ms
[PASS] 11_pytest_stalled_and_raises_parametrized            (trajectories)  0.75 ms
[PASS] 12_pytest_scoped_vs_unscoped_resolution              (trajectories)  0.94 ms
[PASS] 13_vitest_and_jest_ansi_trajectories                 (trajectories)  0.97 ms
[PASS] 14_go_test_and_generic_fallback_trajectories         (trajectories)  1.07 ms
[PASS] 15_edit_thrash_hunk_revert_and_stalled_loop          (thrash      )  0.84 ms
[PASS] 16_clean_edit_then_pass_no_false_thrash              (thrash      )  1.08 ms
[PASS] 17_tight_budget_pins_survive_400_1200_6000           (budget      )  3.29 ms
[PASS] 18_long_override_does_not_starve_negative_constraints (budget     )  1.90 ms
[PASS] 19_hook_pre_compact_to_session_start_roundtrip       (hooks       )  1.47 ms
[PASS] 20_hook_session_start_gating_and_fail_open           (hooks       )  0.44 ms
------------------------------------------------------------------------
Category breakdown:
  - constraints    3/3 passed
  - overrides      5/5 passed
  - trajectories   6/6 passed
  - thrash         2/2 passed
  - budget         2/2 passed
  - hooks          2/2 passed
------------------------------------------------------------------------
Total: 20/20 passed (100.0%) in 27.1 ms
```

See [evals/README.md](evals/README.md) for category details or run `uv run compact-ledger eval --format json` for machine-readable output.

---

## License

MIT
