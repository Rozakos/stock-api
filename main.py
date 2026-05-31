import asyncio
import json
import math
import os
import time
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont
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

# Background quote poller: refreshes the *active working set* (symbols any
# device asked for recently) in batches, so upstream Yahoo load is bounded by
# the number of distinct symbols, not by the number of devices. Device
# requests then serve from the in-memory cache the poller fills.
QUOTE_POLL_SECONDS = int(os.getenv("QUOTE_POLL_SECONDS", "60"))
QUOTE_POLL_CLOSED_SECONDS = int(os.getenv("QUOTE_POLL_CLOSED_SECONDS", "300"))
QUOTE_ACTIVE_WINDOW = int(os.getenv("QUOTE_ACTIVE_WINDOW_SECONDS", "900"))
QUOTE_MAX_ACTIVE = int(os.getenv("QUOTE_MAX_ACTIVE", "1000"))
QUOTE_BATCH_SIZE = int(os.getenv("QUOTE_BATCH_SIZE", "100"))

LOGO_CACHE_DIR = Path(
    os.getenv("LOGO_CACHE_DIR", str(Path(__file__).parent / "data" / "logos"))
)
LOGO_OVERRIDES_FILE = Path(
    os.getenv("LOGO_OVERRIDES_FILE", str(Path(__file__).parent / "logo_sources.json"))
)
LOGO_SIZE = 64                  # default served size (route contract)
LOGO_MASTER_SIZE = 256          # high-res master kept on disk (best source pixels)
LOGO_CONTENT_RATIO = 0.94       # fraction of the square the logo content fills
LOGO_MIN_NATIVE = int(os.getenv("LOGO_MIN_NATIVE", "32"))  # below this -> monogram
LOGO_MISS_TTL = 24 * 3600
LOGO_USER_AGENT = "stock-api-logo/1.0 (+https://rozakos.eu)"
LOGO_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    _LANCZOS = Image.LANCZOS

RangeKey = Literal["1d", "1w", "1mo", "6mo", "1y", "5y", "max"]

