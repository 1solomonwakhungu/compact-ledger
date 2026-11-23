# STATUS

Last updated: 2026-11-23 (branch `dogfood/fable-usability`)

## Just completed

Cold dogfood of `compact-ledger` 0.1.0 on four real Claude Code transcripts, three real Codex
rollouts and a synthetic 62-record billing session, followed by fixes for what it exposed.

- **Per-rule pins.** A long brief or rule list is split into one pin per rule. Before, the
  whole message was one pin, so a one-line correction retired every rule in it.
- **Harness text is no longer treated as user text.** Stop/PreToolUse hook feedback,
  compaction summaries, `<bash-input>`, embedded `<system-reminder>` spans and Codex
  "Background command completed" notices are dropped.
- **Renderer.** Failure and thrash lines no longer repeat the status, test id, path and
  counts; paths are relative to the project; a preamble is added only from spare budget.
- **One store per project.** Hooks and CLI resolve the same `.compact-ledger` from any
  subdirectory (nearest existing store, else repo root). The store git-ignores itself.
- **Agent ergonomics.** `--session-id` defaults to `$CLAUDE_CODE_SESSION_ID`; `pin --json`;
  `status` lists pin IDs; `--help` has examples; `init` defaults to Claude Code and warns
  when the hook command is not on `PATH`.
- **Distribution.** Claude Code plugin (`.claude-plugin/`, `hooks/hooks.json`,
  `bin/compact-ledger` launcher), `skills/compact-ledger/SKILL.md`, PyPI metadata, README
  quick start.

## Key results

- `uv run pytest`: 290 passed (255 before, 35 new in `tests/test_usability.py`).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- `compact-ledger eval`: 20/20.
- `claude plugin validate .`: passes (one optional warning: no icon).
- Injection size on the four real transcripts: 4610/3021/3485/3208 chars before,
  1894/1724/1043/1932 after.
- Snapshot of a 1.3 MB real transcript: about 0.18 s.

## Not verified

- The plugin has not been installed into a live Claude Code session; only the manifest
  validator and the launcher were run.
- No live `/compact` round trip inside Claude Code. Hooks were driven with hand-built stdin
  payloads.

## Next

1. Parse Codex code-mode `exec` tool calls (`tools.exec_command({...})`, `tools.apply_patch`).
   Real rollouts currently yield zero test trajectories and zero modified files.
2. Publish to PyPI so `uvx compact-ledger` works, and add a plugin icon.
3. Retire sibling rules that a correction contradicts (`Use tabs. Never use spaces.` then
   `Actually use 2 spaces`).
4. A `doctor` command that reports whether the hooks fired and what they wrote.
5. Add an eval case for a long brief plus correction; the 20 seeded cases all use
   one-sentence turns.
