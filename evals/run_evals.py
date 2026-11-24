#!/usr/bin/env python3
"""Standalone entry point for running the seeded compact-ledger eval suite."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from compact_ledger.evals import (
    SEEDED_EVAL_CASES,
    format_eval_report_json,
    format_eval_report_text,
    run_eval_suite,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"Run the {len(SEEDED_EVAL_CASES)}-case deterministic compact-ledger evaluation suite."
        )
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
        help="Report format (default: text).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Include detailed case descriptions in text output.",
    )
    args = parser.parse_args(argv)

    report = run_eval_suite()
    if args.output_format == "json":
        print(format_eval_report_json(report))
    else:
        print(format_eval_report_text(report, verbose=args.verbose))
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