# range → (yfinance period, yfinance interval)
RANGE_MAP: dict[str, tuple[str, str]] = {
    "1d":  ("1d",  "5m"),
    "1w":  ("7d",  "1h"),
    "1mo": ("1mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y":  ("1y",  "1d"),
    "5y":  ("5y",  "1wk"),
    "max": ("max", "1wk"),
}

# server-side cache TTL per range (seconds)
RANGE_TTL: dict[str, int] = {
    "1d":  60,
    "1w":  300,
    "1mo": 3600,
    "6mo": 3600,
    "1y":  3600,
    "5y":  3600,
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

# { "AMD": last_requested_at } — the quote poller's working set.
_active: dict[str, datetime] = {}
_quote_poll_at: datetime | None = None
_quote_poll_ok: bool = False

_logo_overrides: dict[str, str] = {}
_logo_locks: dict[str, asyncio.Lock] = {}


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
    quotes_task = asyncio.create_task(_quote_poll_loop())
    try:
        yield
    finally:
        symbols_task.cancel()
        history_task.cancel()
        quotes_task.cancel()
        if _pool is not None:
            _pool.close()


app = FastAPI(
    title="Stock API",
    docs_url="/stocks/api/v1/docs",
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def access_log(request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        ms = int((time.perf_counter() - start) * 1000)
        ip = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "-"
        )
        print(
            f"access {request.method} {request.url.path} -> 500 {ms}ms ip={ip} EXC",
            flush=True,
        )
        raise
    ms = int((time.perf_counter() - start) * 1000)
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "-"
    )
    ua = (request.headers.get("user-agent") or "-")[:60].replace('"', "'")
    q = f"?{request.url.query}" if request.url.query else ""
    print(
        f'access {request.method} {request.url.path}{q} -> {status} {ms}ms '
        f'ip={ip} ua="{ua}"',
        flush=True,
    )
    return response


def _auth(authorization: str):
    if API_SECRET and authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _is_allowed(symbol: str) -> bool:
    if symbol in EXTRA_SYMBOLS:
        return True
    if not _symbols:
        return True  # fail-open: no universe loaded yet
    return symbol in _symbols


def _build_quote(symbol: str, raw_closes) -> dict:
    """Build the /stock payload from a raw Close series (list/Series). Shared
    by the single-symbol fetch and the batch poller so both produce the exact
    same shape."""
    closes = [
        round(float(v), 4)
        for v in raw_closes
        if v is not None and not (isinstance(v, float) and math.isnan(v))
    ]
    if len(closes) < 2:
        raise ValueError(f"not enough data points for {symbol}")
    closes = closes[-5:]

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


def _fetch(symbol: str) -> dict:
    hist = yf.Ticker(symbol).history(period="5d", interval="1d")
    if hist.empty:
        raise ValueError(f"no data returned for {symbol}")
    return _build_quote(symbol, list(hist["Close"]))


def _fetch_batch(symbols: list[str]) -> dict[str, dict]:
    """Fetch quotes for many symbols in a single Yahoo round-trip (yfinance
    batches them internally). Returns {symbol: payload} for the symbols that
    came back with usable data; missing/invalid symbols are simply absent."""
    if not symbols:
        return {}
    df = yf.download(
        symbols,
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            closes = list(df[sym]["Close"])
        except Exception:
            continue
        try:
            out[sym] = _build_quote(sym, closes)
        except Exception:
            continue
    return out


def _mark_active(symbol: str) -> None:
    _active[symbol] = datetime.utcnow()


def _active_symbols() -> list[str]:
    """Most-recently-requested symbols still inside the active window, capped
    at QUOTE_MAX_ACTIVE. Prunes stale entries as a side effect."""
    cutoff = datetime.utcnow() - timedelta(seconds=QUOTE_ACTIVE_WINDOW)
    for sym in [s for s, t in _active.items() if t < cutoff]:
        _active.pop(sym, None)
    ordered = sorted(_active.items(), key=lambda kv: kv[1], reverse=True)
    return [s for s, _ in ordered[:QUOTE_MAX_ACTIVE]]


def _poll_quotes_sync() -> None:
    """Refresh the active working set into the live-quote cache, in batches."""
    global _quote_poll_at, _quote_poll_ok
    symbols = _active_symbols()
    if not symbols:
        _quote_poll_at = datetime.utcnow()
        return
    fetched = 0
    for i in range(0, len(symbols), QUOTE_BATCH_SIZE):
        batch = symbols[i:i + QUOTE_BATCH_SIZE]
        try:
            quotes = _fetch_batch(batch)
        except Exception:
            quotes = {}
        now = datetime.utcnow()
        for sym, data in quotes.items():
            _cache[sym] = {"ts": now, "data": data}
            fetched += 1
    _quote_poll_at = datetime.utcnow()
    _quote_poll_ok = fetched > 0


async def _quote_poll_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_poll_quotes_sync)
        except Exception:
            pass
        delay = QUOTE_POLL_SECONDS if _is_market_open() else QUOTE_POLL_CLOSED_SECONDS
        await asyncio.sleep(delay)


def _get_cached(symbol: str) -> dict | None:
    entry = _cache.get(symbol)
    if not entry:
        return None
    age = (datetime.utcnow() - entry["ts"]).total_seconds()
    if age < CACHE_TTL:
        return entry["data"]
    return None  # expired but keep entry for stale fallback


def _load_logo_overrides() -> dict[str, str]:
    if not LOGO_OVERRIDES_FILE.exists():
        return {}
    try:
        raw = json.loads(LOGO_OVERRIDES_FILE.read_text())
    except Exception:
        return {}
    base = Path(__file__).parent
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and isinstance(v, str) and v):
            continue
        # A scheme-less value is a repo-relative path to a curated logo asset
        # committed alongside the code; resolve it to a file:// URL so the
        # normal override fetch path can read it. Values with a scheme
        # (http(s)://, file://) are used verbatim.
        url = v if "://" in v else (base / v).resolve().as_uri()
        out[k.upper()] = url
    return out


_logo_overrides = _load_logo_overrides()


def _logo_lock(symbol: str) -> asyncio.Lock:
    lock = _logo_locks.get(symbol)
    if lock is None:
        lock = _logo_locks[symbol] = asyncio.Lock()
    return lock


def _logo_paths(symbol: str) -> tuple[Path, Path]:
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return LOGO_CACHE_DIR / f"{symbol}.png", LOGO_CACHE_DIR / f"{symbol}.miss.json"


def _logo_size_path(symbol: str, size: int) -> Path:
    """Per-(symbol, size) rendered variant, derived from the master in one
    clean downscale and cached on disk."""
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return LOGO_CACHE_DIR / f"{symbol}.{size}.png"


def _clear_logo_variants(symbol: str) -> None:
    """Drop cached per-size variants so they get re-derived from a freshly
    (re)built master."""
    for variant in LOGO_CACHE_DIR.glob(f"{symbol}.*.png"):
        try:
            variant.unlink()
        except OSError:
            pass


def _logo_miss_fresh(miss: Path) -> bool:
    if not miss.exists():
        return False
    try:
        data = json.loads(miss.read_text())
        return datetime.utcnow().timestamp() - float(data.get("ts", 0)) < LOGO_MISS_TTL
    except Exception:
        return False


def _domain_from_website(website: str) -> str | None:
    if not website:
        return None
    parsed = urlparse(website if "://" in website else f"https://{website}")
    host = (parsed.netloc or parsed.path).strip()
    if host.startswith("www."):
        host = host[4:]
    host = host.split("/")[0]
    return host or None


def _ticker_domain(symbol: str) -> str | None:
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    return _domain_from_website(info.get("website") or "")


def _http_get(url: str, timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": LOGO_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _resize_rgba(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Resample RGBA with **premultiplied alpha** so semi-transparent edges
    don't bleed toward the (black) transparent background — i.e. no dark
    halos. Pillow's 'RGBa' mode is premultiplied; round-trip through it."""
    if img.size == target:
        return img
    return img.convert("RGBa").resize(target, _LANCZOS).convert("RGBA")


def _autocrop_alpha(img: Image.Image) -> Image.Image:
    """Trim fully/near-transparent margins so the logo content fills the frame
    consistently regardless of the source's own padding."""
    alpha = img.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def _fit_square(img: Image.Image, size: int) -> bytes:
    """Center the (already RGBA) logo on a transparent size×size canvas,
    content scaled to LOGO_CONTENT_RATIO via a single premultiplied Lanczos
    step, encoded as an optimized small RGBA PNG."""
    img = _autocrop_alpha(img.convert("RGBA"))
    content = max(1, round(size * LOGO_CONTENT_RATIO))
    scale = min(content / img.width, content / img.height)
    target = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    img = _resize_rgba(img, target)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ox = (size - img.width) // 2
    oy = (size - img.height) // 2
    canvas.paste(img, (ox, oy))  # no mask: copy RGBA verbatim, no compositing
    buf = BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _decode_image(raw: bytes) -> Image.Image | None:
    """Decode bytes to RGBA, selecting the largest frame for multi-size ICOs
    (favicon .ico files often pack 16/32/48/.../256 in one file)."""
    if not raw:
        return None
    try:
        img = Image.open(BytesIO(raw))
        if getattr(img, "format", None) == "ICO":
            try:
                sizes = img.ico.sizes()
                if sizes:
                    img.size = max(sizes)
            except Exception:
                pass
        return img.convert("RGBA")
    except Exception:
        return None


def _fetch_image(url: str) -> Image.Image | None:
    try:
        raw = _http_get(url)
    except Exception:
        return None
    return _decode_image(raw)


def _domain_logo_sources(domain: str) -> list[str]:
    """Public sources that take a bare domain and return an image. DuckDuckGo's
    ip3 endpoint serves the site's real icon (often up to 256px); Google's s2
    endpoint at sz=256 is the universal fallback. We request the largest each
    can give and then pick whichever decodes biggest."""
    return [
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
    ]


def _best_source_image(symbol: str) -> Image.Image | None:
    """Resolve the highest-resolution logo we can find: manual override +
    every domain source, decoded, with the **largest native** one winning
    (instead of first-success). Returns None if nothing resolves."""
    # A manual override is an explicit choice — trust it and skip the slow
    # yfinance .info round-trip entirely.
    override = _logo_overrides.get(symbol)
    if override:
        img = _fetch_image(override)
        if img is not None:
            return img
    domain = _ticker_domain(symbol)
    if not domain:
        return None
    candidates = [
        img
        for url in _domain_logo_sources(domain)
        if (img := _fetch_image(url)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda im: max(im.size))


def _load_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in LOGO_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _monogram_master(symbol: str) -> bytes:
    """Clean lettermark fallback rendered at master resolution: a rounded tile
    in a deterministic per-symbol color with the ticker in white bold. Drawn
    big and antialiased so the per-size downscale stays smooth, alpha is clean
    (rounded corners transparent, no matte)."""
    size = LOGO_MASTER_SIZE
    text = symbol[:4] if len(symbol) <= 4 else symbol[:3]
    hue = (hash(symbol) % 360)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fill = Image.new("HSV", (1, 1), (int(hue / 360 * 255), 150, 150)).convert("RGB").getpixel((0, 0))
    draw = ImageDraw.Draw(img)
    radius = round(size * 0.18)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(*fill, 255))
    font = _load_font(round(size * 0.5))
    # shrink the font until the text fits within ~80% of the tile
    while font.size > 8:
        box = draw.textbbox((0, 0), text, font=font)
        if (box[2] - box[0]) <= size * 0.8 and (box[3] - box[1]) <= size * 0.8:
            break
        font = _load_font(font.size - 4)
    box = draw.textbbox((0, 0), text, font=font)
    tx = (size - (box[2] - box[0])) / 2 - box[0]
    ty = (size - (box[3] - box[1])) / 2 - box[1]
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _resolve_master_sync(symbol: str) -> bytes | None:
    """Build the on-disk master PNG: best available source, downscaled to at
    most LOGO_MASTER_SIZE (never upscaled), alpha-trimmed and premultiplied.
    Falls back to a monogram only when a logo resolves but is unusably small;
    returns None when nothing resolves (preserving the 404/miss path)."""
    img = _best_source_image(symbol)
    if img is None:
        return None
    img = _autocrop_alpha(img)
    if max(img.size) < LOGO_MIN_NATIVE:
        return _monogram_master(symbol)
    longest = max(img.size)
    if longest > LOGO_MASTER_SIZE:
        scale = LOGO_MASTER_SIZE / longest
        img = _resize_rgba(
            img, (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        )
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _ensure_logo(symbol: str) -> Path | None:
    img_path, miss_path = _logo_paths(symbol)
    if img_path.exists():
        return img_path
    if _logo_miss_fresh(miss_path):
        return None
    async with _logo_lock(symbol):
        if img_path.exists():
            return img_path
        if _logo_miss_fresh(miss_path):
            return None
        png = await asyncio.to_thread(_resolve_master_sync, symbol)
        if png:
            img_path.write_bytes(png)
            _clear_logo_variants(symbol)  # stale per-size renders -> rebuild
            miss_path.unlink(missing_ok=True)
            return img_path
        miss_path.write_text(json.dumps({"ts": datetime.utcnow().timestamp()}))
        return None


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
        "active_symbols": len(_active),
        "quote_poll_seconds": QUOTE_POLL_SECONDS,
        "quote_poll_at": _quote_poll_at.isoformat() if _quote_poll_at else None,
        "quote_poll_ok": _quote_poll_ok,
        "market_open": _is_market_open(),
    }


@app.get("/stocks/api/v1/stock/{symbol}")
def get_stock(symbol: str, authorization: str = Header(default="")):
    _auth(authorization)
    symbol = symbol.upper()

    if not _is_allowed(symbol):
        raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol}")

    _mark_hot(symbol)
    _mark_active(symbol)

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


def _to_utc_dt(value) -> datetime | None:
    """Coerce a yfinance metadata timestamp (epoch number, datetime, or
    pandas Timestamp) into a tz-aware UTC datetime. Returns None for NaT or
    anything unrecognized."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    # pandas NaT / float NaN are never equal to themselves; NaT also sneaks
    # past the datetime isinstance check below, so reject it up front.
    try:
        if value != value:
            return None
    except Exception:
        pass
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    to_py = getattr(value, "to_pydatetime", None)
    if callable(to_py):
        value = to_py()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _trading_period_rows(periods) -> list[tuple[datetime | None, datetime | None]]:
    """Normalize history_metadata['tradingPeriods'] into (start, end) UTC
    datetime pairs, one per regular session. Handles both the pandas
    DataFrame form (one row per day) and the nested-list-of-dicts form."""
    if periods is None:
        return []
    if hasattr(periods, "columns") and hasattr(periods, "itertuples"):
        cols = list(periods.columns)
        if "start" in cols and "end" in cols:
            return [
                (_to_utc_dt(row.start), _to_utc_dt(row.end))
                for row in periods.itertuples(index=False)
            ]
        return []
    out: list[tuple[datetime | None, datetime | None]] = []
    if isinstance(periods, list):
        stack = list(periods)
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack = item + stack
            elif isinstance(item, dict) and "start" in item and "end" in item:
                out.append((_to_utc_dt(item["start"]), _to_utc_dt(item["end"])))
    return out


def _session_bounds_for(ticker, points: list[dict]) -> tuple[int, int] | None:
    """Regular-session open/close (epoch seconds, UTC) for the trading day
    the intraday points cover. Sourced from yfinance chart metadata so
    half-days/holidays use the exchange's real session, never a hardcoded
    16:00. Returns None when the bounds can't be determined."""
    if not points:
        return None
    meta = getattr(ticker, "history_metadata", None) or {}

    tz_name = meta.get("exchangeTimezoneName")
    try:
        market_tz = ZoneInfo(tz_name) if tz_name else MARKET_TZ
    except Exception:
        market_tz = MARKET_TZ

    target_day = (
        datetime.fromtimestamp(points[-1]["ts"], tz=timezone.utc)
        .astimezone(market_tz)
        .date()
    )

    # Preferred source: the per-day regular session from tradingPeriods.
    for start, end in _trading_period_rows(meta.get("tradingPeriods")):
        if start is None or end is None:
            continue
        if start.astimezone(market_tz).date() == target_day:
            return int(start.timestamp()), int(end.timestamp())

    # Fallback: currentTradingPeriod.regular, only if it lands on the same day.
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start = _to_utc_dt(regular.get("start"))
    end = _to_utc_dt(regular.get("end"))
    if start is not None and end is not None:
        if start.astimezone(market_tz).date() == target_day:
            return int(start.timestamp()), int(end.timestamp())

    return None


def _fetch_range(symbol: str, range_key: str) -> dict:
    period, interval = RANGE_MAP[range_key]
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, interval=interval)
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
    result = {
        "symbol":   symbol,
        "range":    range_key,
        "interval": kind,
        "count":    len(points),
        "points":   points,
    }
    # range=1d only: expose the day's regular-session window so the device can
    # render the whole session as a fixed X axis (Revolut-style). Omitted when
    # bounds are unavailable — the client then falls back to a 6.5h assumption.
    if range_key == "1d":
        bounds = _session_bounds_for(ticker, points)
        if bounds is not None:
            result["session_open"], result["session_close"] = bounds
    return result


