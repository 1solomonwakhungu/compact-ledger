"""Allow invoking compact-ledger via ``python -m compact_ledger``."""

from __future__ import annotations

from compact_ledger.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
