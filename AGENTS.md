# AGENTS.md — compact-ledger

## Mission
Build **compact-ledger**, a deterministic, zero-LLM `PreCompact` / `SessionStart` memory injector that preserves verbatim user constraints (including superseding corrections) and a structured test/debug attempt ledger across AI coding agent context compactions.

## Tech Stack & Constraints
- **Language:** Python `>=3.11` managed with `uv`.
- **Runtime dependencies:** **Zero** third-party runtime dependencies (Python standard library only) so hooks run in <50ms with zero install friction across Python 3.11–3.15.
- **Dev dependencies:** `pytest`, `ruff`.
- **Package layout:** `src/compact_ledger/` with console entry point `compact-ledger = "compact_ledger.cli:main"`.
- **No LLM calls anywhere.** Everything is deterministic, local, and fail-open on hook execution.

## Quality Bar
- `uv run pytest` passes 100% with zero skipped tests.
- `uv run ruff check .` and `uv run ruff format --check .` pass cleanly.
- Strict token/character budget enforcement: user pins ALWAYS take priority over attempt history, and output never blows up the post-compaction context window.
- Hook commands always fail-open (exit 0 on unexpected internal error) so a bug never breaks a user's live agent session.

## Mandatory Worker PR, Review & Merge Workflow
Every implementation worker thread owns its PR end-to-end (the orchestrator does NOT review or merge PRs):
1. Work in your assigned git worktree branched from `origin/main`.
2. Implement your milestone cleanly with thorough unit + integration tests.
3. Run `uv run pytest && uv run ruff check . && uv run ruff format --check .`.
4. Commit with clear commit messages, push to `origin`, open a PR targeting `main` via `gh pr create`, and call `link_pull_request` with the full PR URL.
5. **Mandatory Fable 5.2 Review:**
   - Call `orchestrator_capabilities` once in your thread before your first `delegate_task`.
   - Start a review delegate using `delegate_task` with:
     - `role: "review"`
     - `mode: "wait"` (or `"async"` and wait for wakeup)
     - `target: { "providerInstanceId": "claudeAgent_claude_proxy", "model": "claude-fable-5-2", "options": { "effort": "high" } }`
   - Pass the PR URL/number, `git diff origin/main...HEAD` (or instructions to inspect it), and test output. Ask Fable 5.2 for an adversarial review covering edge cases, false positives in override detection, parser robustness, budget enforcement, and hook safety.
   - Fix all valid findings, re-run tests & ruff, and push updates. If changes were non-trivial, run a fresh `delegate_task` re-check with Fable 5.2 (`claudeAgent_claude_proxy` / `claude-fable-5-2`) until approved with no blocking issues.
6. **CI & Self-Merge:**
   - Check PR checks (`gh pr view <num> --json statusCheckRollup`). If GitHub Actions CI is still running, call `watch_pull_request` and end your turn to wake on completion (do NOT use `sleep` or `gh --watch`).
   - If `origin/main` moved while you worked and there are conflicts, `git fetch origin && git rebase origin/main`, resolve conflicts, re-run tests, and push.
   - Once Fable 5.2 approves and CI is green, merge your PR yourself:
     `gh pr merge <num> --squash --delete-branch`
   - Report the merged PR URL, Fable 5.2 review summary, and final test results in your final message.