def _downsample(points: list, limit: int) -> list:
    """Uniformly sample `points` down to at most `limit` items, always
    keeping the first and last point so the displayed % change stays
    correct. Returns the list unchanged when it's already small enough."""
    n = len(points)
    if limit <= 0 or n <= limit:
        return points
    if limit == 1:
        return [points[-1]]
    step = (n - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)})
    return [points[i] for i in indices]


def _apply_limit(data: dict, limit: int | None) -> dict:
    """Return a copy of a range payload downsampled to <= limit points.
    Leaves the cached payload untouched and preserves every other field
    (interval, session bounds, …); `count` is updated to match."""
    if not limit:
        return data
    points = data["points"]
    if len(points) <= limit:
        return data
    sampled = _downsample(points, limit)
    return {**data, "count": len(sampled), "points": sampled}


@app.get(
    "/stocks/api/v1/history/{symbol}",
    summary="Historical price points by range (yfinance) or legacy days (archive).",
)
def get_history(
    symbol: str,
    range_: RangeKey | None = Query(None, alias="range",
                                    description="One of 1d, 1w, 1mo, 6mo, 1y, 5y, max."),
    limit: int | None = Query(None, ge=1,
                              description="Max points to return for range=… ; "
                                          "server downsamples uniformly, keeping "
                                          "the first and last point."),
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
            return _apply_limit(cached[1], limit)
        try:
            data = _fetch_range(symbol, range_)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        _range_cache[key] = (now, data)
        return _apply_limit(data, limit)

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


def _test_logo_png() -> bytes:
    img = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (255, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    draw.line([(0, 0), (LOGO_SIZE - 1, LOGO_SIZE - 1)], fill=(0, 255, 0, 255), width=6)
    cx = cy = LOGO_SIZE // 2
    r = 8
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 255, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


LOGO_ALLOWED_SIZES = {32, 48, 64}


@app.get("/stocks/api/v1/logo/{symbol}")
async def get_logo(
    symbol: str,
    test: int = 0,
    size: int = 64,
    authorization: str = Header(default=""),
):
    _auth(authorization)
    symbol = symbol.upper()
    if not _is_allowed(symbol):
        raise HTTPException(status_code=400, detail=f"unknown symbol: {symbol}")
    if size not in LOGO_ALLOWED_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"size must be one of {sorted(LOGO_ALLOWED_SIZES)}",
        )
    if test or os.getenv("STOCK_API_LOGO_TEST") == "1":
        data = _test_logo_png()
        print(f"[logo] {symbol} test=1 bytes={len(data)} dims={LOGO_SIZE}x{LOGO_SIZE}")
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    path = await _ensure_logo(symbol)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no logo for {symbol}")
    # Derive (and cache) the requested size from the high-res master in one
    # clean premultiplied downscale; serve the per-(symbol,size) file.
    out_path = _logo_size_path(symbol, size)
    if not out_path.exists():
        async with _logo_lock(symbol):
            if not out_path.exists():
                with Image.open(path) as src:
                    png = await asyncio.to_thread(_fit_square, src.convert("RGBA"), size)
                out_path.write_bytes(png)
    return FileResponse(
        out_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )


@app.get("/stocks/api/v1/logos")
def get_logos_manifest(
    symbols: str = Query(..., description="Comma-separated tickers."),
    authorization: str = Header(default=""),
):
    _auth(authorization)
    out: dict[str, dict] = {}
    for raw in symbols.split(","):
        s = raw.strip().upper()
        if not s or s in out:
            continue
        img_path, _ = _logo_paths(s)
        out[s] = {
            "url": f"/stocks/api/v1/logo/{s}",
            "cached": img_path.exists(),
        }
    return {"logos": out}
