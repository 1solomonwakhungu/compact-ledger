import type { Metadata } from "next";
import "./globals.css";

const SITE_URL = "https://compact-ledger.harlo.dev";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "compact-ledger — Deterministic Compaction Memory for AI Coding Agents",
    template: "%s — compact-ledger",
  },
  description:
    "Zero-LLM PreCompact & SessionStart memory injector for AI coding agents. Preserves verbatim user constraints, superseding overrides, and failing test trajectories in Python 3.11+ stdlib.",
  alternates: {
    canonical: "/",
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    siteName: "compact-ledger",
    title: "compact-ledger — Deterministic Compaction Memory for AI Coding Agents",
    description:
      "Preserve verbatim rules, superseding overrides, and failing test trajectories across Claude Code context compactions. Zero LLM calls, zero runtime dependencies.",
  },
  twitter: {
    card: "summary_large_image",
    title: "compact-ledger — Deterministic Compaction Memory for AI Coding Agents",
    description:
      "Deterministic PreCompact + SessionStart memory injector for AI coding agents. Python 3.11+ stdlib, zero LLM calls, 24/24 evals passing.",
  },
};

const THEME_INIT_SCRIPT = `(function(){try{var d=document.documentElement;var p=new URLSearchParams(window.location.search);var qT=p.get('theme');var t=(qT==='dark'||qT==='light')?qT:localStorage.getItem('compact-ledger-theme');if(t!=='dark'&&t!=='light'){t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}d.dataset.tonalTheme=t;d.dataset.tonalMode='pill';}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          href="https://fonts.googleapis.com/css2?family=Google+Sans:opsz,wght@17..18,400;17..18,500;17..18,700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
