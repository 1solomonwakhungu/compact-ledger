---
name: compact-ledger
description: Pin user rules and corrections so they survive context compaction, and read the "Compact Ledger" block that appears after compaction. Use when the user states a hard constraint ("never touch X", "always use Y"), corrects an earlier instruction, asks you to remember something for the rest of the session, or when a "## Compact Ledger" block appears in context.
---

# compact-ledger

`compact-ledger` snapshots this session right before Claude Code compacts it and injects a
short ledger afterwards. The hooks capture most rules on their own. Your job is to pin the
ones that must not be lost, and to obey the ledger when it shows up.

## When a "## Compact Ledger" block appears

It was recorded verbatim before compaction, so trust it over the compaction summary.

| Line | Meaning | What to do |
| --- | --- | --- |
| `[MANUAL]` | A rule pinned on purpose | Follow it for the rest of the session |
| `[OVERRIDE] … (supersedes: …)` | The user corrected an earlier instruction | Follow the new text; the superseded text is dead |
| `[DO NOT]` | A prohibition in the user's words | Do not do it, even if the summary implies otherwise |
| `[CONSTRAINT]` | A standing instruction from the user | Keep following it |
| `[OSCILLATING]` / `[STALLED]` | A test whose values cycle or do not move, oldest first | Stop repeating the edits that produced those values; change approach |
| `[PROGRESSING]` | Values are moving toward the expected one | Continue in that direction |
| `[THRASH/REVERT]` | A file edited and reverted while tests kept failing | Do not reapply the reverted edit |
| `[RESOLVED]` | A test that failed earlier and now passes | No action |

## Pin a rule

Pin as soon as the user states a hard constraint or corrects one. Use their words.

```bash
compact-ledger pin "Use July 31 as the billing cutoff, not June 30"
compact-ledger pin "Never touch db/migrations/" --negative --tag db
```

Inside Claude Code the session id comes from `$CLAUDE_CODE_SESSION_ID`, so `--session-id` is
optional. Pins outrank every other line, survive re-snapshots, and are kept even under a
tight character budget.

When the user replaces a pinned rule, remove the old pin and add the new one:

```bash
compact-ledger status --session-id "$CLAUDE_CODE_SESSION_ID"   # lists pin IDs and text
compact-ledger unpin "<pin-id or exact text>"
compact-ledger pin "<the new rule>"
```

## Inspect

```bash
compact-ledger inject                    # the ledger exactly as it will be injected
compact-ledger inject --max-chars 1200   # the same under a tight budget
compact-ledger status --json             # stored sessions, store directory, current session
```

Exit codes: `0` ok, `1` nothing stored for that session, `2` usage error. Add `--json` to
`pin`, `status` and `snapshot` for machine-readable output.

## Limits to keep in mind

- Rule detection is pattern-based, with no model in the loop. A rule phrased as a casual
  remark can be missed, so pin anything the user would be upset to see forgotten.
- Do not pin secrets. Pins are stored as plain text in `.compact-ledger/` (git-ignored).
- The ledger only records what happened; it does not replace reading the code.
