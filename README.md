# compact-ledger

**Deterministic `PreCompact` + `SessionStart` memory injector for AI coding agents.**

Context compaction drops the exact things an agent needs most during long sessions: verbatim user constraints, corrections that superseded earlier instructions, and the exact values observed across failed test attempts.

`compact-ledger` parses your session transcript right before compaction and deterministically re-injects a compact, token-budgeted block on resume:

1. **Active user pins & superseding corrections** (so *"actually use July 31, not June 30"* survives compaction verbatim).
2. **Structured attempt & thrash ledger** (so the agent sees `400 -> 200 -> 100` as progress and `400 -> 200 -> 400` as going in circles, instead of retrying dead ends).

Zero LLM calls. Zero third-party runtime dependencies. Fail-open hooks.

*MVP currently under active build — see [PLAN.md](PLAN.md).*
