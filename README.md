# compact-ledger

**Deterministic `PreCompact` + `SessionStart` memory injector for AI coding agents.**

Long coding sessions hit context compaction right when precision matters most. Lossy summaries routinely drop or blur verbatim user rules, forget mid-session corrections (*"actually use July 31, not June 30"*), and erase the exact assertion values observed across failed test runs — causing the post-compaction agent to break constraints or repeat dead-end edits.

`compact-ledger` parses the session transcript right before compaction and re-injects a short, strictly budgeted ledger on resume:

1. **Manual pins & superseding corrections (Tier 1)**: verbatim user overrides linked to what they superseded, so the agent sees both the active rule and the stale one.
2. **Active verbatim user constraints (Tier 2)**: positive and negative (`[DO NOT]`) directives, one pin per rule even when they arrive in one long brief, with chatter and harness-written messages filtered out.
3. **Structured attempt & edit-thrash ledger (Tier 3)**: ordered failure trajectories across `pytest`, `vitest`, `jest`, `go test`, and generic commands (`PROGRESSING`, `OSCILLATING`, `STALLED`, `RESOLVED`) plus `[THRASH/REVERT]` warnings when edits loop or revert.
4. **Modified files summary (Tier 4)**: deduplicated list of files touched in the session.

- **Zero LLM calls** — 100% deterministic, local, reproducible output.
- **Zero third-party runtime dependencies** — Python `>=3.11` standard library only.
- **Linear-time & lightweight** — O(N) scanning with atomic JSON storage (runs all 20 seeded eval cases in ~30 ms and scales linearly with transcript size).
- **Fail-open hooks** — hook commands exit `0` without polluting stdout on malformed input, bad flags, broken pipes, or unexpected errors (unless `--strict` is passed) so a hook issue never disrupts a live agent session.

---

## Quick start

### Claude Code plugin (no Python setup)

```text
/plugin install compact-ledger --marketplace 1solomonwakhungu/compact-ledger
```

The plugin bundles the hooks, a `compact-ledger` command for the Bash tool, and a skill that
teaches the agent when to pin a rule. It runs from the plugin's own source with any Python
3.11+ already on the machine and installs nothing.

### CLI + hooks

```bash
uv tool install git+https://github.com/1solomonwakhungu/compact-ledger.git   # or: pip install git+…
cd your-project
compact-ledger init --write      # merges PreCompact + SessionStart hooks into .claude/settings.json
```

Restart Claude Code, run `/compact` once, then `compact-ledger status` to confirm a snapshot
was written. `init` warns if `compact-ledger` is not on `PATH`, because a hook that cannot
find its command fails silently.

Try it without installing: `uvx --from git+https://github.com/1solomonwakhungu/compact-ledger.git compact-ledger eval`.

Local development:

```bash
git clone https://github.com/1solomonwakhungu/compact-ledger.git
cd compact-ledger
uv sync --dev
uv run pytest
```

---

## What the agent sees after compaction

A messy session: a long opening brief with four rules, a date correction, a tool choice that
gets reversed, chatter, a hook complaint, and two tests that keep failing.

- Turn 1 (brief): *"…Use June 30 as the billing cutoff… Never touch migrations under
  db/migrations/… Don't add any new third-party dependencies… All money math must use
  `Decimal`, never floats."*
- *"ok sounds good"*, *"thanks!"*, *"wait, actually can you show me the diff so far?"* (chatter and questions, dropped)
- *"Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year."*
- *"For the frontend bits use pnpm, not npm. And always run the tests with -x…"* then
  *"hmm scratch that, the CI image doesn't have pnpm. Stick with npm."*
- `Stop hook feedback: …` (written by the harness, dropped)
- `test_proration` fails with `400 -> 200 -> 150`; `test_tax_cap` fails with `400 -> 200 -> 400`
  while `src/billing/tax.py` is edited and reverted

This is the exact block injected on resume:

```markdown
## Compact Ledger (Session: billing-refactor)
Recorded verbatim before compaction. Pins, overrides and constraints are the user's standing instructions: where the compaction summary disagrees, follow these. Failure lines list values already observed, oldest first; do not retry an approach that produced them.

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] hmm scratch that, the CI image doesn't have pnpm. Stick with npm. (supersedes: For the frontend bits use pnpm, not npm.)
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year. (supersedes: Use June 30 as the billing cutoff date for the fiscal-year rollover.)

### User Constraints
- [DO NOT] [turn 9] Makes sense. One more thing — I pasted the staging DB URL earlier in Slack, do not commit it or anything under .env* to the repo.
- [CONSTRAINT] [turn 6] And always run the tests with -x so we stop at the first failure.
- [CONSTRAINT] [turn 1] Refactor `compute_proration()` so it's a pure function.
- [DO NOT] [turn 1] Never touch migrations under db/migrations/ — the DBA team owns those.
- [DO NOT] [turn 1] Don't add any new third-party dependencies.
- [CONSTRAINT] [turn 1] Keep the public signature of `compute_proration(account, cutoff)` stable, the mobile team imports it.
- [DO NOT] [turn 1] All money math must use `Decimal`, never floats.

### Active Test Failures & Thrash Warnings
- [OSCILLATING] tests/test_billing.py::test_tax_cap (pytest) [400 -> 200 -> 400; expected 100] @ tests/test_billing.py:88
- [PROGRESSING] tests/test_billing.py::test_proration (pytest) [400 -> 200 -> 150; expected 100] @ tests/test_billing.py:42
- [SINGLE_FAILURE] npm run lint (generic) [exit 1: ✖ 2 problems (2 errors, 0 warnings)]
- [SINGLE_FAILURE] src/ui/Invoice.test.ts > Invoice > shows prorated amount (vitest) ['$4.00'; expected '$1.00'] @ src/ui/Invoice.test.ts:31
- [THRASH/REVERT] src/billing/tax.py (2 edits, 5 failures): Reverted prior edit from turn 7 while failures persisted

### Modified Files
- src/billing/proration.py
- src/billing/tax.py
- services/ledger/rollover.go
- CHANGELOG.md

### Resolved Tests
- [RESOLVED] github.com/acme/billing/services/ledger::TestRollover (go) [2026-06-30 -> PASS; expected 2026-07-31] @ rollover_test.go:27
```

