import type { KpiMetric } from "@/components/tonal/kpi-grid";
import type { AccordionItemData } from "@/components/tonal/accordion";
import type { SurfaceTone } from "@/components/tonal/surface-tile";
import type { TonalChipProps } from "@/components/tonal/chip";

export const SITE_LINKS = {
  homepage: "https://compact-ledger.harlo.dev",
  github: "https://github.com/1solomonwakhungu/compact-ledger",
  pypi: "https://pypi.org/project/compact-ledger/",
  skillDoc:
    "https://github.com/1solomonwakhungu/compact-ledger/blob/main/skills/compact-ledger/SKILL.md",
  evalsDoc:
    "https://github.com/1solomonwakhungu/compact-ledger/blob/main/evals/README.md",
  harlo: "https://harlo.dev",
  tonal: "https://tonal.harlo.dev",
} as const;

export type InstallMethodId = "plugin" | "cli" | "skill" | "uvx";

export interface InstallMethod {
  id: InstallMethodId;
  label: string;
  badge?: string;
  summary: string;
  commands: string;
  note: string;
}

export const INSTALL_METHODS: InstallMethod[] = [
  {
    id: "plugin",
    label: "Claude Plugin",
    summary:
      "Bundles PreCompact + SessionStart hooks, a compact-ledger command, and the agent skill. Uses any Python 3.11+ on the machine and installs nothing.",
    commands: `/plugin marketplace add 1solomonwakhungu/compact-ledger
/plugin install compact-ledger@compact-ledger`,
    note: "Run directly inside Claude Code — no pip or venv setup required.",
  },
  {
    id: "cli",
    label: "CLI + Hooks",
    summary:
      "Install from PyPI, merge hooks atomically into .claude/settings.json, and verify PATH, Python >=3.11, hooks, and store health.",
    commands: `uv tool install compact-ledger
compact-ledger init --write
compact-ledger doctor`,
    note: "Or install with pip install compact-ledger. Restart Claude Code and run /compact once to verify.",
  },
  {
    id: "skill",
    label: "Agent Skill",
    summary:
      "Install via skills.sh into Claude Code, Codex, Cursor, Antigravity, OpenCode, and other skill-compatible agents.",
    commands: `npx skills add 1solomonwakhungu/compact-ledger`,
    note: "Teaches the agent when to pin rules verbatim and how to read every ledger tag.",
  },
  {
    id: "uvx",
    label: "Zero-Install (uvx)",
    summary:
      "Run the packaged 24-case deterministic evaluation suite immediately without installing anything globally.",
    commands: `uvx compact-ledger eval`,
    note: "Pair with compact-ledger init --write --command \"uvx compact-ledger\" to run hooks via uvx.",
  },
];

export type BudgetCapId = "6000" | "1200" | "400";

export interface SessionEvent {
  id: string;
  turn: string;
  actor: "User" | "Manual Pin" | "Harness" | "Test Runner" | "Editor";
  text: string;
  disposition:
    | "PINNED"
    | "OVERRIDE"
    | "SUPERSEDED"
    | "DROPPED"
    | "OSCILLATING"
    | "PROGRESSING"
    | "THRASH/REVERT";
  chipTone: TonalChipProps["tone"];
  detail: string;
}

