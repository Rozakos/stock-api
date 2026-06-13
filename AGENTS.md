# Stock API Service — Design Notes

Self-hosted replacement for the RapidAPI / Yahoo Finance dependency used by
the ESP8266 stock ticker. Hosted on rozakos.eu. Python + FastAPI + yfinance,
with a Postgres-backed 1-minute history archive on the LAN.

For end-user setup and API reference, see `README.md`. This file is the
design rationale and the build/operations log for future-me.

---

## Why this exists

The ESP8266 firmware used to call `yahoo-finance15.p.rapidapi.com` with a
RapidAPI key. The free tier has a monthly call cap that we kept blowing
through. The fix: run our own thin proxy on rozakos.eu so the device calls
**our** endpoint. yfinance scrapes Yahoo Finance directly from the server,
so no third-party key is required.

---

## Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | yfinance is Python-native |
| Framework | FastAPI 0.115 | async, automatic OpenAPI docs, tiny footprint |
| Data source | yfinance 0.2.x | stable Yahoo Finance scraper, no key needed |
| Server | uvicorn (ASGI) | pairs with FastAPI, low memory |
| History store | Postgres (unraid LAN box) | tiny table, persistent across restarts |
| DB driver | psycopg 3 + psycopg_pool | sync + simple |
| Edge | Cloudflare Tunnel (`cloudflared`) | already in front of rozakos.eu |
| TLS | Cloudflare | nothing to do on our side |
| Process manager | systemd | survives reboots |
| Python env | venv | no Docker needed |

Caddy is also installed on the box (historical reverse proxy). Cloudflare
Tunnel now routes paths directly to localhost ports, so Caddy is no longer
in the request path for this service. `nginx.conf.snippet` is kept for
posterity in case the edge ever changes.

---

## Project layout

```
/home/rozakos/stock-api/
├── README.md            ← user-facing setup + API ref
├── AGENTS.md            ← this file (design notes)
├── main.py              ← FastAPI app
├── test_client.py       ← mimics what the ESP8266 does (sparkline mode too)
├── requirements.txt
├── .env                 ← API_SECRET, DATABASE_URL, ...  (never commit)
├── .env.example
├── .gitignore
├── stock-api.service    ← systemd unit (copy to /etc/systemd/system/)
├── nginx.conf.snippet   ← legacy / reference only
├── requirements-dev.txt ← pytest + requests
├── tests/               ← pytest smoke tests against the live service
└── symbols.cache.json   ← persisted universe (gitignored, self-heals)
```

Flat layout — the service is small enough not to need subdirectories.

---

## API design

Base URL: `https://rozakos.eu/stocks/api/v1`.

### `GET /stock/{symbol}` — live quote

The endpoint the ESP8266 hits every poll. Bearer auth. Returns 5 most recent
daily closes plus pre-computed deltas so the device does no arithmetic.

Response shape and error codes are documented in `README.md`. Two non-obvious
behaviors worth knowing:

- **Stale fallback** — if yfinance throws (network blip, Yahoo rate limit)
  and we have a previous response cached for this symbol, we return it with
  `"stale": true` rather than 502. The device displays the last known price
  instead of blanking out.
- **Allowlist validation** — unknown symbols 400 *before* yfinance is
  touched, so a leaked token can't be used to spam Yahoo with garbage
  tickers and grow the in-memory cache without bound.

### `GET /stocks?symbols=…` — batch quotes

One request for a whole watchlist instead of one `/stock/{symbol}` call per
symbol. Built for the STM32 ticker: fanning out N calls per refresh tied up
its single keep-alive connection, so history taps queued behind quote
fetches. `GET /stocks?symbols=AMD,NVDA,AAPL` →
`{"quotes": [{"symbol","last","change_pct","closes"}, …]}`.

- Entries carry **exactly** the field names/types of `/stock/{symbol}`
  (`symbol`, `last`, `change_pct`, 5-point `closes`) and nothing else — the
  firmware's cJSON parser reads them verbatim, and dropping `prev`/`change`/
  `cached`/`stale` keeps the response inside the device's 24 KB buffer
  (~1.5 KB for 16 symbols, compact `JSONResponse` with `Content-Length`).
- **Omission, not failure** — unknown/allowlist-rejected symbols and ones
  with no data are dropped from `quotes`; one bad ticker never fails the
  whole request. The client must key results by each entry's `symbol`, not
  by request index (order is preserved, but entries can be missing).
- Capped at 16 (`STOCKS_MAX_SYMBOLS`); more → 400, empty → 400.
- Shares the quote cache + poller below; every requested symbol is marked
  active, and cache misses are resolved in **one** `_fetch_batch` Yahoo
  round-trip, not N.