How to read it:

- A long message becomes **one pin per rule**. The July 31 correction retires only the
  June 30 line; the other rules from the same brief stay active.
- `(supersedes: …)` shows the dead instruction next to the live one.
- Failure lines list observed values oldest first. `400 -> 200 -> 400` on `test_tax_cap` plus
  the `[THRASH/REVERT]` line says the last change to `tax.py` was already tried and undone.
- Paths are relative to the project. The preamble is only added when it fits in spare budget.

Output is strictly bounded (`len(output) <= --max-chars`, default 6000). Under a tight
budget, pins and overrides come first, then constraints, then failures, then files.

---

## For agents

Inside Claude Code every shell command gets `CLAUDE_CODE_SESSION_ID`, so `--session-id` is
optional:

```bash
compact-ledger pin "Never push to main" --negative --tag git   # survives every compaction
compact-ledger inject                                           # what will be injected
compact-ledger status --json                                    # sessions, store dir, current session
compact-ledger status --session-id "$CLAUDE_CODE_SESSION_ID"    # pin IDs and text, for unpin
```

Rule detection is pattern-based. Pin anything the user would be upset to see forgotten.
[skills/compact-ledger/SKILL.md](skills/compact-ledger/SKILL.md) is a ready-made skill with
the same guidance and a table for reading each ledger tag; the plugin installs it, or copy it
into `.claude/skills/compact-ledger/`.

Exit codes: `0` ok, `1` not found or runtime error, `2` usage error. Hook commands always
exit `0` unless `--strict` is passed.

---

## Claude Code hooks

`compact-ledger init --write` adds these entries to `.claude/settings.json`. It preserves
existing hooks and settings, writes atomically, and changes nothing on a second run. Without
`--write` it prints the merged file.

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

- **`PreCompact`** reads Claude Code's stdin JSON (`session_id`, `transcript_path`, `cwd`, `trigger`), parses the transcript, and atomically writes `.compact-ledger/sessions/<session_id>.json`.
- **`SessionStart`** reads stdin JSON (`session_id`, `source`, `cwd`) and, only when `source` is `"compact"` or `"resume"` (or `--always` is passed), emits `hookSpecificOutput.additionalContext` with the bounded Markdown ledger.

Use `--command` to change the hook command prefix, for example `--command "uvx compact-ledger"` once the package is on PyPI.

### Where snapshots live

One store per project, at `<project root>/.compact-ledger/`. The project root is the nearest
ancestor that already has a `.compact-ledger` directory, else the nearest ancestor with
`.git`, else the starting directory. Hooks start from `CLAUDE_PROJECT_DIR` (or the payload
`cwd`), CLI commands from the current directory, so a hook and an agent running
`compact-ledger pin` from a subdirectory reach the same store.

Override with `--store-dir` or `COMPACT_LEDGER_DIR`. The store writes its own `.gitignore`,
because snapshots contain user messages verbatim.

---

## Known limits

- **Claude Code is the only wired integration.** `snapshot` parses Codex rollouts and
  generic JSONL, but Codex has no compaction hook to attach to, and Codex's current
  code-mode `exec` tool calls are not yet parsed into test runs or file edits. User rules
  from Codex transcripts are still extracted.
- **Rule detection is heuristic.** Casual phrasings can be missed and some task lines in a
  long brief are labelled `[CONSTRAINT]`. Manual pins are the reliable path.
- **Sibling rules can outlive a correction.** `Use tabs. Never use spaces.` followed by
  `Actually use 2 spaces instead of tabs.` retires `Use tabs.` and leaves
  `Never use spaces.` active. Prohibitions are only retired by an explicit retraction.
- **Not on PyPI yet.** Install from GitHub or as a plugin.

---

## CLI Reference

All session commands accept `--store-dir <dir>` (or `COMPACT_LEDGER_DIR`). `--session-id`
defaults to `COMPACT_LEDGER_SESSION_ID`, then `CLAUDE_CODE_SESSION_ID`.

| Command | Description |
| --- | --- |
| `compact-ledger init [--write] [--settings-path <path>] [--command <cmd>]` | Print or atomically merge `PreCompact` & `SessionStart` hooks into `.claude/settings.json` (idempotent; refuses invalid JSON without corruption; warns when the hook command is not on `PATH`). |
| `compact-ledger pin "<text>" [--session-id <id>] [--tag <tag>...] [--negative] [--json]` | Add a high-priority manual pin that survives across snapshots and tight budgets. |
| `compact-ledger unpin "<pin-id-or-text>" [--session-id <id>]` | Remove a manual pin by pin ID or verbatim text. |
| `compact-ledger snapshot --transcript <path> [--session-id <id>] [--format auto\|claude-code\|codex\|generic] [--json]` | Parse a JSONL transcript, preserve manual pins, compute overrides/trajectories/thrash, and save an atomic snapshot. |
| `compact-ledger inject [--session-id <id>] [--max-chars 6000] [--format markdown\|json]` | Render the stored snapshot strictly within `--max-chars`. |
| `compact-ledger status [--session-id <id>] [--json]` | List stored sessions (the current one is marked `*`), or show one session's counts and manual pins. |
| `compact-ledger clear [--session-id <id>]` | Delete the stored snapshot for a session. |
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