export const BILLING_SESSION_EVENTS: SessionEvent[] = [
  {
    id: "manual-pin",
    turn: "Manual",
    actor: "Manual Pin",
    text: 'compact-ledger pin "Zero third-party runtime dependencies." --tag arch',
    disposition: "PINNED",
    chipTone: "blue",
    detail: "Tier 1 · Survives every compaction and tight budget",
  },
  {
    id: "brief-june30",
    turn: "Turn 1",
    actor: "User",
    text: "Use June 30 as the billing cutoff date for the fiscal-year rollover.",
    disposition: "SUPERSEDED",
    chipTone: "neutral",
    detail: "Retired by Turn 4 override; other Turn 1 rules remain active",
  },
  {
    id: "brief-rules",
    turn: "Turn 1",
    actor: "User",
    text: "Never touch migrations under db/migrations/… Don't add any new third-party dependencies… All money math must use Decimal, never floats.",
    disposition: "PINNED",
    chipTone: "warm",
    detail: "Tier 2 · Split into one verbatim pin per rule",
  },
  {
    id: "chatter",
    turn: "Turns 2–3",
    actor: "User",
    text: '"ok sounds good", "thanks!", "wait, actually can you show me the diff so far?"',
    disposition: "DROPPED",
    chipTone: "neutral",
    detail: "Chatter & questions filtered out",
  },
  {
    id: "override-july31",
    turn: "Turn 4",
    actor: "User",
    text: "Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year.",
    disposition: "OVERRIDE",
    chipTone: "blue",
    detail: "Tier 1 · Linked via (supersedes: Use June 30...)",
  },
  {
    id: "pnpm-to-npm",
    turn: "Turns 6→8",
    actor: "User",
    text: '"For the frontend bits use pnpm, not npm. And always run the tests with -x…" → "hmm scratch that, the CI image doesn\'t have pnpm. Stick with npm."',
    disposition: "OVERRIDE",
    chipTone: "blue",
    detail: "Tier 1 · Retires pnpm rule while preserving the -x test rule",
  },
  {
    id: "harness-noise",
    turn: "Turn 7",
    actor: "Harness",
    text: "Stop hook feedback: …",
    disposition: "DROPPED",
    chipTone: "neutral",
    detail: "Harness-written feedback excluded from user rules",
  },
  {
    id: "test-trajectories",
    turn: "Turns 3–8",
    actor: "Test Runner",
    text: "test_proration: 400 -> 200 -> 150 (expected 100); test_tax_cap: 400 -> 200 -> 400 (expected 100)",
    disposition: "OSCILLATING",
    chipTone: "warning",
    detail: "Tier 3 · Exact observed values preserved oldest first",
  },
  {
    id: "edit-thrash",
    turn: "Turns 7–8",
    actor: "Editor",
    text: "src/billing/tax.py edited twice and reverted while 5 failures persisted",
    disposition: "THRASH/REVERT",
    chipTone: "danger",
    detail: "Tier 3 · Flags dead-end revert loop",
  },
];

export interface BudgetPreset {
  id: BudgetCapId;
  label: string;
  maxChars: number;
  command: string;
  summary: string;
  tiersKept: {
    tier1: "full" | "partial";
    tier2: "full" | "dropped";
    tier3: "full" | "dropped";
    tier4: "full" | "dropped";
  };
  markdown: string;
}

