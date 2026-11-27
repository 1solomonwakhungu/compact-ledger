"use client";

import * as React from "react";
import {
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  FileCode2,
  Lock,
  Moon,
  ShieldCheck,
  Sun,
  Terminal,
} from "lucide-react";
import { SurfaceTile } from "@/components/tonal/surface-tile";
import { TonalChip } from "@/components/tonal/chip";
import { ChevronLink } from "@/components/tonal/chevron-link";
import { TonalSegmentedControl } from "@/components/tonal/segmented-control";
import { TonalAccordion } from "@/components/tonal/accordion";
import { KpiGrid } from "@/components/tonal/kpi-grid";
import { CopyButton } from "@/components/copy-button";
import { CompactionSimulator } from "@/components/compaction-simulator";
import { BrandMark } from "@/components/brand-mark";
import {
  BENCHMARK_KPIS,
  BENCHMARK_ROWS,
  CLAUDE_SETTINGS_JSON,
  CLI_COMMANDS,
  CODEX_COMMANDS,
  EVAL_CATEGORIES,
  FAQ_ITEMS,
  HONEST_LIMITS,
  INSTALL_METHODS,
  PRIORITY_TIERS,
  SITE_LINKS,
  type InstallMethodId,
} from "@/lib/content";

const NAV_LINKS = [
  { href: "#tiers", label: "Tiers" },
  { href: "#simulator", label: "Simulator" },
  { href: "#hooks", label: "Hooks" },
  { href: "#benchmarks", label: "Benchmarks" },
  { href: "#cli", label: "CLI" },
  { href: "#faq", label: "FAQ" },
] as const;

