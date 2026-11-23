# MVP Plan — compact-ledger

Build a production-quality, user-ready MVP across 4 sequenced/parallel PRs.

## PR 1 (Foundation — land first on `main`)
**Branch:** `feat/foundation-store`
- Create `pyproject.toml` (`compact-ledger`, Python `>=3.11`, pure stdlib runtime, `pytest` + `ruff` dev deps, `src/` layout).
- Define core models in `src/compact_ledger/models.py`:
  - `UserTurn`, `PinEntry` (with `supersedes` link, `source`, `timestamp`), `TestAttempt` (`test_id`, `runner`, `assertion_loc`, `expected`, `actual`, `outcome`, `turn_index`), `EditThrashEntry`, `CompactionSnapshot`.
- Implement durable local session storage in `src/compact_ledger/store.py` (`.compact-ledger/sessions/<session_id>.json` with atomic temp-file writes and manual `pin` persistence).
- Implement transcript readers in `src/compact_ledger/parsers/transcript.py` for Claude Code JSONL, Codex JSONL, and generic JSONL (extracting ordered user turns, bash/tool calls + outputs, and file edits).
- Implement CLI skeleton (`src/compact_ledger/cli.py`): `snapshot`, `inject`, `pin`, `status`, `clear`, `--version`.
- Unit tests for storage atomicity, transcript parsing, and CLI commands.

## PR 2 (User Pin & Superseding Correction Engine — parallel after PR 1)
**Branch:** `feat/pins-and-overrides`
- Implement `src/compact_ledger/pins.py`:
  - Extracts verbatim user directives, constraints, negative constraints (*"don't touch X"*, *"never use Y"*, *"must keep Z"*), and explicit manual pins.
  - Detects **superseding corrections** (*"actually use July 31 instead of June 30"*, *"ignore what I said earlier about..."*, *"switch from X to Y"*, *"correction:"*) and links newer pins to earlier superseded ones so the agent knows BOTH the active rule AND what is now stale.
  - Deduplicates repetitive chatter/acknowledgements (*"ok"*, *"thanks"*, *"continue"*) so the budget stays dense and high-signal.
- Implement token/character budget allocator in `src/compact_ledger/renderer.py`:
  - Default budget (e.g., `--max-chars 6000` / ~1500 tokens, configurable).
  - Strict priority order: (1) Manual pins & active superseding corrections, (2) Remaining verbatim user constraints (newest first), (3) Active failing test attempts & thrash warnings, (4) Modified files summary.
- Thorough unit tests for override chains, negative constraints, chatter filtering, and exact budget truncation.

## PR 3 (Test Attempt Ledger & Thrash Detector — parallel after PR 1)
**Branch:** `feat/attempt-ledger`
- Implement runner output parsers in `src/compact_ledger/parsers/runners.py`:
  - **pytest**: extracts failing node IDs, `file:line`, assertion expression, and `expected` vs `actual` (from `assert X == Y`, `E   assert 400 == 100`, `AssertionError`, `pytest.raises`).
  - **vitest / jest**: extracts test names, `Expected:` / `Received:` pairs, file:line.
  - **go test**: extracts `--- FAIL: TestName`, file:line, got/want lines.
  - **generic fallback**: command + non-zero exit code + normalized last error line.
- Implement `src/compact_ledger/ledger.py`:
  - Tracks ordered attempts per unresolved test ID across the session.
  - Classifies trajectories: `PROGRESSING` (e.g., `400 -> 200 -> 100` moving or distinct values), `OSCILLATING` (`400 -> 200 -> 400`), `STALLED` (same assertion + same observed value repeated), `RESOLVED` (later passed).
  - Detects edit-revert thrashing (editing the same file repeatedly between identical failures, or reverting a prior hunk).
  - Emits concise, high-signal ledger lines (last N distinct states per unresolved test) so post-compaction agents immediately see what was already tried.
- Thorough unit tests with real pytest, vitest/jest, and `go test` failure traces.

## PR 4 (Claude Code Hooks, Seeded 20-Case Eval Suite & Docs — final PR after PR 2 & PR 3)
**Branch:** `feat/hooks-evals-docs`
- Wire pins + attempt ledger + renderer into `snapshot` and `inject` end-to-end.
- Implement `src/compact_ledger/hooks.py`:
  - `compact-ledger hook pre-compact`: reads Claude Code `PreCompact` stdin JSON (`session_id`, `transcript_path`, `cwd`), builds and saves the snapshot.
  - `compact-ledger hook session-start`: reads Claude Code `SessionStart` stdin JSON (`session_id`, `source`), and when `source` is `"compact"` or `"resume"` (or `--always`), emits structured hook JSON (`hookSpecificOutput.additionalContext`) injecting the bounded ledger.
  - Fail-open error handling (never exits non-zero on malformed stdin unless `--strict`).
- Implement `compact-ledger init --claude-code [--write]` to print or safely merge both hooks into `.claude/settings.json`.
- Build `evals/` with 20 realistic multi-turn transcripts verifying: verbatim constraint survival, superseding correction priority, pytest/jest/go progress vs oscillation classification, edit-thrash warnings, and strict budget compliance. Add `compact-ledger eval` CLI command.
- Finalize `README.md` with install (`uvx`/`pip`), Claude Code setup, before/after compaction examples, and eval benchmark output.