export const BUDGET_PRESETS: Record<BudgetCapId, BudgetPreset> = {
  "6000": {
    id: "6000",
    label: "Full · 6,000 chars",
    maxChars: 6000,
    command: "compact-ledger inject --session-id billing-refactor --max-chars 6000",
    summary:
      "Default cap. Preamble, Tier 1 pins & overrides, Tier 2 verbatim rules, Tier 3 trajectories & thrash warnings, Tier 4 modified files, and resolved tests all fit cleanly.",
    tiersKept: {
      tier1: "full",
      tier2: "full",
      tier3: "full",
      tier4: "full",
    },
    markdown: `## Compact Ledger (Session: billing-refactor)
Recorded verbatim before compaction. Pins, overrides and constraints are the user's standing instructions: where the compaction summary disagrees, follow these. Failure lines list values already observed, oldest first; do not retry an approach that produced them.

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] hmm scratch that, the CI image doesn't have pnpm. Stick with npm. (supersedes: For the frontend bits use pnpm, not npm.)
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year. (supersedes: Use June 30 as the billing cutoff date for the fiscal-year rollover.)

### User Constraints
- [DO NOT] [turn 9] Makes sense. One more thing — I pasted the staging DB URL earlier in Slack, do not commit it or anything under .env* to the repo.
- [CONSTRAINT] [turn 6] And always run the tests with -x so we stop at the first failure.
- [CONSTRAINT] [turn 1] Refactor \`compute_proration()\` so it's a pure function.
- [DO NOT] [turn 1] Never touch migrations under db/migrations/ — the DBA team owns those.
- [DO NOT] [turn 1] Don't add any new third-party dependencies.
- [CONSTRAINT] [turn 1] Keep the public signature of \`compute_proration(account, cutoff)\` stable, the mobile team imports it.
- [DO NOT] [turn 1] All money math must use \`Decimal\`, never floats.

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
- [RESOLVED] github.com/acme/billing/services/ledger::TestRollover (go) [2026-06-30 -> PASS; expected 2026-07-31] @ rollover_test.go:27`,
  },
  "1200": {
    id: "1200",
    label: "Tight · 1,200 chars",
    maxChars: 1200,
    command: "compact-ledger inject --session-id billing-refactor --max-chars 1200",
    summary:
      "Spare-budget preamble, test trajectories, and modified files are shed automatically so 100% of Tier 1 manual pins/overrides and Tier 2 verbatim user rules survive intact.",
    tiersKept: {
      tier1: "full",
      tier2: "full",
      tier3: "dropped",
      tier4: "dropped",
    },
    markdown: `## Compact Ledger (Session: billing-refactor)

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] hmm scratch that, the CI image doesn't have pnpm. Stick with npm. (supersedes: For the frontend bits use pnpm, not npm.)
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year. (supersedes: Use June 30 as the billing cutoff date for the fiscal-year rollover.)

### User Constraints
- [DO NOT] [turn 9] Makes sense. One more thing — I pasted the staging DB URL earlier in Slack, do not commit it or anything under .env* to the repo.
- [CONSTRAINT] [turn 6] And always run the tests with -x so we stop at the first failure.
- [CONSTRAINT] [turn 1] Refactor \`compute_proration()\` so it's a pure function.
- [DO NOT] [turn 1] Never touch migrations under db/migrations/ — the DBA team owns those.
- [DO NOT] [turn 1] Don't add any new third-party dependencies.
- [CONSTRAINT] [turn 1] Keep the public signature of \`compute_proration(account, cutoff)\` stable, the mobile team imports it.
- [DO NOT] [turn 1] All money math must use \`Decimal\`, never floats.`,
  },
  "400": {
    id: "400",
    label: "Ultra-tight · 400 chars",
    maxChars: 400,
    command: "compact-ledger inject --session-id billing-refactor --max-chars 400",
    summary:
      "Hard cap strictly enforced (len(output) <= 400). Lower tiers drop cleanly while Tier 1 manual pins and superseding overrides claim the entire remaining budget.",
    tiersKept: {
      tier1: "partial",
      tier2: "dropped",
      tier3: "dropped",
      tier4: "dropped",
    },
    markdown: `## Compact Ledger (Session: billing-refactor)

### Manual Pins & Active Overrides
- [MANUAL] [tags: arch] Zero third-party runtime dependencies.
- [OVERRIDE] hmm scratch that, the CI image doesn't have pnpm. Stick with npm. (supersedes: For the frontend bits use pnpm, not npm.)
- [OVERRIDE] Actually use July 31 instead of June 30 for the billing cutoff. Finance moved the fiscal year. (supersede...`,
  },
};

export interface TierCard {
  tier: string;
  title: string;
  tone: SurfaceTone;
  chipTone: TonalChipProps["tone"];
  tags: string[];
  description: string;
  highlights: string[];
}