### Live-quote cache & background poller

Distinct from the Postgres minute-bar history further down. Live quotes sit
in an in-memory dict `_cache[symbol] = {ts, data}` with a 10-min TTL
(`CACHE_TTL_SECONDS`); on a miss the single endpoint fetches one symbol and
the batch endpoint fetches all misses in one `_fetch_batch` call. A
background task (`_quote_poll_loop`) refreshes the **active working set** —
every symbol requested via `/stock` or `/stocks` within `QUOTE_ACTIVE_WINDOW`
(15 min), capped at `QUOTE_MAX_ACTIVE` — every `QUOTE_POLL_SECONDS` in
batches of `QUOTE_BATCH_SIZE`. Fast cadence runs through the **extended
session** (`_is_extended_open()`, 04:00–20:00 ET pre+regular+post), dropping
to `QUOTE_POLL_CLOSED_SECONDS` overnight/weekends. Net effect: upstream Yahoo
load scales with the number of *distinct symbols*, not with the number of
devices or requests, so adding clients on the same watchlist is free.

**Extended-hours fields.** Every quote (both endpoints) also carries
`market_state` ∈ {PRE, REGULAR, POST, CLOSED} (coarse, from `_market_state()`
on the US-market clock — no holiday/half-day awareness) plus `pre_market` /
`post_market` and their `*_change_pct` vs the regular close (`last`).
`last`/`change_pct`/`closes` stay regular-session values; the extended price
is the close of the last bar from a batched prepost intraday pull
(`_extended_prices`), attached by `_attach_extended`. Populated only off-hours
(`pre_market` during PRE; `post_market` during POST/CLOSED); during REGULAR
both are null and the upstream prepost call is skipped entirely.

### `GET /history/{symbol}` — two modes

The endpoint serves two distinct data sources behind one URL. The decision
is which query parameter the caller passes.

**`?range=…`** — served live from yfinance per request and cached
server-side per `(symbol, range)`. Required for windows longer than the
30-day archive (`1y`, `max`) and the natural client-facing surface going
forward. Each range maps to a fixed `(period, interval)` pair (see
`RANGE_MAP` in `main.py`) chosen to balance resolution against payload
size. Response carries:
- `ts` as **epoch seconds** (compact on the wire, trivial to parse on
  embedded clients).
- `interval` ∈ {`intraday`, `daily`} so the client knows whether to label
  the X axis with times or dates without re-deriving from the range.
- `range` echoed back for debugging.

Per-range TTLs (`RANGE_TTL`): 60 s for `1d`, 5 min for short intraday
(`1w`), 1 h for daily/weekly ranges. Cache key is `(symbol, range)`,
which also means range-mode responses are de-duped across clients.

**`?days=N`** — legacy minute-bar query from the Postgres archive. Only
useful for symbols currently in the hot LRU. `ts` stays ISO 8601, the top-
level key stays `days`, and the response shape is byte-identical to what
it was before `range=` existed — so the in-field firmware keeps working
until it migrates. 503 if `DATABASE_URL` is not configured.

The two modes intentionally diverge on `ts` shape — same endpoint, different
contract per mode. Less ideal than a unified schema, but the alternative
was either breaking the in-field firmware or shipping a new endpoint just
for the new format. The asymmetry will disappear when the firmware moves
to `range=` and `days=N` is removed.

### `GET /logo/{symbol}` and `GET /logos`

Serves a transparent RGBA PNG per ticker, cached on disk under
`LOGO_CACHE_DIR`. Resolution order is: `logo_sources.json` override →
Brandfetch Logo Link `symbol` (high-res, transparent; only if
`BRANDFETCH_CLIENT_ID` is set, with its "B" placeholder rejected by sha256
and opaque assets skipped) → the larger of the DuckDuckGo ip3 / Google s2
favicons → a generated **monogram** tile when nothing usable resolves
(source below `LOGO_MIN_NATIVE`, 32 px). The chosen source is stored as a
high-res **master** `{SYM}.png` (native resolution capped at 256 px,
alpha-trimmed). Total misses are remembered in a `{SYM}.miss.json` marker
(24 h TTL) and return 404. `logo_sources.json` values may be full URLs or
repo-relative paths to committed assets (e.g. `logo_overrides/NVDA.png`,
how `NVDA` is pinned to a clean mark). `/logos?symbols=A,B,C` is the
manifest endpoint — one URL + cache-status entry per symbol, no images.
If the resolver/resize logic changes, delete cached `*.png` and let them
rebuild (or pre-warm) so stale masters aren't served forever.

