interface BrandMarkProps {
  className?: string;
}

export function BrandMark({ className = "h-6 w-6 shrink-0" }: BrandMarkProps) {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" className={className}>
      <rect x="8" y="5" width="16" height="4" rx="2" fill="var(--t-blue)" />
      <path
        d="M8 13.5A1.5 1.5 0 0 1 9.5 12H22.5A1.5 1.5 0 0 1 24 13.5V25C24 26.2 22.65 26.92 21.66 26.22L16.88 22.85C16.35 22.48 15.65 22.48 15.12 22.85L10.34 26.22C9.35 26.92 8 26.2 8 25V13.5Z"
        fill="var(--t-ink)"
      />
    </svg>
  );
}