export const PRIORITY_TIERS: TierCard[] = [
  {
    tier: "Tier 1 · Highest Priority",
    title: "Manual Pins & Superseding Overrides",
    tone: "sky",
    chipTone: "blue",
    tags: ["[MANUAL]", "[OVERRIDE]", "(supersedes: …)"],
    description:
      "Explicit manual pins and mid-session user corrections always claim budget first. Active overrides print alongside the exact rule they retired so the resumed agent never blends old and new instructions.",
    highlights: [
      "Retires only the matching rule in a multi-rule brief — sibling rules stay active",
      "Handles multi-sentence retractions (e.g. “scratch that… stick with npm”) as one unit",
      "Retires general bans when explicitly replaced while keeping scoped bans active",
    ],
  },
  {
    tier: "Tier 2 · Verbatim Rules",
    title: "Active User Constraints",
    tone: "warm",
    chipTone: "warm",
    tags: ["[DO NOT]", "[CONSTRAINT]", "Per-rule split"],
    description:
      "Positive and negative directives extracted verbatim from user turns, newest first. One long opening brief splits into one pin per rule instead of a single bloated paragraph.",
    highlights: [
      "Filters conversational chatter (“ok sounds good”, “thanks!”) and questions",
      "Drops harness-authored blocks (Stop hook feedback:, <system-reminder>, Codex wrappers)",
      "Re-indexes reactivated constraints deterministically after compaction",
    ],
  },
  {
    tier: "Tier 3 · Debug Ledger",
    title: "Test Trajectories & Edit-Thrash Warnings",
    tone: "sand",
    chipTone: "warning",
    tags: ["OSCILLATING", "PROGRESSING", "STALLED", "SINGLE_FAILURE", "RESOLVED", "[THRASH/REVERT]"],
    description:
      "Ordered assertion sequences across pytest, vitest, jest, go test, and generic shell commands — listing observed values oldest first alongside edit-revert loops so the agent skips failed approaches.",
    highlights: [
      "Tracks exact observed vs expected values (e.g. [400 -> 200 -> 400; expected 100])",
      "Detects hunk reverts and stalled edit loops on the same file while failures persist",
      "Separates active failures from resolved tests cleanly",
    ],
  },
  {
    tier: "Tier 4 · Workspace Context",
    title: "Modified Files Summary",
    tone: "mint",
    chipTone: "mint",
    tags: ["Project-relative", "Deduplicated"],
    description:
      "A deduplicated list of files edited, written, or deleted during the session, relativized against the detected project root so paths stay compact and unambiguous.",
    highlights: [
      "Relativizes paths consistently even when PreCompact fires from a subdirectory",
      "Deduplicates across Claude Code tool events and Codex FileChange / apply_patch records",
      "Shed gracefully under tight --max-chars budgets before any constraint is touched",
    ],
  },
];

export const CLAUDE_SETTINGS_JSON = `{
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
}`;

export const CODEX_COMMANDS = `# Snapshot a Codex CLI rollout (code-mode exec, event_msg, or legacy events)
compact-ledger snapshot --transcript ~/.codex/sessions/.../rollout-*.jsonl

# Render the strictly bounded ledger for handoff or resume
compact-ledger inject --session-id <session-id>

# Pin a critical constraint manually (--session-id optional inside Claude Code)
compact-ledger pin "Never push to main" --negative --tag git`;

export const BENCHMARK_KPIS: KpiMetric[] = [
  {
    id: "manual-pin",
    title: "Atomic Manual Pin",
    value: "0.42 ms",
    delta: "p95 0.56 ms",
    tone: "mint",
    subtitle: "In-process file-locked write + fsync",
  },
  {
    id: "render-snapshot",
    title: "Ledger Render",
    value: "0.01 ms",
    delta: "p95 0.01 ms",
    tone: "blue",
    subtitle: "6,000-char priority-ordered cap",
  },
  {
    id: "large-transcript",
    title: "162 KB Transcript",
    value: "11.68 ms",
    delta: "1,098 chars",
    tone: "blue",
    subtitle: "80 failing cycles → ~274 tokens",
  },
  {
    id: "eval-suite",
    title: "Seeded Eval Suite",
    value: "24 / 24",
    delta: "48.5 ms",
    tone: "mint",
    subtitle: "100.0% pass across 6 categories",
  },
];

export interface BenchmarkRow {
  benchmark: string;
  mode: "In-process" | "Cold CLI";
  median: string;
  p95: string;
  notes: string;
}