export function LandingPage() {
  const [theme, setTheme] = React.useState<"light" | "dark">("light");
  const [installMethod, setInstallMethod] =
    React.useState<InstallMethodId>("plugin");

  React.useEffect(() => {
    try {
      const ds = document.documentElement.dataset;
      if (ds.tonalTheme === "dark" || ds.tonalTheme === "light") {
        setTheme(ds.tonalTheme);
      } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
        document.documentElement.dataset.tonalTheme = "dark";
        setTheme("dark");
      }
    } catch {
      // Ignore storage / matchMedia errors
    }
  }, []);

  const toggleTheme = React.useCallback(() => {
    setTheme((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      document.documentElement.dataset.tonalTheme = next;
      try {
        window.localStorage.setItem("compact-ledger-theme", next);
      } catch {
        // Ignore storage errors
      }
      return next;
    });
  }, []);

  const activeInstall =
    INSTALL_METHODS.find((m) => m.id === installMethod) ?? INSTALL_METHODS[0];

  return (
    <div className="min-h-screen bg-[var(--t-bg)] text-[var(--t-ink)] flex flex-col transition-colors duration-200">
      {/* Sticky Header */}
      <header className="sticky top-0 z-40 bg-[var(--t-bg)]/90 backdrop-blur-md border-b border-[var(--t-border-subtle)] transition-colors duration-200">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 h-16 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <a
              href="#"
              className="flex items-center gap-2.5 text-[16px] sm:text-[17px] font-medium tracking-[-0.02em] text-[var(--t-ink)] shrink-0 rounded-[8px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <BrandMark className="h-7 w-7 shrink-0" />
              <span>compact-ledger</span>
            </a>

            <span
              aria-hidden="true"
              className="hidden sm:inline text-[var(--t-border)]"
            >
              /
            </span>

            <a
              href={SITE_LINKS.harlo}
              className="hidden sm:inline-flex items-center gap-1 text-[13px] text-[var(--t-ink-muted)] hover:text-[var(--t-ink)] transition-colors rounded-[6px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <span>harlo.dev</span>
              <ArrowUpRight className="h-3.5 w-3.5 opacity-60" />
            </a>
          </div>

          <nav
            aria-label="Primary sections"
            className="hidden md:flex items-center gap-1"
          >
            {NAV_LINKS.map((item) => (
              <a
                key={item.href}
                href={item.href}
                className="px-3 py-1.5 rounded-full text-[13.5px] font-medium text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface-muted)] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
              >
                {item.label}
              </a>
            ))}
          </nav>

          <div className="flex items-center gap-2 shrink-0">
            <a
              href={SITE_LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-full bg-[var(--t-surface)] hover:bg-[var(--t-surface-muted)] border border-[var(--t-border-subtle)] px-3.5 h-8 sm:h-9 text-[12.5px] font-medium text-[var(--t-ink)] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <span>GitHub</span>
              <ArrowUpRight className="h-3.5 w-3.5 text-[var(--t-ink-muted)]" />
            </a>

            <button
              type="button"
              onClick={toggleTheme}
              aria-label={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              title={
                theme === "dark"
                  ? "Switch to light mode"
                  : "Switch to dark mode"
              }
              className="inline-flex h-8 w-8 sm:h-9 sm:w-9 shrink-0 items-center justify-center rounded-full bg-[var(--t-surface)] border border-[var(--t-border-subtle)] text-[var(--t-ink-secondary)] hover:text-[var(--t-ink)] hover:bg-[var(--t-surface-muted)] cursor-pointer transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              {theme === "dark" ? (
                <Sun className="h-4 w-4" />
              ) : (
                <Moon className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 mx-auto w-full max-w-6xl px-5 sm:px-8 pt-12 sm:pt-16 pb-20 flex flex-col gap-20 sm:gap-24">
        {/* Hero Section */}
        <section className="flex flex-col items-center text-center max-w-4xl mx-auto w-full">
          <TonalChip tone="blue" className="mb-5">
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 rounded-full bg-[var(--t-success)]"
            />
            v0.1.1 · Zero-LLM · Python 3.11+ stdlib
          </TonalChip>

          <h1 className="t-display text-[38px] sm:text-[54px] lg:text-[62px] max-w-3xl mb-5">
            Keep verbatim rules alive across context compaction.
          </h1>

          <p className="t-body text-[16px] sm:text-[18.5px] text-[var(--t-ink-muted)] max-w-2xl mb-8">
            Deterministic{" "}
            <code className="font-mono text-[0.9em] text-[var(--t-ink)]">
              PreCompact
            </code>{" "}
            +{" "}
            <code className="font-mono text-[0.9em] text-[var(--t-ink)]">
              SessionStart
            </code>{" "}
            memory injection for AI coding agents. Preserve manual pins,
            superseding corrections, and failing test trajectories — with zero
            LLM calls and zero runtime dependencies.
          </p>

          {/* Primary & Secondary CTAs */}
          <div className="flex flex-wrap items-center justify-center gap-3 mb-10">
            <a
              href="#hooks"
              className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] select-none px-7 py-3.5 text-[15px] rounded-[var(--t-radius-btn)] bg-[var(--t-cta-bg)] text-[var(--t-cta-fg,#ffffff)] hover:bg-[var(--t-cta-hover)] transition-all duration-200 [transition-timing-function:var(--t-ease)] active:scale-[0.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <span>Get started</span>
              <ArrowRight className="h-4 w-4" />
            </a>
            <a
              href={SITE_LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 font-medium leading-none tracking-[-0.01em] select-none px-7 py-3.5 text-[15px] rounded-[var(--t-radius-btn)] bg-transparent text-[var(--t-ink)] border border-[var(--t-ink-muted)] hover:bg-[var(--t-hover-overlay,rgba(32,33,36,0.04))] transition-all duration-200 [transition-timing-function:var(--t-ease)] active:scale-[0.98] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              <span>View on GitHub</span>
              <ArrowUpRight className="h-4 w-4" />
            </a>
          </div>

          {/* Interactive Install Switcher */}
          <SurfaceTile
            tone="cool"
            bordered
            padding="md"
            className="w-full max-w-3xl text-left flex flex-col gap-4"
          >
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <TonalSegmentedControl<InstallMethodId>
                ariaLabel="Choose installation method"
                size="sm"
                value={installMethod}
                onValueChange={setInstallMethod}
                options={INSTALL_METHODS.map((m) => ({
                  value: m.id,
                  label: m.label,
                }))}
              />
              <CopyButton
                text={activeInstall.commands}
                label="Copy commands"
                compact
              />
            </div>

            <pre className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] px-4 py-3.5 overflow-x-auto font-mono text-[13px] leading-[1.65] text-[var(--t-ink)]">
              <code>{activeInstall.commands}</code>
            </pre>

            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-[13px]">
              <p className="text-[var(--t-ink-secondary)]">
                {activeInstall.summary}
              </p>
            </div>
            <p className="text-[12px] text-[var(--t-ink-muted)] pt-2 border-t border-[var(--t-border-subtle)]">
              {activeInstall.note}
            </p>
          </SurfaceTile>

          {/* Grounded Proof Strip */}
          <div className="mt-7 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-[13px] text-[var(--t-ink-muted)]">
            <span className="inline-flex items-center gap-1.5">
              <CheckCircle2 className="h-3.5 w-3.5 text-[var(--t-success)]" />
              O(N) linear scan · 11.68 ms on 162 KB transcript
            </span>
            <span className="inline-flex items-center gap-1.5">
              <ShieldCheck className="h-3.5 w-3.5 text-[var(--t-blue)]" />
              Fail-open hooks (exit 0 on error)
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Lock className="h-3.5 w-3.5 text-[var(--t-ink-secondary)]" />
              Atomic file-locked local JSON store
            </span>
          </div>
        </section>

        {/* 4-Tier Priority Architecture Bento */}
        <section id="tiers" className="scroll-mt-24 flex flex-col gap-6">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
            <div className="max-w-2xl">
              <TonalChip tone="warm" className="mb-3">
                4-Tier Priority Architecture
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                Budgeted by priority, never by lossy summary.
              </h2>
              <p className="t-body text-[15px] sm:text-[16px]">
                Every snapshot renders strictly within{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  --max-chars
                </code>{" "}
                (default 6,000). Under pressure, lower tiers shed first so your
                standing rules and overrides always survive.
              </p>
            </div>

            <div className="inline-flex items-center gap-1.5 text-[12.5px] font-mono text-[var(--t-ink-muted)] bg-[var(--t-surface)] border border-[var(--t-border-subtle)] rounded-full px-4 py-2 self-start md:self-auto">
              <span className="text-[var(--t-blue)] font-semibold">Tier 1</span>
              <span>&gt;</span>
              <span className="text-[var(--t-ink)]">Tier 2</span>
              <span>&gt;</span>
              <span className="text-[var(--t-warning)]">Tier 3</span>
              <span>&gt;</span>
              <span className="text-[var(--t-success)]">Tier 4</span>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {PRIORITY_TIERS.map((tier) => (
              <SurfaceTile
                key={tier.tier}
                tone={tier.tone}
                bordered
                padding="md"
                className="flex flex-col justify-between gap-5"
              >
                <div className="flex flex-col gap-3.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <TonalChip tone={tier.chipTone}>{tier.tier}</TonalChip>
                  </div>

                  <div>
                    <h3 className="t-headline text-[22px] mb-2">
                      {tier.title}
                    </h3>
                    <p className="t-body text-[14.5px]">{tier.description}</p>
                  </div>

                  <div className="flex flex-wrap gap-1.5">
                    {tier.tags.map((tag) => (
                      <span
                        key={tag}
                        className="rounded-full bg-[var(--t-elevated)]/85 border border-[var(--t-border-subtle)] px-2.5 py-0.5 text-[11.5px] font-mono text-[var(--t-ink-secondary)]"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>

                <ul className="flex flex-col gap-2 pt-3 border-t border-[var(--t-border-subtle)]/80 text-[13.5px] text-[var(--t-ink-secondary)]">
                  {tier.highlights.map((item) => (
                    <li key={item} className="flex items-start gap-2">
                      <span
                        aria-hidden="true"
                        className="mt-2 h-1.5 w-1.5 rounded-full bg-[var(--t-ink-muted)] shrink-0"
                      />
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              </SurfaceTile>
            ))}
          </div>
        </section>

        {/* Interactive Compaction & Budget Simulator */}
        <CompactionSimulator />

        {/* Claude Code Hooks & Codex CLI Integration */}
        <section id="hooks" className="scroll-mt-24 flex flex-col gap-6">
          <div className="max-w-2xl">
            <TonalChip tone="mint" className="mb-3">
              Hooks &amp; Agent Integration
            </TonalChip>
            <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
              Native Claude Code hooks. Full Codex CLI transcript parsing.
            </h2>
            <p className="t-body text-[15px] sm:text-[16px]">
              Run{" "}
              <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                compact-ledger init --write
              </code>{" "}
              to merge hooks idempotently into{" "}
              <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                .claude/settings.json
              </code>{" "}
              without touching existing settings.
            </p>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
            {/* .claude/settings.json */}
            <SurfaceTile
              tone="white"
              bordered
              padding="md"
              className="lg:col-span-7 flex flex-col gap-4"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2.5">
                  <div className="h-9 w-9 rounded-[12px] bg-[var(--t-surface-sky)] text-[var(--t-blue)] grid place-items-center shrink-0">
                    <FileCode2 className="h-4 w-4" />
                  </div>
                  <div>
                    <h3 className="t-headline text-[17px]">
                      .claude/settings.json
                    </h3>
                    <p className="text-[12.5px] text-[var(--t-ink-muted)]">
                      Generated by{" "}
                      <code className="font-mono">
                        compact-ledger init --write
                      </code>
                    </p>
                  </div>
                </div>
                <CopyButton
                  text={CLAUDE_SETTINGS_JSON}
                  label="Copy JSON"
                  compact
                />
              </div>

              <pre className="rounded-[16px] bg-[var(--t-surface)] border border-[var(--t-border-subtle)] p-4 overflow-x-auto font-mono text-[12.5px] leading-[1.6] text-[var(--t-ink)]">
                <code>{CLAUDE_SETTINGS_JSON}</code>
              </pre>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-[13px]">
                <div className="rounded-[14px] bg-[var(--t-surface-cool)] p-3.5">
                  <div className="font-mono text-[12px] font-semibold text-[var(--t-blue)] mb-1">
                    PreCompact
                  </div>
                  <p className="text-[var(--t-ink-secondary)] leading-[1.5]">
                    Reads stdin JSON (
                    <code className="font-mono text-[11.5px]">session_id</code>,{" "}
                    <code className="font-mono text-[11.5px]">
                      transcript_path
                    </code>
                    , <code className="font-mono text-[11.5px]">cwd</code>,{" "}
                    <code className="font-mono text-[11.5px]">trigger</code>)
                    and atomically writes{" "}
                    <code className="font-mono text-[11.5px]">
                      .compact-ledger/sessions/&lt;id&gt;.json
                    </code>
                    .
                  </p>
                </div>
                <div className="rounded-[14px] bg-[var(--t-surface-mint)] p-3.5">
                  <div className="font-mono text-[12px] font-semibold text-[var(--t-success)] mb-1">
                    SessionStart (compact|resume)
                  </div>
                  <p className="text-[var(--t-ink-secondary)] leading-[1.5]">
                    Only fires when{" "}
                    <code className="font-mono text-[11.5px]">source</code> is{" "}
                    <code className="font-mono text-[11.5px]">
                      &quot;compact&quot;
                    </code>{" "}
                    or{" "}
                    <code className="font-mono text-[11.5px]">
                      &quot;resume&quot;
                    </code>{" "}
                    (or <code className="font-mono text-[11.5px]">--always</code>
                    ), emitting{" "}
                    <code className="font-mono text-[11.5px]">
                      hookSpecificOutput.additionalContext
                    </code>
                    .
                  </p>
                </div>
              </div>
            </SurfaceTile>

            {/* Storage Safety + Codex CLI */}
            <div className="lg:col-span-5 flex flex-col gap-5">
              <SurfaceTile
                tone="neutral"
                bordered
                padding="md"
                className="flex flex-col gap-3.5"
              >
                <div className="flex items-center gap-2.5">
                  <div className="h-9 w-9 rounded-[12px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] text-[var(--t-ink)] grid place-items-center shrink-0">
                    <ShieldCheck className="h-4 w-4 text-[var(--t-success)]" />
                  </div>
                  <h3 className="t-headline text-[18px]">
                    Storage &amp; Hook Safety
                  </h3>
                </div>

                <ul className="flex flex-col gap-2.5 text-[13.5px] text-[var(--t-ink-secondary)]">
                  <li>
                    <strong className="font-medium text-[var(--t-ink)]">
                      Project-root store:
                    </strong>{" "}
                    Saved at{" "}
                    <code className="font-mono text-[12px] text-[var(--t-ink)]">
                      .compact-ledger/sessions/&lt;session_id&gt;.json
                    </code>{" "}
                    with automatic{" "}
                    <code className="font-mono text-[12px]">.gitignore</code>{" "}
                    creation. Override via{" "}
                    <code className="font-mono text-[12px]">--store-dir</code>{" "}
                    or{" "}
                    <code className="font-mono text-[12px]">
                      COMPACT_LEDGER_DIR
                    </code>
                    .
                  </li>
                  <li>
                    <strong className="font-medium text-[var(--t-ink)]">
                      Atomic &amp; hardened:
                    </strong>{" "}
                    Per-session file locking, atomic JSON replace +{" "}
                    <code className="font-mono text-[12px]">fsync</code>,
                    symlink violation guards, and timestamped{" "}
                    <code className="font-mono text-[12px]">.corrupt-*</code>{" "}
                    quarantine.
                  </li>
                  <li>
                    <strong className="font-medium text-[var(--t-ink)]">
                      Fail-open hooks:
                    </strong>{" "}
                    Hooks exit{" "}
                    <code className="font-mono text-[12px]">0</code> with empty
                    stdout on malformed input or internal errors unless{" "}
                    <code className="font-mono text-[12px]">--strict</code> is
                    passed.
                  </li>
                </ul>
              </SurfaceTile>

              <SurfaceTile
                tone="warm"
                bordered
                padding="md"
                className="flex flex-col gap-3.5"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    <div className="h-9 w-9 rounded-[12px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] text-[var(--t-ink)] grid place-items-center shrink-0">
                      <Terminal className="h-4 w-4" />
                    </div>
                    <div>
                      <h3 className="t-headline text-[18px]">
                        Codex CLI &amp; Generic JSONL
                      </h3>
                    </div>
                  </div>
                  <CopyButton text={CODEX_COMMANDS} label="Copy" compact />
                </div>

                <p className="text-[13.5px] text-[var(--t-ink-secondary)]">
                  Parses Codex code-mode{" "}
                  <code className="font-mono text-[12px]">exec</code> (
                  <code className="font-mono text-[12px]">
                    tools.exec_command
                  </code>
                  ,{" "}
                  <code className="font-mono text-[12px]">
                    tools.shell_command
                  </code>
                  ,{" "}
                  <code className="font-mono text-[12px]">
                    tools.apply_patch
                  </code>
                  ),{" "}
                  <code className="font-mono text-[12px]">
                    event_msg item_completed
                  </code>
                  , legacy shell calls, and generic JSONL transcripts.
                </p>

                <pre className="rounded-[14px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-3.5 overflow-x-auto font-mono text-[12px] leading-[1.6] text-[var(--t-ink)]">
                  <code>{CODEX_COMMANDS}</code>
                </pre>

                <div className="pt-1">
                  <ChevronLink
                    href={SITE_LINKS.skillDoc}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Read the bundled Agent Skill (SKILL.md)
                  </ChevronLink>
                </div>
              </SurfaceTile>
            </div>
          </div>
        </section>

        {/* Benchmarks & 24-Case Eval Suite */}
        <section id="benchmarks" className="scroll-mt-24 flex flex-col gap-6">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
            <div className="max-w-2xl">
              <TonalChip tone="blue" className="mb-3">
                Measured Performance &amp; Verification
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                Sub-millisecond render. 24/24 seeded evals.
              </h2>
              <p className="t-body text-[15px] sm:text-[16px]">
                Measured locally with{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  uv run python evals/bench.py
                </code>{" "}
                on macOS (
                <code className="font-mono text-[13px]">Darwin arm64</code>,
                Python <code className="font-mono text-[13px]">3.13.14</code>).
              </p>
            </div>

            <CopyButton
              text="uv run python evals/bench.py"
              label="Copy bench command"
              compact
            />
          </div>

          <KpiGrid items={BENCHMARK_KPIS} columns={4} />

          {/* Footprint Strip */}
          <SurfaceTile
            tone="sky"
            bordered
            padding="sm"
            className="grid grid-cols-1 md:grid-cols-3 gap-4"
          >
            <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4">
              <div className="text-[12px] font-medium text-[var(--t-ink-muted)] mb-1">
                Realistic session footprint
              </div>
              <div className="text-[15px] font-medium text-[var(--t-ink)] font-mono">
                4,719 B → 1,146 chars
              </div>
              <div className="text-[12.5px] text-[var(--t-ink-secondary)] mt-1">
                4,939 B snapshot JSON → ~286 tokens injected Markdown
              </div>
            </div>
            <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4">
              <div className="text-[12px] font-medium text-[var(--t-ink-muted)] mb-1">
                Heavy debug session (80 cycles)
              </div>
              <div className="text-[15px] font-medium text-[var(--t-ink)] font-mono">
                161,841 B → 1,098 chars
              </div>
              <div className="text-[12.5px] text-[var(--t-ink-secondary)] mt-1">
                162 KB transcript compresses to ~274 tokens in 11.68 ms
              </div>
            </div>
            <div className="rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-4">
              <div className="text-[12px] font-medium text-[var(--t-ink-muted)] mb-1">
                Hard budget ceiling
              </div>
              <div className="text-[15px] font-medium text-[var(--t-ink)] font-mono">
                --max-chars 400 → 400 chars
              </div>
              <div className="text-[12.5px] text-[var(--t-ink-secondary)] mt-1">
                Bench fixture at --max-chars 1200 emits 1,146 chars (vs 1,161
                chars on the README simulator above)
              </div>
            </div>
          </SurfaceTile>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
            {/* Full Benchmark Table */}
            <SurfaceTile
              tone="white"
              bordered
              padding="md"
              className="lg:col-span-7 flex flex-col gap-4"
            >
              <div className="flex items-center justify-between gap-2">
                <h3 className="t-headline text-[19px]">
                  In-Process &amp; Cold CLI Latencies
                </h3>
                <TonalChip tone="neutral">Darwin arm64 · Py 3.13.14</TonalChip>
              </div>

              <div className="overflow-x-auto -mx-2 px-2">
                <table className="w-full text-left border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-[var(--t-border-subtle)] text-[11.5px] uppercase tracking-wider text-[var(--t-ink-muted)]">
                      <th className="py-2.5 pr-3 font-medium">Benchmark</th>
                      <th className="py-2.5 px-2 font-medium text-right">
                        Median
                      </th>
                      <th className="py-2.5 px-2 font-medium text-right">
                        p95
                      </th>
                      <th className="py-2.5 pl-3 font-medium hidden sm:table-cell">
                        Notes
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--t-border-subtle)]">
                    {BENCHMARK_ROWS.map((row) => (
                      <tr key={`${row.mode}-${row.benchmark}`}>
                        <td className="py-2.5 pr-3 align-top">
                          <div className="font-mono text-[12.5px] text-[var(--t-ink)]">
                            {row.benchmark}
                          </div>
                          <span className="inline-block mt-0.5 text-[11px] text-[var(--t-ink-muted)]">
                            {row.mode}
                          </span>
                        </td>
                        <td className="py-2.5 px-2 text-right font-mono tabular-nums font-medium text-[var(--t-ink)] whitespace-nowrap align-top">
                          {row.median}
                        </td>
                        <td className="py-2.5 px-2 text-right font-mono tabular-nums text-[var(--t-ink-muted)] whitespace-nowrap align-top">
                          {row.p95}
                        </td>
                        <td className="py-2.5 pl-3 text-[var(--t-ink-secondary)] hidden sm:table-cell align-top">
                          {row.notes}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </SurfaceTile>

            {/* 24-Case Eval Suite Breakdown */}
            <SurfaceTile
              tone="mint"
              bordered
              padding="md"
              className="lg:col-span-5 flex flex-col justify-between gap-4"
            >
              <div className="flex flex-col gap-4">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <h3 className="t-headline text-[19px]">
                      24-Case Eval Suite
                    </h3>
                    <p className="text-[12.5px] text-[var(--t-ink-muted)]">
                      24/24 passed (100.0%) in 48.5 ms
                    </p>
                  </div>
                  <TonalChip tone="mint">100.0%</TonalChip>
                </div>

                <div className="flex flex-col gap-2.5">
                  {EVAL_CATEGORIES.map((cat) => (
                    <div
                      key={cat.name}
                      className="rounded-[14px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-3 flex flex-col gap-1.5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[13px] font-medium text-[var(--t-ink)]">
                          {cat.name}
                        </span>
                        <span className="font-mono text-[12px] font-semibold text-[var(--t-success)] tabular-nums">
                          {cat.passed}/{cat.total} passed
                        </span>
                      </div>
                      <div className="h-1.5 w-full rounded-full bg-[var(--t-surface-muted)] overflow-hidden">
                        <div className="h-full w-full rounded-full bg-[var(--t-success)]" />
                      </div>
                      <p className="text-[12px] text-[var(--t-ink-muted)] leading-[1.45]">
                        {cat.coverage}
                      </p>
                    </div>
                  ))}
                </div>
              </div>

              <div className="flex items-center justify-between gap-2 pt-2 border-t border-[var(--t-border-subtle)]/80">
                <code className="font-mono text-[12px] text-[var(--t-ink-secondary)]">
                  uv run compact-ledger eval
                </code>
                <ChevronLink
                  href={SITE_LINKS.evalsDoc}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Eval docs
                </ChevronLink>
              </div>
            </SurfaceTile>
          </div>
        </section>

        {/* Complete CLI Reference */}
        <section id="cli" className="scroll-mt-24 flex flex-col gap-6">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
            <div className="max-w-2xl">
              <TonalChip tone="neutral" className="mb-3">
                CLI Reference · 11 Commands
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                Every command, flag, and exit code.
              </h2>
              <p className="t-body text-[15px] sm:text-[16px]">
                All session commands accept{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  --store-dir
                </code>{" "}
                (or{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  COMPACT_LEDGER_DIR
                </code>
                ).{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  --session-id
                </code>{" "}
                defaults to{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  COMPACT_LEDGER_SESSION_ID
                </code>
                , then{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  CLAUDE_CODE_SESSION_ID
                </code>
                .
              </p>
            </div>
          </div>

          <SurfaceTile tone="white" bordered padding="none" className="overflow-hidden">
            {/* Mobile stacked cards (prevents mid-flag wrapping on narrow phones) */}
            <div className="md:hidden divide-y divide-[var(--t-border-subtle)]">
              {CLI_COMMANDS.map((row) => (
                <div key={row.command} className="p-5 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="font-mono font-semibold text-[13px] text-[var(--t-blue)]">
                      compact-ledger {row.command}
                    </div>
                    <span className="inline-flex rounded-full bg-[var(--t-surface-muted)] px-2.5 py-0.5 font-mono text-[11px] text-[var(--t-ink-secondary)] whitespace-nowrap shrink-0">
                      {row.exitBehavior}
                    </span>
                  </div>
                  <div className="rounded-[10px] bg-[var(--t-surface)] border border-[var(--t-border-subtle)] px-3 py-1.5 overflow-x-auto">
                    <code className="font-mono text-[11.5px] text-[var(--t-ink-muted)] whitespace-nowrap">
                      {row.flags}
                    </code>
                  </div>
                  <p className="text-[13px] text-[var(--t-ink-secondary)] leading-[1.55]">
                    {row.description}
                  </p>
                </div>
              ))}
            </div>

            {/* Desktop table */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full text-left border-collapse text-[13.5px]">
                <thead>
                  <tr className="bg-[var(--t-surface)] border-b border-[var(--t-border-subtle)] text-[11.5px] uppercase tracking-wider text-[var(--t-ink-muted)]">
                    <th className="py-3.5 pl-6 pr-4 font-medium">Command</th>
                    <th className="py-3.5 px-4 font-medium">Description</th>
                    <th className="py-3.5 pl-4 pr-6 font-medium text-right whitespace-nowrap">
                      Exit codes
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--t-border-subtle)]">
                  {CLI_COMMANDS.map((row) => (
                    <tr
                      key={row.command}
                      className="hover:bg-[var(--t-surface)]/70 transition-colors"
                    >
                      <td className="py-3.5 pl-6 pr-4 align-top">
                        <div className="font-mono font-semibold text-[13px] text-[var(--t-blue)] whitespace-nowrap">
                          compact-ledger {row.command}
                        </div>
                        <div className="font-mono text-[11.5px] text-[var(--t-ink-muted)] mt-0.5 max-w-sm">
                          {row.flags}
                        </div>
                      </td>
                      <td className="py-3.5 px-4 align-top text-[var(--t-ink-secondary)] leading-[1.55]">
                        {row.description}
                      </td>
                      <td className="py-3.5 pl-4 pr-6 align-top text-right whitespace-nowrap">
                        <span className="inline-flex rounded-full bg-[var(--t-surface-muted)] px-2.5 py-0.5 font-mono text-[11.5px] text-[var(--t-ink-secondary)]">
                          {row.exitBehavior}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="bg-[var(--t-surface)] border-t border-[var(--t-border-subtle)] px-6 py-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-[12.5px] text-[var(--t-ink-secondary)]">
              <span>
                <strong className="font-mono text-[var(--t-success)]">0</strong>{" "}
                ok (including <code className="font-mono">clear</code> on a
                missing session)
              </span>
              <span>
                <strong className="font-mono text-[var(--t-danger)]">1</strong>{" "}
                not found, corrupt store/settings, symlink violation, or runtime
                error
              </span>
              <span>
                <strong className="font-mono text-[var(--t-warning)]">2</strong>{" "}
                usage error (invalid flags or missing{" "}
                <code className="font-mono">--session-id</code>)
              </span>
              <span>
                <strong className="font-medium text-[var(--t-ink)]">
                  Hooks:
                </strong>{" "}
                always exit <code className="font-mono">0</code> unless{" "}
                <code className="font-mono">--strict</code> is passed
              </span>
            </div>
          </SurfaceTile>
        </section>

        {/* Honest Limits & FAQ */}
        <section id="faq" className="scroll-mt-24 flex flex-col gap-8">
          <div className="flex flex-col gap-5">
            <div className="max-w-2xl">
              <TonalChip tone="warning" className="mb-3">
                Honest Limits &amp; FAQ
              </TonalChip>
              <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
                Clear boundaries, zero hand-waving.
              </h2>
              <p className="t-body text-[15px] sm:text-[16px]">
                Know exactly where deterministic extraction shines and when to
                reach for{" "}
                <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
                  compact-ledger pin
                </code>
                .
              </p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {HONEST_LIMITS.map((limit) => (
                <SurfaceTile
                  key={limit.title}
                  tone={limit.tone}
                  bordered
                  padding="sm"
                  className="flex flex-col gap-2"
                >
                  <h3 className="t-headline text-[16.5px]">{limit.title}</h3>
                  <p className="t-body text-[13.5px]">{limit.body}</p>
                </SurfaceTile>
              ))}
            </div>
          </div>

          <SurfaceTile tone="white" bordered padding="md">
            <h3 className="t-headline text-[20px] mb-2">
              Frequently Asked Questions
            </h3>
            <TonalAccordion items={FAQ_ITEMS} defaultOpenIndex={0} />
          </SurfaceTile>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-[var(--t-border-subtle)] mt-auto">
        <div className="mx-auto max-w-6xl px-5 sm:px-8 py-9 flex flex-col sm:flex-row items-center justify-between gap-4 text-[13px] text-[var(--t-ink-muted)]">
          <div className="flex items-center gap-2.5">
            <BrandMark />
            <span className="font-medium text-[var(--t-ink)]">
              compact-ledger
            </span>
            <span>· MIT License</span>
          </div>

          <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2">
            <a
              href={SITE_LINKS.github}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors rounded-[4px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              GitHub
            </a>
            <a
              href={SITE_LINKS.pypi}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-[var(--t-ink)] transition-colors rounded-[4px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              PyPI
            </a>
            <a
              href={SITE_LINKS.harlo}
              className="hover:text-[var(--t-ink)] transition-colors rounded-[4px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
            >
              harlo.dev
            </a>
            <span>
              Built with{" "}
              <a
                href={SITE_LINKS.tonal}
                className="text-[var(--t-blue)] hover:underline underline-offset-4 rounded-[4px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--t-blue)]"
              >
                Tonal
              </a>
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