**`?size=` query parameter** — accepts `{32, 48, 64}`, default `64`,
anything else 400s. Every size is derived from the high-res master in a
single premultiplied-alpha LANCZOS downscale (premultiply avoids dark
halos on transparent edges), centered on a transparent square at
`LOGO_CONTENT_RATIO` fill, and cached per size as `{SYM}.{size}.png`.
Because the master is now high-res (≤256 px) rather than 64×64, `size=64`
is a derived render too — no longer byte-identical to the master file.
Driven by the ESP32 CYD firmware: lodepng peaks ~60 KB transient for a
64×64 RGBA decode, but the largest contiguous heap block after WiFi+TLS
is ~40 KB — so the device requests `?size=48`; sizes never exceed 64 px.

**`?test=1` diagnostic mode** — `GET /logo/{symbol}?test=1` (or env
`STOCK_API_LOGO_TEST=1`) skips the resolver and the cache and returns a
synthetic 64x64 RGBA PNG: red background, green diagonal stripe, blue
center dot, `Cache-Control: no-store`. Exists to isolate firmware-side
PNG render bugs from logo-content/contrast issues. If the device renders
the red/green/blue mark, the rendering path works and the real logos are
a contrast problem; if not, the renderer (e.g. LVGL file-PNG) is the
suspect, not the API. Remove or gate this if it ever becomes load-bearing
in production traffic.

### `GET /health`

No auth. Exposes:
- `universe_size`, `universe_refreshed_at` — symbol allowlist state
- `cached_symbols` — what's currently in the live-quote cache
- `history_enabled`, `hot_symbols`, `hot_max`, `tick_seconds` — Postgres
  minute-bar history state
- `active_symbols`, `quote_poll_seconds`, `quote_poll_at`, `quote_poll_ok`
  — live-quote poller state (working-set size + last run)
- `market_open` — am I currently in US RTH?

### `GET /docs`

FastAPI auto-generated UI. Reachable on 127.0.0.1 only. Cloudflare Tunnel
returns 404 for `^/stocks/api/v1/(docs|openapi\.json|redoc).*` externally.

---

## Caching strategy

### Live-quote cache (in-memory)

Process-local dict, `{ "AMD": {"ts": datetime, "data": {...}} }`, TTL 10 min.

```
request in
    │
    ├─ cache hit + fresh?  → return cached (cached=true)
    ├─ yfinance fetch OK?  → store + return (cached=false)
    └─ yfinance failed?
          ├─ stale entry exists? → return stale (cached=true, stale=true)
          └─ no entry at all?   → 502
```

No Redis, no SQLite for this layer. Three home tickers + 8 hot symbols ×
tiny dicts = negligible RAM.

### Symbol universe allowlist

On startup, load the last fetched universe from `symbols.cache.json`. A
background task refreshes every 24 h from:

- `https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`
- `https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt`

These are pipe-delimited files with a "Test Issue" column — we filter those
out. Combined set is ~12.6 k tickers. A `set[str]` of that size is ~1–2 MB
of RAM; trivial.

Failure mode: if both fetch *and* the disk cache fail (cold start, network
down), we **fail open** and accept any symbol. Otherwise we enforce against
the in-memory set even if it's a few days stale. The threat model is a
leaked bearer being used to spam unknown tickers, not a 30-minute
NASDAQ Trader outage; staleness is fine.

`EXTRA_SYMBOLS` env var unions in tickers that aren't in those files —
currently `BTC-USD`. When we add a proper crypto/indices source later, this
list shrinks.

### History store (Postgres)

Schema:

```sql
CREATE TABLE prices (
  symbol TEXT NOT NULL,
  ts     TIMESTAMPTZ NOT NULL,
  last   DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX prices_symbol_ts_idx ON prices (symbol, ts DESC);
```

Hot symbols are tracked in-memory as an LRU `OrderedDict` capped at 8
(`HISTORY_MAX_HOT`). Each `/stock/{X}` hit moves `X` to the front; the
oldest entry is evicted when the cap is exceeded. On startup the LRU is
seeded from `SELECT symbol FROM prices GROUP BY symbol ORDER BY MAX(ts)
ASC LIMIT 8` so a restart preserves the most-recently-active tickers.

A background task ticks every 60 s. If `_is_market_open()` returns true
(US weekday + 09:30 ≤ ET < 16:00), it fetches
`yf.Ticker(sym).history(period="1d", interval="1m")` for each hot symbol
and bulk-inserts the resulting bars with `ON CONFLICT DO NOTHING`. The
same tick prunes rows older than 30 days.