export const BENCHMARK_ROWS: BenchmarkRow[] = [
  {
    benchmark: "add_manual_pin (atomic write + fsync)",
    mode: "In-process",
    median: "0.42 ms",
    p95: "0.56 ms",
    notes: "File-locked atomic JSON replace",
  },
  {
    benchmark: "render_snapshot (6,000-char cap)",
    mode: "In-process",
    median: "0.01 ms",
    p95: "0.01 ms",
    notes: "Priority-ordered Markdown renderer",
  },
  {
    benchmark: "hook pre-compact (4.7 KB transcript)",
    mode: "In-process",
    median: "1.40 ms",
    p95: "1.73 ms",
    notes: "Full parse + pins + trajectories + save",
  },
  {
    benchmark: "hook pre-compact (162 KB transcript)",
    mode: "In-process",
    median: "11.68 ms",
    p95: "12.38 ms",
    notes: "80 failing edit/test cycles, O(N) scan",
  },
  {
    benchmark: "hook session-start (load + render)",
    mode: "In-process",
    median: "0.11 ms",
    p95: "0.12 ms",
    notes: "Disk load + bounded JSON output",
  },
  {
    benchmark: "compact-ledger pin",
    mode: "Cold CLI",
    median: "54.87 ms",
    p95: "59.40 ms",
    notes: "Includes Python interpreter startup",
  },
  {
    benchmark: "compact-ledger inject",
    mode: "Cold CLI",
    median: "52.41 ms",
    p95: "56.95 ms",
    notes: "Includes Python interpreter startup",
  },
  {
    benchmark: "compact-ledger hook pre-compact",
    mode: "Cold CLI",
    median: "53.55 ms",
    p95: "56.35 ms",
    notes: "4.7 KB transcript, includes startup",
  },
  {
    benchmark: "compact-ledger hook session-start",
    mode: "Cold CLI",
    median: "50.98 ms",
    p95: "52.58 ms",
    notes: "Includes Python interpreter startup",
  },
];

export interface EvalCategory {
  name: string;
  passed: number;
  total: number;
  coverage: string;
}

export const EVAL_CATEGORIES: EvalCategory[] = [
  {
    name: "constraints",
    passed: 4,
    total: 4,
    coverage: "Negative rules, Codex transcripts, manual pin priority, harness filtering",
  },
  {
    name: "overrides",
    passed: 7,
    total: 7,
    coverage: "Date corrections, 3-step chains, casual 'actually' guard, long-brief single-rule retirement",
  },
  {
    name: "trajectories",
    passed: 7,
    total: 7,
    coverage: "pytest, vitest/jest ANSI, go test, generic fallback, Codex code-mode exec",
  },
  {
    name: "thrash",
    passed: 2,
    total: 2,
    coverage: "Hunk revert & stalled loop detection, plus clean edit-then-pass negative guard",
  },
  {
    name: "budget",
    passed: 2,
    total: 2,
    coverage: "Strict 400 / 1,200 / 6,000 char caps and anti-starvation override capping",
  },
  {
    name: "hooks",
    passed: 2,
    total: 2,
    coverage: "PreCompact → SessionStart roundtrip, compact|resume gating, fail-open safety",
  },
];

export interface CliCommandRow {
  command: string;
  flags: string;
  description: string;
  exitBehavior: string;
}

export const CLI_COMMANDS: CliCommandRow[] = [
  {
    command: "init",
    flags: "[--write] [--settings-path <path>] [--command <cmd>]",
    description:
      "Print or atomically merge PreCompact & SessionStart hooks into .claude/settings.json (idempotent; refuses invalid JSON; warns if command is not on PATH).",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "doctor",
    flags: "[--settings-path <path>] [--store-dir <dir>] [--json]",
    description:
      "Verify compact-ledger on PATH, Python >=3.11, .claude/settings.json hooks, .claude-plugin/ health, store writability & .gitignore, and hook snapshot activity.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "pin",
    flags: '"<text>" [--session-id <id>] [--tag <tag>...] [--negative] [--json]',
    description:
      "Add a Tier 1 manual pin that survives across snapshots and tight character budgets.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "unpin",
    flags: '"<pin-id-or-text>" [--session-id <id>]',
    description: "Remove a manual pin by pin ID or verbatim text.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "snapshot",
    flags: "--transcript <path> [--session-id <id>] [--format auto|claude-code|codex|generic] [--json]",
    description:
      "Parse a JSONL transcript, preserve manual pins, compute overrides/trajectories/thrash, and save an atomic snapshot.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "inject",
    flags: "[--session-id <id>] [--max-chars 6000] [--format markdown|json]",
    description:
      "Render the stored snapshot strictly within --max-chars in priority order.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "status",
    flags: "[--session-id <id>] [--json]",
    description:
      "List stored sessions (marking the current env session with *), or inspect one session's counts and manual pins.",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "clear",
    flags: "[--session-id <id>]",
    description:
      "Delete the stored snapshot and any .corrupt* backups for a session (exits 0 even if absent).",
    exitBehavior: "0 / 1 / 2",
  },
  {
    command: "hook pre-compact",
    flags: "[--store-dir <dir>] [--strict]",
    description:
      "Claude Code PreCompact stdin hook handler — parses transcript and saves snapshot atomically.",
    exitBehavior: "0 (fail-open)",
  },
  {
    command: "hook session-start",
    flags: "[--store-dir <dir>] [--max-chars 6000] [--always] [--strict]",
    description:
      "Claude Code SessionStart stdin hook handler — emits hookSpecificOutput.additionalContext on compact/resume (or --always).",
    exitBehavior: "0 (fail-open)",
  },
  {
    command: "eval",
    flags: "[--format text|json] [--verbose]",
    description:
      "Run the packaged 24-case deterministic evaluation suite from any working directory.",
    exitBehavior: "0 / 1 / 2",
  },
];

