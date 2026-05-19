#!/usr/bin/env python3
"""Pre-populate the logo cache for one or more tickers.

Usage:
    python scripts/fetch_logos.py AAPL IONQ NVDA

Resolves each symbol through the same chain the API uses (override file
→ yfinance .info website → Clearbit), writes normalized 64x64 PNGs to
LOGO_CACHE_DIR, and reports which ones failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402


def run(args: list[str]) -> int:
    if not args:
        print("usage: fetch_logos.py SYMBOL [SYMBOL ...]", file=sys.stderr)
        return 2

    ok: list[str] = []
    missing: list[str] = []
    for raw in args:
        sym = raw.strip().upper()
        if not sym:
            continue
        img_path, miss_path = main._logo_paths(sym)
        if img_path.exists():
            print(f"  = {sym}: already cached at {img_path}")
            ok.append(sym)
            continue
        png = main._resolve_logo_sync(sym)
        if png is None:
            print(f"  - {sym}: NOT FOUND")
            missing.append(sym)
            continue
        img_path.write_bytes(png)
        miss_path.unlink(missing_ok=True)
        print(f"  + {sym}: {len(png)} bytes -> {img_path}")
        ok.append(sym)

    print(f"\nDone: {len(ok)} cached, {len(missing)} missing.")
    if missing:
        print(f"Missing: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
