# compact-ledger Launch Assets

Ready-to-post copy for Show HN, Reddit, X, and the `v0.1.1` GitHub release. Every number below comes from `uv run python evals/bench.py`, `uv run compact-ledger eval`, and `uv run pytest` on macOS (`Darwin arm64`, Python 3.13.14).

---

## 1. Show HN

**Title:**
Show HN: Compact-Ledger – PreCompact memory for Claude Code (Codex via CLI)

**Body:**
Hi HN,

I built `compact-ledger` because context compaction kept wrecking long Claude Code sessions right when precision mattered. After `/compact`, the summary routinely dropped a negative rule from turn 1 ("never touch `db/migrations/`"), mixed up a mid-session correction ("actually use July 31, not June 30"), or erased the exact assertion values from three failed `pytest` runs so the model went right back to an edit that already failed.

`compact-ledger` is a small Python package (3.11+, zero runtime dependencies, zero LLM calls) that plugs into Claude Code's `PreCompact` and `SessionStart` hooks. Right before compaction it reads the session JSONL transcript, extracts four things into a local file-locked JSON snapshot under `.compact-ledger/`, and injects a strictly budgeted Markdown block on `compact` or `resume`:

1. Manual pins (`compact-ledger pin "..."`) and superseding corrections, printed with what they retired: `[OVERRIDE] Actually use July 31 instead of June 30... (supersedes: Use June 30 as the billing cutoff date...)`. When a correction explicitly adopts something a general negative rule forbade (e.g. "Never use spaces." followed by "Actually use 2 spaces instead of tabs."), it retires the general ban while leaving scoped bans like "Never use spaces in filenames." active.
2. Active positive and `[DO NOT]` rules extracted per-rule from long opening briefs, with chatter and harness-authored messages (`Stop hook feedback:`, `<system-reminder>`, interrupted tool notices) filtered out.
3. Structured test failure trajectories across `pytest`, `vitest`, `jest`, `go test`, and generic commands, classified as `PROGRESSING`, `OSCILLATING`, `STALLED`, `SINGLE_FAILURE`, or `RESOLVED` with observed values oldest first (e.g. `[400 -> 200 -> 400; expected 100]`), plus `[THRASH/REVERT]` warnings when an edit reverts an earlier hunk while failures persist.
4. Modified files in the session, relativized to the project root.

Everything is bounded by `--max-chars` (default 6,000). Under tight caps, manual pins and overrides always win over auto-extracted rules, test trajectories, and file lists. Hook invocations fail open (exit 0 with empty stdout on malformed input or internal errors) so a hook bug never breaks a live session.

Measured numbers on an Apple Silicon Mac (Python 3.13.14, reproducible via `uv run python evals/bench.py`):
- In-process: `add_manual_pin` (atomic write + `fsync`) 0.42 ms median; `render_snapshot` 0.01 ms; `pre-compact` 1.40 ms on a 4.7 KB transcript and 11.68 ms on a 162 KB transcript (80 failing edit/test cycles); `session-start` 0.11 ms.
- Cold CLI subprocess (including Python startup): 51-55 ms median for `pin`, `inject`, `hook pre-compact`, and `hook session-start`.
- Output size: the 162 KB transcript compresses to 1,098 chars (~274 tokens); a 4.7 KB realistic session renders to 1,146 chars (~286 tokens).
- Verification: 346 unit/integration tests and 24/24 seeded end-to-end eval cases passing in ~50 ms (`uvx compact-ledger eval`).

Honest limits:
- Automatic compaction hooks are Claude Code native. Codex CLI does not expose a compaction hook yet, so Codex rollouts (including code-mode `exec` and `apply_patch` events) work via the CLI commands (`compact-ledger snapshot`, `inject`, `pin`).
- Automatic rule detection from prose is heuristic. It catches common directives and filters chatter, but manual `compact-ledger pin` is the reliable path for anything you cannot afford to lose.
- It is session-scoped compaction memory, not cross-session project memory.

