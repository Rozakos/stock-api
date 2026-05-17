import asyncio
import json
import math
import os
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from psycopg_pool import ConnectionPool

load_dotenv()

API_SECRET = os.getenv("API_SECRET", "")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
EXTRA_SYMBOLS = {
    s.strip().upper()
    for s in os.getenv("EXTRA_SYMBOLS", "").split(",")
    if s.strip()
}

SYMBOL_SOURCES = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]
SYMBOL_CACHE_FILE = Path(__file__).parent / "symbols.cache.json"
SYMBOL_REFRESH_INTERVAL = 24 * 60 * 60  # 24h

HISTORY_TICK_SECONDS = int(os.getenv("HISTORY_TICK_SECONDS", "60"))
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", "30"))
HISTORY_MAX_HOT = int(os.getenv("HISTORY_MAX_HOT", "8"))
MARKET_TZ = ZoneInfo("America/New_York")

RangeKey = Literal["1d", "5d", "1w", "1mo", "6mo", "1y", "max"]

# range → (yfinance period, yfinance interval)
RANGE_MAP: dict[str, tuple[str, str]] = {
    "1d":  ("1d",  "5m"),
    "5d":  ("5d",  "30m"),
    "1w":  ("7d",  "1h"),
    "1mo": ("1mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y":  ("1y",  "1d"),
    "max": ("max", "1wk"),
}

# server-side cache TTL per range (seconds)
RANGE_TTL: dict[str, int] = {
    "1d":  60,
    "5d":  300,
    "1w":  300,
    "1mo": 3600,
    "6mo": 3600,
    "1y":  3600,
    "max": 3600,
}

# yfinance interval strings considered intraday (anything finer than 1d)
_INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}

# { "AMD": {"ts": datetime, "data": {...}} }
_cache: dict = {}
_symbols: set[str] = set()
_symbols_refreshed_at: datetime | None = None
_hot: "OrderedDict[str, None]" = OrderedDict()
_pool: ConnectionPool | None = None
# { ("AMD","1d"): (fetched_at, payload) }
_range_cache: dict[tuple[str, str], tuple[datetime, dict]] = {}


def _mark_hot(symbol: str) -> None:
    if symbol in _hot:
        _hot.move_to_end(symbol)
    else:
        _hot[symbol] = None
        while len(_hot) > HISTORY_MAX_HOT:
            _hot.popitem(last=False)


def _parse_nasdaq_file(text: str) -> set[str]:
    out: set[str] = set()
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    sym_idx = 0
    for candidate in ("ACT Symbol", "Symbol"):
        if candidate in header:
            sym_idx = header.index(candidate)
            break
    test_idx = header.index("Test Issue") if "Test Issue" in header else None
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= sym_idx:
            continue
        sym = parts[sym_idx].strip().upper()
        if not sym:
            continue
        if test_idx is not None and len(parts) > test_idx and parts[test_idx] == "Y":
            continue
        out.add(sym)
    return out


