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
- **1-minute history** — for the 8 most-recently-requested symbols (LRU),
  one row per minute bar is written to Postgres during US regular trading
  hours, retained 30 days. Queryable via `GET /history/{symbol}?days=N`.
- **Range history** — `GET /history/{symbol}?range=…` returns longer windows
  (up to `max`) fetched live from yfinance at fixed period+interval pairs,
  server-cached with TTLs tuned per range.

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
server-side. One of `1d`, `5d`, `1w`, `1mo`, `6mo`, `1y`, `max`. The server
maps each range to a fixed period+interval pair:

| `range` | yfinance period | yfinance interval | `interval` field | cache TTL |
|---|---|---|---|---|
| `1d`  | `1d`  | `5m`  | `intraday` | 60 s |
| `5d`  | `5d`  | `30m` | `intraday` | 5 min |
| `1w`  | `7d`  | `1h`  | `intraday` | 5 min |
| `1mo` | `1mo` | `1d`  | `daily`    | 1 h |
| `6mo` | `6mo` | `1d`  | `daily`    | 1 h |
| `1y`  | `1y`  | `1d`  | `daily`    | 1 h |
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
  "market_open": true
}
```

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

## Tests

A smoke test in `tests/` exercises every `range=` value against the live
service plus a couple of regression checks.

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