Repo: https://github.com/1solomonwakhungu/compact-ledger
Quick try with no install: `uvx compact-ledger eval`
Claude Code plugin: `/plugin marketplace add 1solomonwakhungu/compact-ledger` then `/plugin install compact-ledger@compact-ledger`
CLI + hooks: `uv tool install compact-ledger && compact-ledger init --write && compact-ledger doctor`

Happy to answer questions about transcript parsing edge cases, override matching, or why I avoided putting another LLM call in the compaction path.

---

## 2. Reddit Posts

### r/ClaudeAI

**Title:**
I built a zero-LLM PreCompact/SessionStart hook that keeps verbatim rules, mid-session corrections, and failed test values alive across /compact

**Body:**
Every long Claude Code session hits `/compact` eventually, and the compaction summary has three failure modes that kept biting me:
- A negative constraint from the start of the session ("never touch `db/migrations/`", "use `Decimal`, never floats") gets dropped or softened.
- A mid-session correction ("actually use July 31, not June 30" or "scratch pnpm, stick with npm") gets blended with the old instruction.
- Exact assertion values from earlier test runs vanish, so post-compaction Claude tries the same broken fix again.

I built `compact-ledger` to fix this deterministically with no LLM call and zero third-party Python dependencies (Python 3.11+ stdlib only).

Before compaction, the `PreCompact` hook reads the session JSONL transcript and writes an atomic snapshot under `.compact-ledger/` (auto-gitignored). On `SessionStart` (`compact|resume`), it injects a short Markdown ledger capped at 6,000 chars by default. Excerpt of the block injected on resume:

```markdown
## Compact Ledger (Session: billing-refactor)
Recorded verbatim before compaction. Pins, overrides and constraints are the user's standing instructions: where the compaction summary disagrees, follow these. Failure lines list values already observed, oldest first; do not retry an approach that produced them.

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year. (supersedes: Use June 30 as the billing cutoff date for the fiscal-year rollover.)

### User Constraints
- [DO NOT] [turn 1] Never touch migrations under db/migrations/ — the DBA team owns those.
- [DO NOT] [turn 1] All money math must use `Decimal`, never floats.

### Active Test Failures & Thrash Warnings
- [OSCILLATING] tests/test_billing.py::test_tax_cap (pytest) [400 -> 200 -> 400; expected 100] @ tests/test_billing.py:88
- [THRASH/REVERT] src/billing/tax.py (2 edits, 5 failures): Reverted prior edit from turn 7 while failures persisted
```

You (or Claude, via the bundled skill) can also pin anything critical manually without `--session-id` because it reads `$CLAUDE_CODE_SESSION_ID`:

```bash
compact-ledger pin "Never push to main" --negative --tag git
```

What I measured (`uv run python evals/bench.py` on Apple Silicon, Python 3.13):
- Cold hook CLI latency including Python startup: ~51-54 ms (`pre-compact` 53.6 ms median, `session-start` 51.0 ms median).
- In-process parsing of a 162 KB transcript (80 failing edit/test cycles): 11.7 ms median, compressing down to 1,098 chars (~274 tokens).
- Hooks fail open (exit 0 on any error) so they never block a session.
- `compact-ledger doctor` checks PATH, Python version, hook wiring, plugin manifest, and store `.gitignore`.

Two ways to install in Claude Code:
1. Plugin (no Python package install needed, uses bundled launcher):
   `/plugin marketplace add 1solomonwakhungu/compact-ledger`
   `/plugin install compact-ledger@compact-ledger`
2. CLI + `.claude/settings.json` hooks:
   `uv tool install compact-ledger`
   `compact-ledger init --write && compact-ledger doctor`

One caveat: automatic rule detection is heuristic, so use `compact-ledger pin` (or install the skill with `npx skills add 1solomonwakhungu/compact-ledger`) for hard constraints you never want missed.

GitHub: https://github.com/1solomonwakhungu/compact-ledger

---

### r/codex