def _fetch_symbol_universe() -> set[str]:
    new: set[str] = set()
    for url in SYMBOL_SOURCES:
        req = urllib.request.Request(url, headers={"User-Agent": "stock-api/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            new |= _parse_nasdaq_file(resp.read().decode())
    return new


def _load_cached_symbols() -> set[str]:
    if not SYMBOL_CACHE_FILE.exists():
        return set()
    try:
        return set(json.loads(SYMBOL_CACHE_FILE.read_text()))
    except Exception:
        return set()


def _save_cached_symbols(syms: set[str]) -> None:
    SYMBOL_CACHE_FILE.write_text(json.dumps(sorted(syms)))


async def _refresh_loop() -> None:
    global _symbols, _symbols_refreshed_at
    while True:
        try:
            new = await asyncio.to_thread(_fetch_symbol_universe)
            if new:
                _symbols = new
                _symbols_refreshed_at = datetime.utcnow()
                await asyncio.to_thread(_save_cached_symbols, new)
        except Exception:
            pass
        await asyncio.sleep(SYMBOL_REFRESH_INTERVAL)


def _is_market_open(now_utc: datetime | None = None) -> bool:
    now = (now_utc or datetime.now(tz=timezone.utc)).astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _init_db() -> None:
    assert _pool is not None
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prices (
                symbol TEXT        NOT NULL,
                ts     TIMESTAMPTZ NOT NULL,
                last   DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (symbol, ts)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS prices_symbol_ts_idx ON prices (symbol, ts DESC)"
        )


def _load_hot_from_db() -> "OrderedDict[str, None]":
    if _pool is None:
        return OrderedDict()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol
            FROM prices
            WHERE ts > %s
            GROUP BY symbol
            ORDER BY MAX(ts) ASC
            LIMIT %s
            """,
            (cutoff, HISTORY_MAX_HOT),
        )
        return OrderedDict((r[0], None) for r in cur.fetchall())


def _fetch_minute_bars(symbol: str) -> list[tuple[datetime, float]]:
    hist = yf.Ticker(symbol).history(period="1d", interval="1m")
    if hist.empty:
        return []
    out: list[tuple[datetime, float]] = []
    for idx, row in hist.iterrows():
        v = row["Close"]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        out.append((ts, float(v)))
    return out


def _tick_history_sync() -> None:
    if _pool is None or not _hot:
        return
    rows: list[tuple[str, datetime, float]] = []
    for sym in list(_hot.keys()):
        try:
            bars = _fetch_minute_bars(sym)
        except Exception:
            continue
        rows.extend((sym, ts, last) for ts, last in bars)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    with _pool.connection() as conn, conn.cursor() as cur:
        if rows:
            cur.executemany(
                "INSERT INTO prices (symbol, ts, last) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                rows,
            )
        cur.execute("DELETE FROM prices WHERE ts < %s", (cutoff,))


async def _history_loop() -> None:
    while True:
        try:
            if _is_market_open():
                await asyncio.to_thread(_tick_history_sync)
        except Exception:
            pass
        await asyncio.sleep(HISTORY_TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _symbols, _pool, _hot
    _symbols = _load_cached_symbols()

    if DATABASE_URL:
        try:
            pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False)
            pool.open(wait=True, timeout=10)
            _pool = pool
            await asyncio.to_thread(_init_db)
            _hot = await asyncio.to_thread(_load_hot_from_db)
        except Exception:
            _pool = None

    symbols_task = asyncio.create_task(_refresh_loop())
    history_task = asyncio.create_task(_history_loop())
    try:
        yield
    finally:
        symbols_task.cancel()
        history_task.cancel()
        if _pool is not None:
            _pool.close()


app = FastAPI(
    title="Stock API",
    docs_url="/stocks/api/v1/docs",
    redoc_url=None,
    lifespan=lifespan,
)


def _auth(authorization: str):
    if API_SECRET and authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _is_allowed(symbol: str) -> bool:
    if symbol in EXTRA_SYMBOLS:
        return True
    if not _symbols:
        return True  # fail-open: no universe loaded yet
    return symbol in _symbols


def _fetch(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="5d", interval="1d")
    if hist.empty:
        raise ValueError(f"no data returned for {symbol}")

    closes = [round(float(v), 4) for v in hist["Close"] if not math.isnan(v)]
    if len(closes) < 2:
        raise ValueError(f"not enough data points for {symbol}")

    last = closes[-1]
    prev = closes[-2]
    change = round(last - prev, 4)
    change_pct = round((change / prev) * 100, 4) if prev else 0.0

    return {
        "symbol":     symbol,
        "closes":     closes,
        "last":       last,
        "prev":       prev,
        "change":     change,
        "change_pct": change_pct,
    }


def _get_cached(symbol: str) -> dict | None:
    entry = _cache.get(symbol)
    if not entry:
        return None
    age = (datetime.utcnow() - entry["ts"]).total_seconds()
    if age < CACHE_TTL:
        return entry["data"]
    return None  # expired but keep entry for stale fallback


@app.get("/stocks/api/v1/health")
def health():
    return {
        "status": "ok",
        "cached_symbols": list(_cache.keys()),
        "universe_size": len(_symbols),
        "universe_refreshed_at": _symbols_refreshed_at.isoformat() if _symbols_refreshed_at else None,
        "extra_symbols": sorted(EXTRA_SYMBOLS),
        "history_enabled": _pool is not None,
        "hot_symbols": list(_hot.keys()),
        "hot_max": HISTORY_MAX_HOT,
        "tick_seconds": HISTORY_TICK_SECONDS,
        "market_open": _is_market_open(),
    }


@app.get("/stocks/api/v1/stock/{symbol}")
def get_stock(symbol: str, authorization: str = Header(default="")):
    _auth(authorization)
    symbol = symbol.upper()

    if not _is_allowed(symbol):
        raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol}")

    _mark_hot(symbol)

    fresh = _get_cached(symbol)
    if fresh:
        return {**fresh, "cached": True, "stale": False}

    try:
        data = _fetch(symbol)
        _cache[symbol] = {"ts": datetime.utcnow(), "data": data}
        return {**data, "cached": False, "stale": False}

    except Exception as exc:
        stale = _cache.get(symbol)
        if stale:
            return {**stale["data"], "cached": True, "stale": True}
        raise HTTPException(status_code=502, detail=str(exc))


def _fetch_range(symbol: str, range_key: str) -> dict:
    period, interval = RANGE_MAP[range_key]
    hist = yf.Ticker(symbol).history(period=period, interval=interval)
    points: list[dict] = []
    if not hist.empty:
        for idx, row in hist.iterrows():
            v = row["Close"]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            points.append({"ts": int(ts.timestamp()), "last": round(float(v), 4)})
    kind = "intraday" if interval in _INTRADAY_INTERVALS else "daily"
    return {
        "symbol":   symbol,
        "range":    range_key,
        "interval": kind,
        "count":    len(points),
        "points":   points,
    }


@app.get(
    "/stocks/api/v1/history/{symbol}",
    summary="Historical price points by range (yfinance) or legacy days (archive).",
)
def get_history(
    symbol: str,
    range_: RangeKey | None = Query(None, alias="range",
                                    description="One of 1d, 5d, 1w, 1mo, 6mo, 1y, max."),
    days: int = Query(7, ge=1, le=HISTORY_RETENTION_DAYS,
                      description="Legacy: minute bars over the last N days from the archive."),
    authorization: str = Header(default=""),
):
    _auth(authorization)
    symbol = symbol.upper()
    if not _is_allowed(symbol):
        raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol}")

    if range_ is not None:
        key = (symbol, range_)
        now = datetime.now(tz=timezone.utc)
        cached = _range_cache.get(key)
        if cached and (now - cached[0]).total_seconds() < RANGE_TTL[range_]:
            return cached[1]
        try:
            data = _fetch_range(symbol, range_)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        _range_cache[key] = (now, data)
        return data

    # Legacy ?days=N path — minute bars from the Postgres archive.
    if _pool is None:
        raise HTTPException(status_code=503, detail="history not available")

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, last FROM prices WHERE symbol = %s AND ts > %s ORDER BY ts ASC",
            (symbol, cutoff),
        )
        rows = cur.fetchall()

    return {
        "symbol": symbol,
        "days":   days,
        "count":  len(rows),
        "points": [{"ts": ts.isoformat(), "last": last} for ts, last in rows],
    }
