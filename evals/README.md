# compact-ledger Evaluation Suite

This directory provides a direct runner for the **24 seeded deterministic evaluation cases** packaged inside `compact_ledger.evals`.

## Running the Evals

Via the installed CLI (works from any working directory):

```bash
uv run compact-ledger eval
uv run compact-ledger eval --verbose
uv run compact-ledger eval --format json
```

Or directly via the helper script in this folder:

```bash
uv run python evals/run_evals.py --verbose
```

## Coverage (24 Seeded Multi-Turn Cases)

| Category | Cases | What is verified |
| --- | :---: | --- |
| `constraints` | 4 | Verbatim positive & negative constraints (`[DO NOT]`), chatter filtering, Claude Code & Codex harness feedback/background-notice filtering, Codex `apply_patch` transcripts, manual pin priority over auto constraints and ledger items |
| `overrides` | 7 | Superseding date corrections, *"ignore what I said earlier / switch from X to Y"*, 3-step `A -> B -> C` override chains, casual *"actually"* false-positive guard, unrelated topic isolation, 3–4 KB multi-rule opening brief + single-rule correction, contradictory negative-constraint retirement with scope/qualifier guards |
| `trajectories` | 7 | `pytest` `PROGRESSING` / `OSCILLATING` / `STALLED` / `pytest.raises` parametrized IDs, scoped vs. unscoped pass resolution guard, ANSI-colored `jest` & `vitest` (`Expected`/`Received`), `go test` subtest `got`/`want` + generic command failure & resolution, real-shape Codex code-mode `exec` & `item_completed` rollouts |
| `thrash` | 2 | Edit-revert hunk thrashing (`[THRASH/REVERT]`) and negative guard ensuring clean edit-then-pass produces zero false warnings |
| `budget` | 2 | Exact `<= max_chars` compliance at `400` / `1200` / `6000` (Markdown & JSON), user pin survival under tight caps, and non-starvation of negative constraints when an override is huge |
| `hooks` | 2 | Full Claude Code `PreCompact` -> `SessionStart` JSON round-trip (`compact` & `resume`), `startup`/`clear`/`--always` gating, and fail-open vs. `--strict` safety |