Why fetch a full day on every tick instead of just the latest bar:
- Idempotent — a missed tick (service restart, transient network blip)
  is self-healing on the next fire.
- Aligned timestamps — we store Yahoo's actual minute-bar timestamps
  rather than "whenever the cron fired", so the series is regular even
  across tick jitter.
- Tiny — ~400 rows in a payload yfinance is already returning, dedup'd
  at the PK; the wasted bandwidth is negligible at home scale.

Yahoo load: 8 calls/min during RTH = ~0.13 req/s. Well under any anecdotal
yfinance rate-limit threshold.

### Why Postgres on the LAN and not local SQLite

The unraid box already runs Postgres and is the natural place to put
persistent data on this network. If the LAN box is unreachable at startup,
history simply disables itself (`_pool = None`) and the live quote endpoint
runs untouched. `/history` returns 503 in that mode.

---

## Edge routing

Cloudflare → cloudflared tunnel → `127.0.0.1:8001`. Tunnel config at
`/etc/cloudflared/config.yml`:

```yaml
ingress:
  - hostname: rozakos.eu
    path: ^/stocks/api/v1/(docs|openapi\.json|redoc).*
    service: http_status:404
  - hostname: rozakos.eu
    path: ^/stocks/api/.*
    service: http://127.0.0.1:8001
  - hostname: rozakos.eu
    service: http://127.0.0.1:3000   # the existing Next.js app on rozakos.eu
  - service: http_status:404
```

`www.rozakos.eu` has the same rule set (duplicated in the actual config).

Cloudflare's default bot-fight mode 403s requests with no/default User-Agent.
Any non-empty UA passes; the firmware and `test_client.py` both set
`User-Agent: stock-ticker/1.0` for this reason.

---

## ESP8266 firmware changes

Only `fetchHistoricalPrices()` (or wherever the current RapidAPI call lives)
needs to change. Summary for the firmware patch:

1. Host: `rozakos.eu`. Path: `/stocks/api/v1/stock/<SYMBOL>`.
2. Drop the `x-rapidapi-key` and `x-rapidapi-host` headers.
3. Add `Authorization: Bearer <token>`.
4. Add `http.setUserAgent("stock-ticker/1.0")` — without this, Cloudflare
   bot-fight returns 403. The default `ESP8266HTTPClient` UA gets blocked.
5. JSON parsing simplifies: `doc["closes"]` is a flat array, no multi-schema
   guessing required.
6. `doc["change"]` and `doc["change_pct"]` come pre-computed — remove
   the on-device calculation so the screen value matches the server.
7. `SETTINGS_FILE` grows a `bearerToken` field so it can be set via the web
   UI the same way `apiKey` was.

Optional: the `/history/{symbol}?days=N` endpoint is available behind the
same bearer if we want to render a sparkline on the OLED. Up to 30 days,
minute-resolution.

The firmware can cycle through any number of symbols, but only the 8
most-recently-requested get their 1-minute history recorded. That's a
non-issue at 3-ish tickers.

---

## Operations cheat sheet

```bash
# logs
journalctl -u stock-api -f

# state
curl https://rozakos.eu/stocks/api/v1/health | jq

# restart
sudo systemctl restart stock-api

# rotate token
python3 -c "import secrets; print(secrets.token_hex(24))"
# update .env, then restart, then update LittleFS on the device

# inspect history
psql "$DATABASE_URL" -c "SELECT symbol, count(*), min(ts), max(ts) FROM prices GROUP BY symbol"

# force a one-shot tick for testing
.venv/bin/python -c "import main; from psycopg_pool import ConnectionPool; \
  main._pool = ConnectionPool(main.DATABASE_URL, open=True); \
  main._mark_hot('AMD'); main._tick_history_sync()"

# run the smoke tests (service must be up)
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

---

## What's intentionally not built

- **App-layer rate limiting.** The 10-min cache caps real upstream load to
  1 call per symbol per 10 min, and the allowlist caps the symbol space.
  At home scale this is enough. Reach for `slowapi` if it ever opens up.
- **Holiday calendar.** The history tick fires Mon–Fri 09:30–16:00 ET. On
  US market holidays we'll record bars yfinance returns from a closed
  market (usually flat / nulls). Few rows; not worth `pandas_market_calendars`.
- **Crypto / indices / FX in the universe.** `EXTRA_SYMBOLS` covers what
  we need today. Adding CoinGecko-style sources is straightforward when we
  want them.
- **Per-client auth.** One shared bearer. Treat it as rotatable, not secret.

---

## Status

Deployed. ESP8266 firmware patch pending in the separate firmware repo.
