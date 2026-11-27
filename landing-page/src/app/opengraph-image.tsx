import { ImageResponse } from "next/og";

export const alt =
  "compact-ledger — Deterministic PreCompact & SessionStart Memory for AI Coding Agents";
export const size = {
  width: 1200,
  height: 630,
};
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: "#FFFFFF",
          padding: "56px 64px",
          fontFamily: "sans-serif",
          color: "#202124",
        }}
      >
        {/* Header Row */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: 12,
                background: "#202124",
                display: "flex",
                flexDirection: "column",
                justifyContent: "center",
                gap: 4,
                padding: "0 9px",
              }}
            >
              <div
                style={{
                  width: 22,
                  height: 4,
                  borderRadius: 2,
                  background: "#8AB4F8",
                }}
              />
              <div
                style={{
                  width: 16,
                  height: 3.5,
                  borderRadius: 2,
                  background: "#FFFFFF",
                }}
              />
              <div
                style={{
                  width: 11,
                  height: 3.5,
                  borderRadius: 2,
                  background: "#9AA0A6",
                }}
              />
            </div>
            <span
              style={{
                fontSize: 30,
                fontWeight: 600,
                letterSpacing: "-0.02em",
              }}
            >
              compact-ledger
            </span>
          </div>

          <div
            style={{
              background: "#E8F0FE",
              color: "#174EA6",
              fontSize: 19,
              fontWeight: 600,
              padding: "10px 22px",
              borderRadius: 9999,
            }}
          >
            v0.1.1 · Zero-LLM · Python 3.11+ stdlib
          </div>
        </div>

        {/* Main Title */}
        <div style={{ display: "flex", flexDirection: "column", gap: "18px" }}>
          <div
            style={{
              fontSize: 60,
              fontWeight: 600,
              lineHeight: 1.07,
              letterSpacing: "-0.035em",
              maxWidth: 1020,
            }}
          >
            Deterministic compaction memory for AI coding agents.
          </div>
          <div
            style={{
              fontSize: 25,
              color: "#5F6368",
              lineHeight: 1.45,
              maxWidth: 920,
            }}
          >
            Preserve verbatim rules, superseding overrides, and failing test
            trajectories across Claude Code PreCompact &amp; SessionStart hooks.
          </div>
        </div>

        {/* 4 Tonal Tier Tiles */}
        <div style={{ display: "flex", gap: "16px", alignItems: "center" }}>
          {[
            { tag: "Tier 1", label: "Pins & Overrides", bg: "#E8F0FE", fg: "#174EA6" },
            { tag: "Tier 2", label: "Verbatim Rules", bg: "#F4F2EE", fg: "#3C4043" },
            { tag: "Tier 3", label: "Test Trajectories", bg: "#FEF7E0", fg: "#9C5300" },
            { tag: "Tier 4", label: "Modified Files", bg: "#E6F4EA", fg: "#137333" },
          ].map((tile) => (
            <div
              key={tile.tag}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "4px",
                flex: 1,
                background: tile.bg,
                borderRadius: 20,
                padding: "16px 20px",
              }}
            >
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: tile.fg,
                }}
              >
                {tile.tag}
              </span>
              <span
                style={{
                  fontSize: 19,
                  fontWeight: 600,
                  color: "#202124",
                }}
              >
                {tile.label}
              </span>
            </div>
          ))}
        </div>
      </div>
    ),
    { ...size }
  );
}
