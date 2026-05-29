# stock-api

Self-hosted yfinance proxy for the ESP8266 stock ticker. Runs on rozakos.eu,
replaces the RapidAPI/Yahoo Finance free tier (and its monthly call cap).

```
ESP8266 ──HTTPS──▶ Cloudflare ──tunnel──▶ stock-api (FastAPI/uvicorn) ──▶ yfinance ──▶ Yahoo
                                                │
                                                └──▶ Postgres (1-min history)
```

## What it does

- **Live quotes** — `GET /stock/{symbol}` returns the last 5 daily closes plus
  pre-computed change/change_pct. Bearer-token auth.
- **Symbol allowlist** — daily refresh of the NASDAQ+NYSE universe
  (~12.6k tickers) from the NASDAQ Trader files. Unknown symbols are
  rejected at the API edge before any yfinance call.
- **Live cache** — 10-minute in-memory cache. On yfinance failure, falls back
  to stale cache rather than 502'ing, so the device never blanks out.
- **Quote poller** — a background task refreshes the *active working set*
  (every symbol requested in the last 15 min) in batches, once a minute while
  the market is open. Device `/stock` requests serve from the cache the poller
  fills, so upstream Yahoo load is bounded by the number of distinct symbols,
  not the number of devices — 500 devices on 500 symbols is ~5 Yahoo requests
  per minute, the same as 50,000 devices would be. See [Scaling](#scaling).
- **1-minute history** — for the 8 most-recently-requested symbols (LRU),
  one row per minute bar is written to Postgres during US regular trading
  hours, retained 30 days. Queryable via `GET /history/{symbol}?days=N`.
- **Range history** — `GET /history/{symbol}?range=…` returns longer windows
  (up to `max`) fetched live from yfinance at fixed period+interval pairs,
  server-cached with TTLs tuned per range. An optional `&limit=N` downsamples
  the series server-side so memory-constrained clients (ESP32) never have to
  parse a multi-thousand-point `max` response.

## API

Base: `https://rozakos.eu/stocks/api/v1`. All `/stock` and `/history`
requests require `Authorization: Bearer <token>`. Cloudflare bot-fight
blocks empty/default User-Agents, so clients **must** send a non-empty UA.

### `GET /stock/{symbol}`

```json
{
  "symbol": "AMD",
  "closes": [148.32, 151.10, 149.88, 153.44, 155.20],
  "last": 155.20,
  "prev": 153.44,
  "change": 1.76,
  "change_pct": 1.15,
  "cached": false,
  "stale": false
}
```

`closes` is oldest→newest, length ≤ 5, no nulls. `cached`/`stale` are debug
flags (`stale: true` means the upstream failed and we served the last good
response).

Status codes: `400` unknown symbol, `401` bad/missing bearer, `502` upstream
Yahoo failure with no prior cache.

### `GET /history/{symbol}` — two modes

**`?range=…`** (recommended) — served live from yfinance and cached
server-side. One of `1d`, `1w`, `1mo`, `6mo`, `1y`, `5y`, `max`. The server
maps each range to a fixed period+interval pair:

| `range` | yfinance period | yfinance interval | `interval` field | cache TTL |
|---|---|---|---|---|
| `1d`  | `1d`  | `5m`  | `intraday` | 60 s |
| `1w`  | `7d`  | `1h`  | `intraday` | 5 min |
| `1mo` | `1mo` | `1d`  | `daily`    | 1 h |
| `6mo` | `6mo` | `1d`  | `daily`    | 1 h |
| `1y`  | `1y`  | `1d`  | `daily`    | 1 h |
| `5y`  | `5y`  | `1wk` | `daily`    | 1 h |
| `max` | `max` | `1wk` | `daily`    | 1 h |

```json
{
  "symbol": "AMD",
  "range": "1mo",
  "interval": "daily",
  "count": 22,
  "points": [{"ts": 1776312000, "last": 278.26}, ...]
}
```

`ts` is **epoch seconds**. `interval` tells the client whether to format
the X axis as time-of-day (`intraday`) or as dates (`daily`).

Invalid `range` values return `422` (FastAPI validation).

**`range=1d` session window** — the `1d` response additionally carries the
day's *regular* trading session as top-level `session_open` / `session_close`
(epoch seconds, UTC), so a client can render the whole session as a fixed X
axis (Revolut-style) and grow the line through it. The bounds come from
yfinance chart metadata, so half-days and holidays use the exchange's real
session rather than a hardcoded 16:00. `points` stays ascending and contains
only elapsed data up to "now" — the future is **not** padded. If the bounds
can't be determined they're omitted, and the client should fall back to a
6.5h assumption.

```json
{
  "symbol": "AMD",
  "range": "1d",
  "interval": "intraday",
  "session_open": 1779975000,
  "session_close": 1779998400,
  "count": 78,
  "points": [{"ts": 1779975000, "last": 98.12}, ...]
}
```

**`&limit=N`** (optional, `N ≥ 1`, any range) — caps the response at `N`
points by **uniform downsampling on the server**, always keeping the first
and last point so the displayed % change stays correct. Without it, `range=max`
returns the full series (a few thousand points for old listings like AAPL/IBM/KO),
which can exhaust RAM on an ESP32 that buffers the whole body before
downsampling. With it, e.g. `range=max&limit=30` returns ≤ 30 points. The
range cache stores the full series, so different `limit` values for the same
symbol/range are all served from one cached fetch. `count` reflects the
returned (downsampled) length; the rest of the shape is unchanged. `limit`
applies only to `range=` — the legacy `days=` path ignores it.

**`?days=N`** (legacy, 1 ≤ N ≤ 30) — served from the Postgres minute-bar
archive. Returned only for symbols currently in the hot LRU. Kept for the
in-field ESP8266 firmware until it migrates to `range=`.

```json
{
  "symbol": "AMD",
  "days": 1,
  "count": 198,
  "points": [{"ts": "2026-05-14T13:30:00+00:00", "last": 442.51}, ...]
}
```

`ts` is ISO 8601 here, not epoch seconds — different mode, different shape.
`503` if `DATABASE_URL` is not configured.

### `GET /logo/{symbol}` and `GET /logos`

Serves cached ticker logos so devices don't need to embed every PNG in
firmware flash. Both endpoints require the bearer token.

**`GET /logo/{symbol}`** returns a 64×64 PNG. Symbol is case-insensitive.
On a miss, the server resolves the logo on the fly through this chain:

1. Manual override in `logo_sources.json` — `{ "IONQ": "https://..." }`.
   Useful for symbols where auto-resolution returns something wrong or
   ugly.
2. The company website from `yfinance.Ticker(symbol).info["website"]`,
   resolved to a logo via DuckDuckGo's `icons.duckduckgo.com/ip3/{domain}.ico`
   (highest-quality), with Google's `s2/favicons?domain={domain}&sz=128`
   as a universal fallback.
3. If all sources fail, the symbol is remembered as a "miss" for 24h to
   avoid retry storms, and the endpoint returns `404 {"detail": "no
   logo for X"}`.

Successful logos are written to `LOGO_CACHE_DIR` and served with
`Cache-Control: public, max-age=2592000, immutable` so the device (and
Cloudflare, if you configure it to ignore the auth header on this
path) can cache aggressively.

**`?size=` query parameter** — accepts `32`, `48`, or `64` (default
`64`). Anything else returns `400`. `size=64` serves the cached file
byte-identical; `size=32` and `size=48` resize the cached 64×64 PNG
on-the-fly with LANCZOS, preserving the RGBA alpha channel. Useful for
embedded clients with constrained transient heap during PNG decode —
e.g. the ESP32 CYD firmware needs `?size=48` to keep lodepng's
allocation inside its largest free block after WiFi+TLS.

**`?test=1` diagnostic mode** — bypasses the resolver and cache and
returns a synthetic 64×64 RGBA PNG (red background, green diagonal,
blue center dot, `Cache-Control: no-store`). Exists to separate
firmware-side PNG render bugs from logo-content/contrast issues. Can
also be forced server-wide with env `STOCK_API_LOGO_TEST=1`.

**`GET /logos?symbols=AAPL,IONQ,NVDA`** returns a JSON manifest without
fetching anything — handy for the device to check which logos it can
download cheaply:

```json
{
  "logos": {
    "AAPL": { "url": "/stocks/api/v1/logo/AAPL", "cached": true },
    "IONQ": { "url": "/stocks/api/v1/logo/IONQ", "cached": false }
  }
}
```

To pre-warm the cache from the command line:

```bash
.venv/bin/python scripts/fetch_logos.py AAPL IONQ NVDA TSLA
```

Logo normalization is applied only when a logo is first fetched. If the
normalization logic changes, remove existing cached PNGs before pre-warming
again, otherwise `/logo/{symbol}` will keep serving the old files:

```bash
find "${LOGO_CACHE_DIR:-data/logos}" -maxdepth 1 -type f -name '*.png' -delete
.venv/bin/python scripts/fetch_logos.py AAPL IONQ NVDA TSLA
```

### `GET /health`

No auth. Useful for uptime checks and seeing service state:

```json
{
  "status": "ok",
  "cached_symbols": ["AMD", "NVDA"],
  "universe_size": 12601,
  "universe_refreshed_at": "2026-05-14T16:07:50.092132",
  "extra_symbols": ["BTC-USD"],
  "history_enabled": true,
  "hot_symbols": ["AMD", "NVDA"],
  "hot_max": 8,
  "tick_seconds": 60,
  "active_symbols": 137,
  "quote_poll_seconds": 60,
  "quote_poll_at": "2026-05-14T16:08:12.114390",
  "quote_poll_ok": true,
  "market_open": true
}
```

`active_symbols` is the size of the quote poller's working set; `quote_poll_at`
/ `quote_poll_ok` are the timestamp and result of its last run (handy for
alerting if the poller stalls).

## Setup

```bash
git clone git@github.com:Rozakos/stock-api.git /home/rozakos/stock-api
cd /home/rozakos/stock-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env: set API_SECRET (token_hex(24)), DATABASE_URL if you want history
sudo cp stock-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-api
```

Edge routing on rozakos.eu uses Cloudflare Tunnel (`cloudflared`):

```yaml
# /etc/cloudflared/config.yml
ingress:
  - hostname: rozakos.eu
    path: ^/stocks/api/v1/(docs|openapi\.json|redoc).*
    service: http_status:404
  - hostname: rozakos.eu
    path: ^/stocks/api/.*
    service: http://127.0.0.1:8001
  - hostname: rozakos.eu
    service: http://127.0.0.1:3000   # the existing Next.js app
  - service: http_status:404
```

If you're fronting with nginx instead, see `nginx.conf.snippet`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_SECRET` | — | Bearer token. Empty disables auth (LAN-only). |
| `CACHE_TTL_SECONDS` | 600 | TTL for the live-quote cache. |
| `EXTRA_SYMBOLS` | (empty) | Comma-separated tickers to allow alongside the universe. Use this for crypto/indices (e.g. `BTC-USD,^GSPC`) until proper sources are added. |
| `DATABASE_URL` | (empty) | Postgres DSN. If unset, history features are disabled and `/history` returns 503. |
| `HISTORY_TICK_SECONDS` | 60 | How often to record minute bars. |
| `HISTORY_RETENTION_DAYS` | 30 | Older rows are pruned on each tick. |
| `HISTORY_MAX_HOT` | 8 | LRU cap on archived symbols. Hitting `/stock/{X}` bumps `X` to the front; oldest is evicted when full. |
| `QUOTE_POLL_SECONDS` | 60 | How often the quote poller refreshes the active set while the market is open. |
| `QUOTE_POLL_CLOSED_SECONDS` | 300 | Poll interval while the market is closed. |
| `QUOTE_ACTIVE_WINDOW_SECONDS` | 900 | A symbol stays in the poller's working set this long after it was last requested. |
| `QUOTE_MAX_ACTIVE` | 1000 | Cap on the working set. The most recently requested symbols win; the rest fall back to on-demand fetch. |
| `QUOTE_BATCH_SIZE` | 100 | Symbols per `yf.download` batch (one Yahoo round-trip). |
| `LOGO_CACHE_DIR` | `data/logos` | Where resolved logos are stored as 64×64 PNGs. Relative paths are resolved from the project root. |
| `LOGO_OVERRIDES_FILE` | `logo_sources.json` | JSON map of `TICKER -> logo URL` to override the auto-resolution chain. |

The on-disk file `symbols.cache.json` stores the last fetched symbol universe
so restarts don't depend on the network. It's gitignored and self-heals on
the next daily refresh.

## Test client

`test_client.py` mimics what the ESP8266 will do: bearer auth, sets a
User-Agent, calls the API, renders the same fields the OLED draws.

```bash
.venv/bin/python test_client.py                          # one cycle, defaults
.venv/bin/python test_client.py --loop --interval 30     # mimic device polling
.venv/bin/python test_client.py --symbols TSM AAPL       # custom tickers
.venv/bin/python test_client.py --history AMD --days 7   # unicode sparkline
.venv/bin/python test_client.py --base http://127.0.0.1:8001/stocks/api/v1  # skip the tunnel
```

## Operations

- Logs: `journalctl -u stock-api -f`
- State: `curl https://rozakos.eu/stocks/api/v1/health | jq`
- Restart: `sudo systemctl restart stock-api`
- DB: rows live in `prices(symbol TEXT, ts TIMESTAMPTZ, last DOUBLE PRECISION)`
  with a `(symbol, ts DESC)` index. Created on first startup.

## Scaling

The thing that breaks first under many devices is **upstream Yahoo load**, not
serving. On-demand fetching makes one Yahoo call per cache miss, so N devices
on N symbols means N fetches per cache cycle — which both stampedes (no
per-symbol lock on the quote path) and risks Yahoo rate-limiting/IP-banning the
server. Serving itself is cheap: in-memory cache reads handle thousands/sec.

The **quote poller** flips this from pull to push: a background task refreshes
the active working set on a timer and device requests just read the cache it
fills. Consequences:

- **Yahoo load is decoupled from device count.** It scales with the number of
  *distinct symbols*, not devices. 500 symbols at `QUOTE_BATCH_SIZE=100` is
  ~5 Yahoo requests/minute, constant, whether 500 or 50,000 devices ask.
- **No stampede / no thread-pool blocking** on the request path — requests are
  in-memory reads.
- A brand-new symbol's first request still falls back to an on-demand fetch;
  the poller picks it up on the next cycle.

Two deliberate boundaries:

- **`/history` ranges stay on-demand** with their per-range cache TTLs. Long
  windows (`1y`, `max`) don't change minute-to-minute, so they don't belong on
  the 1-minute poller. Use `&limit=N` to keep those responses small for
  constrained clients.
- The poller is a **single point of staleness** — if it stalls, quotes freeze.
  The stale-serve fallback covers brief gaps; watch `quote_poll_at` /
  `quote_poll_ok` in `/health` for anything longer.

## Tests

A smoke test in `tests/` exercises every `range=` value against the live
service plus a couple of regression checks. `tests/test_quotes.py` unit-tests
the quote payload builder and the active-set registry offline (no network).

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

Set `STOCK_API_BASE` and `TEST_SYMBOL` to point at a different deployment
or ticker. The test reads `API_SECRET` from `.env`.

## Security notes

- The bearer token is anti-casual-discovery, not crypto. The device stores it
  in plaintext in LittleFS, so treat it as rotatable rather than secret.
- yfinance does HTTPS to Yahoo from the server. The device does HTTPS to
  rozakos.eu. No keys are compiled into the firmware image.
- FastAPI's `/docs` UI is intentionally returned as 404 at the edge so the
  surface isn't advertised. Reachable on `127.0.0.1` if you tunnel in.
- No rate limiting at the app layer. The 10-min cache caps real yfinance load
  to ~1 call per symbol per 10 min, and the symbol allowlist prevents
  unknown-symbol spam from growing the cache or hitting Yahoo.
