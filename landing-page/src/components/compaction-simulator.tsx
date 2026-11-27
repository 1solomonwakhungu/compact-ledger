"use client";

import * as React from "react";
import { SurfaceTile } from "@/components/tonal/surface-tile";
import { TonalChip } from "@/components/tonal/chip";
import { TonalSegmentedControl } from "@/components/tonal/segmented-control";
import { CopyButton } from "@/components/copy-button";
import {
  BILLING_SESSION_EVENTS,
  BUDGET_PRESETS,
  type BudgetCapId,
} from "@/lib/content";

type ViewMode = "split" | "before" | "ledger";

function renderLedgerLine(line: string, idx: number) {
  if (line.startsWith("## ")) {
    return (
      <div key={idx} className="font-semibold text-[var(--t-ink)]">
        {line}
      </div>
    );
  }
  if (line.startsWith("### ")) {
    return (
      <div key={idx} className="font-semibold text-[var(--t-blue)] pt-2">
        {line}
      </div>
    );
  }
  if (line.startsWith("- [MANUAL]") || line.startsWith("- [OVERRIDE]")) {
    return (
      <div key={idx} className="text-[var(--t-ink)]">
        <span className="font-semibold text-[var(--t-blue)]">
          {line.slice(0, line.indexOf("]") + 1)}
        </span>
        {line.slice(line.indexOf("]") + 1)}
      </div>
    );
  }
  if (line.startsWith("- [DO NOT]")) {
    return (
      <div key={idx} className="text-[var(--t-ink)]">
        <span className="font-semibold text-[var(--t-danger)]">
          - [DO NOT]
        </span>
        {line.slice("- [DO NOT]".length)}
      </div>
    );
  }
  if (line.startsWith("- [CONSTRAINT]")) {
    return (
      <div key={idx} className="text-[var(--t-ink-secondary)]">
        <span className="font-semibold text-[var(--t-ink)]">
          - [CONSTRAINT]
        </span>
        {line.slice("- [CONSTRAINT]".length)}
      </div>
    );
  }
  if (
    line.startsWith("- [OSCILLATING]") ||
    line.startsWith("- [THRASH/REVERT]")
  ) {
    const tagEnd = line.indexOf("]") + 1;
    return (
      <div key={idx} className="text-[var(--t-ink)]">
        <span className="font-semibold text-[var(--t-danger)]">
          {line.slice(0, tagEnd)}
        </span>
        {line.slice(tagEnd)}
      </div>
    );
  }
  if (
    line.startsWith("- [PROGRESSING]") ||
    line.startsWith("- [SINGLE_FAILURE]")
  ) {
    const tagEnd = line.indexOf("]") + 1;
    return (
      <div key={idx} className="text-[var(--t-ink)]">
        <span className="font-semibold text-[var(--t-warning)]">
          {line.slice(0, tagEnd)}
        </span>
        {line.slice(tagEnd)}
      </div>
    );
  }
  if (line.startsWith("- [RESOLVED]")) {
    return (
      <div key={idx} className="text-[var(--t-ink-secondary)]">
        <span className="font-semibold text-[var(--t-success)]">
          - [RESOLVED]
        </span>
        {line.slice("- [RESOLVED]".length)}
      </div>
    );
  }
  if (line.trim() === "") {
    return <div key={idx} className="h-2" aria-hidden="true" />;
  }
  return (
    <div key={idx} className="text-[var(--t-ink-muted)]">
      {line}
    </div>
  );
}