**Title:**
compact-ledger: deterministic transcript parser for Codex rollouts that preserves user rules, overrides, and failing test trajectories across long sessions

**Body:**
If you run long debugging sessions in Codex CLI, context truncation and handoffs tend to lose two things: verbatim constraints you gave 30 turns ago (especially when you revised one mid-session) and the exact sequence of assertion values from failing `pytest`/`vitest`/`jest`/`go test` runs.

I built `compact-ledger` (MIT, Python 3.11+, zero runtime dependencies, zero LLM calls) to parse session transcripts deterministically and render a strictly budgeted Markdown ledger you can re-inject on handoff or resume.

For Codex CLI specifically, the parser handles real rollout formats:
- Modern code-mode `exec` tool calls (`tools.exec_command`, `tools.shell_command`, `tools.apply_patch`)
- `event_msg` `item_completed` records (`CommandExecution`, `FileChange`, `UserMessage`) with cross-origin deduplication
- Legacy `function_call`, `local_shell_call`, and `apply_patch` events
- Filters out Codex harness wrappers (`<environment_context>`, `# AGENTS.md instructions`, `Background command completed (...)`) so they never leak into user rules

Workflow in Codex (since Codex does not expose automatic `PreCompact`/`SessionStart` hooks yet):

```bash
uv tool install compact-ledger
compact-ledger pin "Never touch db/migrations/" --negative --session-id refactor-1
compact-ledger snapshot --transcript ~/.codex/sessions/2026/.../rollout-*.jsonl --session-id refactor-1
compact-ledger inject --session-id refactor-1
```

You can also install the agent skill (`npx skills add 1solomonwakhungu/compact-ledger`) so Codex knows to pin rules as you state them and snapshot/inject before handoffs.

Real numbers (`uv run python evals/bench.py`, Python 3.13 on Apple Silicon):
- Parses a 162 KB transcript (80 failing edit/test loops) in 11.7 ms in-process and compresses it to 1,098 chars (~274 tokens).
- Cold CLI commands run in ~51-55 ms median (mostly Python startup).
- 346 unit/integration tests and 24/24 seeded eval cases passing in ~50 ms (`uvx compact-ledger eval`).

GitHub: https://github.com/1solomonwakhungu/compact-ledger

---

## 3. X / Twitter Launch Thread

**Tweet 1 (Hook + Demo):**
Compaction breaks long coding sessions:
1. Drops "never touch X" rules from turn 1.
2. Mixes up corrections ("use July 31, not June 30").
3. Erases failed test values.

Built `compact-ledger`: zero-LLM PreCompact memory for Claude Code (Codex via CLI).
https://github.com/1solomonwakhungu/compact-ledger

**Tweet 2 (What the agent sees):**
Before `/compact`, it parses the transcript and injects a bounded ledger:

- `[MANUAL]` locked pins
- `[OVERRIDE] Use July 31... (supersedes: Use June 30...)`
- `[DO NOT]` rules split per-rule
- `[OSCILLATING] test_tax_cap [400 -> 200 -> 400]`
- `[THRASH/REVERT] tax.py`

**Tweet 3 (Benchmarks):**
Measured via `uv run python evals/bench.py` (Python 3.13, Apple Silicon):
- Zero LLM calls, zero runtime deps
- 162 KB transcript -> parsed in 11.7 ms in-process, rendered to 1,098 chars (~274 tokens)
- Cold CLI hooks: 51-54 ms median
- 346 tests + 24/24 evals in ~50 ms

**Tweet 4 (Safety & limits):**
Hooks fail open (exit 0 on errors) so they never block a session.

Honest limits: automatic hooks are Claude Code native (Codex uses CLI `snapshot`/`inject`/`pin`), rule extraction is heuristic (use `compact-ledger pin` for hard rules), and snapshots are session-scoped.

**Tweet 5 (Try it):**
Try the 24-case eval suite:
`uvx compact-ledger eval`

Claude Code plugin:
`/plugin marketplace add 1solomonwakhungu/compact-ledger`
`/plugin install compact-ledger@compact-ledger`

