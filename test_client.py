"""Mimics what the ESP8266 firmware does: GET /stock/<symbol> with a bearer
token, parse the JSON, render the fields the OLED would draw.

Also supports --history SYM to render a unicode sparkline of stored history."""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE = "https://rozakos.eu/stocks/api/v1"
DEFAULT_SYMBOLS = ["AMD", "NVDA", "BTC-USD"]
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _get(url: str, token: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "stock-ticker/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch(base: str, symbol: str, token: str, timeout: float) -> dict:
    return _get(f"{base}/stock/{symbol}", token, timeout)


def fetch_history(base: str, symbol: str, days: int, token: str, timeout: float) -> dict:
    qs = urllib.parse.urlencode({"days": days})
    return _get(f"{base}/history/{symbol}?{qs}", token, timeout)


def render(data: dict) -> str:
    arrow = "▲" if data["change"] >= 0 else "▼"
    flags = []
    if data.get("cached"):
        flags.append("cached")
    if data.get("stale"):
        flags.append("STALE")
    tag = f"  [{', '.join(flags)}]" if flags else ""
    return (
        f"{data['symbol']:<8} {data['last']:>10.2f} "
        f"{arrow} {data['change']:+.2f} ({data['change_pct']:+.2f}%){tag}"
    )


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0:
        return SPARK_CHARS[0] * len(values)
    step = span / (len(SPARK_CHARS) - 1)
    return "".join(SPARK_CHARS[min(int((v - lo) / step), len(SPARK_CHARS) - 1)] for v in values)


def render_history(data: dict) -> str:
    pts = data["points"]
    if not pts:
        return f"{data['symbol']}: no history yet (need market hours + a tick)"
    values = [p["last"] for p in pts]
    lo, hi = min(values), max(values)
    first, last = values[0], values[-1]
    delta = last - first
    pct = (delta / first * 100) if first else 0.0
    arrow = "▲" if delta >= 0 else "▼"
    return (
        f"{data['symbol']} {data['days']}d  n={len(values):<4} "
        f"lo={lo:.2f}  hi={hi:.2f}  {arrow} {delta:+.2f} ({pct:+.2f}%)\n"
        f"  {sparkline(values)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default=os.getenv("API_SECRET", ""))
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--loop", action="store_true", help="keep cycling like the device")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--history", nargs="+", metavar="SYM",
                    help="render sparkline(s) of stored history instead of live quote")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("no token: set API_SECRET in .env or pass --token")

    if args.history:
        for sym in args.history:
            try:
                data = fetch_history(args.base, sym, args.days, args.token, args.timeout)
                print(render_history(data))
            except urllib.error.HTTPError as e:
                print(f"{sym:<8} HTTP {e.code}: {e.reason}")
            except Exception as e:
                print(f"{sym:<8} ERROR: {e}")
        return

    while True:
        for sym in args.symbols:
            t0 = time.monotonic()
            try:
                data = fetch(args.base, sym, args.token, args.timeout)
                ms = (time.monotonic() - t0) * 1000
                print(f"{render(data)}  ({ms:.0f} ms)")
            except urllib.error.HTTPError as e:
                print(f"{sym:<8} HTTP {e.code}: {e.reason}")
            except Exception as e:
                print(f"{sym:<8} ERROR: {e}")
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