export function CompactionSimulator() {
  const [budgetCap, setBudgetCap] = React.useState<BudgetCapId>("6000");
  const [viewMode, setViewMode] = React.useState<ViewMode>("split");

  const preset = BUDGET_PRESETS[budgetCap];
  const actualChars = preset.markdown.length;
  const approxTokens = Math.round(actualChars / 4);
  const fillPercent = Math.min(
    100,
    Math.round((actualChars / preset.maxChars) * 100)
  );

  const tierStatusItems = [
    {
      label: "Tier 1 · Pins & Overrides",
      state: preset.tiersKept.tier1,
    },
    {
      label: "Tier 2 · Verbatim Rules",
      state: preset.tiersKept.tier2,
    },
    {
      label: "Tier 3 · Failures & Thrash",
      state: preset.tiersKept.tier3,
    },
    {
      label: "Tier 4 · Modified Files",
      state: preset.tiersKept.tier4,
    },
  ] as const;

  return (
    <section id="simulator" className="scroll-mt-24 flex flex-col gap-6">
      <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-4">
        <div className="max-w-2xl">
          <TonalChip tone="blue" className="mb-3">
            Interactive Compaction Simulator
          </TonalChip>
          <h2 className="t-headline text-[28px] sm:text-[36px] mb-2">
            What the agent sees after{" "}
            <code className="font-mono text-[0.88em] px-2 py-0.5 rounded-[10px] bg-[var(--t-surface-cool)] text-[var(--t-blue)]">
              /compact
            </code>
          </h2>
          <p className="t-body text-[15px] sm:text-[16px]">
            Inspect the real{" "}
            <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
              billing-refactor
            </code>{" "}
            session from the README. Switch budget caps to see how{" "}
            <code className="font-mono text-[13.5px] text-[var(--t-ink)]">
              len(output) &lt;= --max-chars
            </code>{" "}
            always protects Tier 1 pins and overrides first.
          </p>
        </div>

        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2.5">
          <TonalSegmentedControl<BudgetCapId>
            ariaLabel="Select character budget cap"
            size="sm"
            value={budgetCap}
            onValueChange={setBudgetCap}
            options={[
              { value: "6000", label: "Full · 6,000" },
              { value: "1200", label: "Tight · 1,200" },
              { value: "400", label: "Ultra-tight · 400" },
            ]}
          />
          <TonalSegmentedControl<ViewMode>
            ariaLabel="Toggle comparison view"
            size="sm"
            value={viewMode}
            onValueChange={setViewMode}
            options={[
              { value: "split", label: "Before + Ledger" },
              { value: "before", label: "Before" },
              { value: "ledger", label: "Injected Ledger" },
            ]}
          />
        </div>
      </div>

      {/* Budget Telemetry Strip */}
      <SurfaceTile
        tone="cool"
        bordered
        padding="sm"
        className="flex flex-col gap-3.5"
      >
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <span className="font-mono font-medium text-[var(--t-ink)]">
              {preset.command}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0 text-[12.5px] font-mono tabular-nums">
            <TonalChip tone={actualChars <= preset.maxChars ? "mint" : "danger"}>
              {actualChars.toLocaleString()} / {preset.maxChars.toLocaleString()}{" "}
              chars (~{approxTokens} tokens)
            </TonalChip>
          </div>
        </div>

        {/* Budget fill bar */}
        <div
          className="h-2 w-full rounded-full bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] overflow-hidden"
          role="progressbar"
          aria-label="Character budget usage"
          aria-valuenow={actualChars}
          aria-valuemin={0}
          aria-valuemax={preset.maxChars}
        >
          <div
            className="h-full rounded-full bg-[var(--t-blue)] transition-all duration-300 [transition-timing-function:var(--t-ease)]"
            style={{ width: `${fillPercent}%` }}
          />
        </div>

        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <p className="text-[13.5px] text-[var(--t-ink-secondary)]">
            {preset.summary}
          </p>
          <div className="flex flex-wrap items-center gap-1.5 shrink-0">
            {tierStatusItems.map((t) => (
              <TonalChip
                key={t.label}
                tone={
                  t.state === "full"
                    ? "mint"
                    : t.state === "partial"
                      ? "warning"
                      : "neutral"
                }
                className="text-[11px] px-2.5 py-0.5"
              >
                <span>{t.label.split(" · ")[0]}</span>
                <span className="opacity-75">
                  {t.state === "full"
                    ? "kept"
                    : t.state === "partial"
                      ? "capped"
                      : "trimmed"}
                </span>
              </TonalChip>
            ))}
          </div>
        </div>
      </SurfaceTile>

      {/* Main Comparison Panes */}
      <div
        className={`grid gap-5 ${
          viewMode === "split" ? "grid-cols-1 lg:grid-cols-12" : "grid-cols-1"
        }`}
      >
        {(viewMode === "split" || viewMode === "before") && (
          <SurfaceTile
            tone="warm"
            bordered
            padding="md"
            className={`${
              viewMode === "split" ? "lg:col-span-5" : ""
            } flex flex-col gap-4`}
          >
            <div className="flex items-center justify-between gap-2 pb-3 border-b border-[var(--t-border-subtle)]">
              <div>
                <h3 className="t-headline text-[18px]">
                  Before{" "}
                  <span className="font-mono text-[15px]">/compact</span>
                </h3>
                <p className="text-[12.5px] text-[var(--t-ink-muted)]">
                  Messy multi-turn transcript ·{" "}
                  <span className="font-mono">billing-refactor</span>
                </p>
              </div>
              <TonalChip tone="neutral">Raw events</TonalChip>
            </div>

            <ol className="flex flex-col gap-2.5">
              {BILLING_SESSION_EVENTS.map((ev) => (
                <li
                  key={ev.id}
                  className={`rounded-[16px] bg-[var(--t-elevated)] border border-[var(--t-border-subtle)] p-3.5 flex flex-col gap-1.5 ${
                    ev.disposition === "DROPPED" ||
                    ev.disposition === "SUPERSEDED"
                      ? "opacity-75"
                      : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11.5px] font-mono text-[var(--t-ink-muted)]">
                      {ev.turn} · {ev.actor}
                    </span>
                    <TonalChip
                      tone={ev.chipTone}
                      className="text-[10.5px] px-2 py-0.5 font-mono"
                    >
                      {ev.disposition}
                    </TonalChip>
                  </div>
                  <p
                    className={`text-[13px] leading-[1.5] text-[var(--t-ink)] ${
                      ev.disposition === "SUPERSEDED" ||
                      ev.disposition === "DROPPED"
                        ? "line-through decoration-[var(--t-ink-muted)]/60"
                        : ""
                    }`}
                  >
                    {ev.text}
                  </p>
                  <p className="text-[11.5px] text-[var(--t-ink-muted)]">
                    {ev.detail}
                  </p>
                </li>
              ))}
            </ol>
          </SurfaceTile>
        )}

        {(viewMode === "split" || viewMode === "ledger") && (
          <SurfaceTile
            tone="white"
            bordered
            padding="md"
            className={`${
              viewMode === "split" ? "lg:col-span-7" : ""
            } flex flex-col gap-4`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-[var(--t-border-subtle)]">
              <div>
                <h3 className="t-headline text-[18px]">
                  Injected Ledger ({preset.label})
                </h3>
                <p className="text-[12.5px] text-[var(--t-ink-muted)]">
                  Emitted into{" "}
                  <code className="font-mono text-[12px]">
                    hookSpecificOutput.additionalContext
                  </code>{" "}
                  on resume
                </p>
              </div>
              <CopyButton text={preset.markdown} label="Copy ledger" compact />
            </div>

            <div
              aria-live="polite"
              className="rounded-[16px] bg-[var(--t-surface)] border border-[var(--t-border-subtle)] p-4 sm:p-5 overflow-x-auto font-mono text-[12.5px] leading-[1.65]"
            >
              {preset.markdown
                .split("\n")
                .map((line, idx) => renderLedgerLine(line, idx))}
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 pt-1 text-[12px] text-[var(--t-ink-muted)]">
              <div className="rounded-[12px] bg-[var(--t-surface)] px-3 py-2 border border-[var(--t-border-subtle)]">
                <span className="font-medium text-[var(--t-ink)] block">
                  One pin per rule
                </span>
                July 31 retires only the June 30 line; sibling Turn 1 rules stay
                active.
              </div>
              <div className="rounded-[12px] bg-[var(--t-surface)] px-3 py-2 border border-[var(--t-border-subtle)]">
                <span className="font-medium text-[var(--t-ink)] block">
                  Oldest values first
                </span>
                <code className="font-mono">400 -&gt; 200 -&gt; 400</code> +{" "}
                <code className="font-mono">[THRASH/REVERT]</code> stops dead-end
                retries.
              </div>
              <div className="rounded-[12px] bg-[var(--t-surface)] px-3 py-2 border border-[var(--t-border-subtle)]">
                <span className="font-medium text-[var(--t-ink)] block">
                  Strict cap guarantee
                </span>
                Preamble drops first, then Tier 4 → Tier 3 → Tier 2. Tier 1
                always wins.
              </div>
            </div>
          </SurfaceTile>
        )}
      </div>
    </section>
  );
}