export const HONEST_LIMITS = [
  {
    title: "Automatic hooks are Claude Code native",
    tone: "cool" as SurfaceTone,
    body: "Claude Code exposes PreCompact and SessionStart hooks directly. Codex CLI has no compaction hook to attach to yet, so Codex rollouts (including code-mode exec and apply_patch events) use compact-ledger snapshot, inject, and pin.",
  },
  {
    title: "Prose rule detection is heuristic",
    tone: "warm" as SurfaceTone,
    body: "Pattern-based extraction catches common directives and filters chatter, but casual phrasings can be missed and some task lines in a long brief can be labelled [CONSTRAINT]. For rules you cannot afford to lose, use compact-ledger pin.",
  },
  {
    title: "Session-scoped, not cross-session memory",
    tone: "neutral" as SurfaceTone,
    body: "Snapshots are keyed per session under .compact-ledger/sessions/<session_id>.json to protect live work across context compactions. It is intentionally not a global cross-session project memory store.",
  },
];

export const FAQ_ITEMS: AccordionItemData[] = [
  {
    question: "Why zero LLM calls in the compaction path?",
    answer:
      "Calling another LLM before or after compaction adds latency, cost, API key plumbing, and a second layer of lossy paraphrasing. compact-ledger runs deterministic O(N) Python stdlib parsing in ~1–12 ms in-process (~51–55 ms cold CLI) so rules remain verbatim and outputs are reproducible.",
  },
  {
    question: "Can a hook failure ever block or corrupt my Claude Code session?",
    answer:
      "No. Hook commands (hook pre-compact and hook session-start) fail open by design: on malformed stdin, bad flags, broken pipes, or unexpected errors they exit 0 without polluting stdout (unless you explicitly pass --strict). Snapshot writes are file-locked, atomic, and symlink-hardened, and corrupt files are quarantined to timestamped .corrupt-* backups.",
  },
  {
    question: "Where are snapshots stored, and could user messages leak into git?",
    answer:
      "One store per project lives at <project root>/.compact-ledger/sessions/<session_id>.json (overridable via --store-dir or COMPACT_LEDGER_DIR). The store automatically writes its own .gitignore so verbatim user messages stay local and untracked.",
  },
  {
    question: "How does --session-id work when an agent runs compact-ledger pin?",
    answer:
      "Inside Claude Code every shell command receives CLAUDE_CODE_SESSION_ID, so --session-id is optional. CLI commands default to COMPACT_LEDGER_SESSION_ID, then CLAUDE_CODE_SESSION_ID, and discover the same project-root store even when invoked from a nested subdirectory.",
  },
  {
    question: "How do I use compact-ledger with Codex CLI or other coding agents?",
    answer:
      "compact-ledger parses modern Codex code-mode exec rollouts (tools.exec_command, tools.shell_command, tools.apply_patch), event_msg item_completed records, legacy function_call/local_shell_call/apply_patch events, and generic JSONL transcripts. Run compact-ledger snapshot --transcript <path> followed by compact-ledger inject --session-id <id>, and optionally install the Agent Skill with npx skills add 1solomonwakhungu/compact-ledger.",
  },
];
