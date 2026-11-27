"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { TonalButton } from "@/components/tonal/button";

export interface CopyButtonProps {
  text: string;
  label?: string;
  className?: string;
  compact?: boolean;
}

export function CopyButton({
  text,
  label = "Copy",
  className = "",
  compact = false,
}: CopyButtonProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard unavailable in restricted contexts
    }
  }, [text]);

  return (
    <TonalButton
      type="button"
      variant="tonal"
      size="sm"
      onClick={handleCopy}
      aria-label={copied ? "Copied to clipboard" : label}
      title={copied ? "Copied!" : label}
      className={`${compact ? "px-3.5 py-2 text-[12px]" : ""} shrink-0 ${className}`}
    >
      {copied ? (
        <>
          <Check className="h-3.5 w-3.5 shrink-0" />
          <span>Copied</span>
        </>
      ) : (
        <>
          <Copy className="h-3.5 w-3.5 shrink-0" />
          <span>{label}</span>
        </>
      )}
    </TonalButton>
  );
}