Agent Skill:
`npx skills add 1solomonwakhungu/compact-ledger`

---

## 4. GitHub Release Notes (`v0.1.1`)

````markdown
## compact-ledger v0.1.1

Release `v0.1.1` follows the initial `0.1.0` PyPI upload with store/symlink hardening, concurrent-write race fixes, Codex/JS runner parser improvements, reproducible benchmarks, and CI wheel verification.

### What's new since `0.1.0`

- **Store safety & quarantine hardening**:
  - Custom `--store-dir` paths (such as `--store-dir .` inside a git repo) now write `.gitignore` inside `sessions/` instead of creating a root `./.gitignore` with `*`.
  - Intra-store session symlinks and symlinked `.lock` files are rejected before `.resolve()` / `open()`, and CLI commands consistently exit `1` on store symlink violations.
  - Corrupt snapshots are quarantined to unique timestamped backups (`<session>.json.corrupt-<timestamp>`) without overwriting prior backups, `compact-ledger pin` / `snapshot` print a stderr warning naming the backup, and `compact-ledger clear` removes both snapshots and `.corrupt*` backups.
  - Serialized `clear_session()` under the per-session file lock and removed pre-lock manual pin loading in `create_and_save_snapshot()` so a concurrent `unpin` during `PreCompact` is never resurrected.
- **Override grouping & parser fixes**:
  - Multi-sentence retractions followed by a replacement (e.g. `"Ignore my previous rule about pnpm. Stick with npm."`) now stay a single `[OVERRIDE]` unit and retire contradictory general negative constraints.
  - Reactivated negative constraints are re-indexed after bucket compaction.
  - Jest/Vitest inline test names now strip decimal durations (`1.25s`, `12.5ms`, `(1.5 s)`), Codex/shell headers recognize `Command exited with code N`, and `PreCompact` from a subdirectory relativizes paths against the project root.
  - `compact-ledger doctor` validates snapshot filename stems on a single read (preventing double-counting from mismatched stray JSON files).
- **CI, packaging & benchmarks**:
  - Migrated `pyproject.toml` to PEP 639 (`license = "MIT"`, `license-files = ["LICENSE"]`, `hatchling>=1.27`).
  - Expanded GitHub Actions CI to Python 3.11, 3.12, and 3.13 with `compact-ledger eval` and an isolated `--no-deps` wheel install smoke test.
  - Added `evals/bench.py` for reproducible in-process latency, cold CLI latency, and transcript-to-ledger footprint measurements.
  - Updated `README.md` with absolute GitHub URLs (fixing relative links on PyPI), measured benchmarks, and aligned `SKILL.md` tag and exit-code docs.

### Install

```bash
# CLI + Claude Code hooks
uv tool install compact-ledger
compact-ledger init --write
compact-ledger doctor

# Or Claude Code plugin (no Python package install)
/plugin marketplace add 1solomonwakhungu/compact-ledger
/plugin install compact-ledger@compact-ledger

# Or Agent Skill (Claude Code, Codex, Cursor, OpenCode)
npx skills add 1solomonwakhungu/compact-ledger

# Try without installing
uvx compact-ledger eval
```

### Verification

- **Tests**: 346/346 unit and integration tests passing on Python 3.11, 3.12, and 3.13.
- **Eval suite**: 24/24 seeded cases passing (100.0%) in ~50 ms.
- **Benchmarks** (`uv run python evals/bench.py`, Darwin arm64, Python 3.13.14):
  - In-process: `add_manual_pin` 0.42 ms, `render_snapshot` 0.01 ms, `hook pre-compact` 1.40 ms (4.7 KB) / 11.68 ms (162 KB), `hook session-start` 0.11 ms.
  - Cold CLI subprocess (including Python startup): 51.0-54.9 ms median.
  - Footprint: 162 KB transcript -> 1,098 chars (~274 tokens) injected Markdown.
- **Runtime dependencies**: None (Python >= 3.11 standard library only).
````
