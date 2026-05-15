# Stock API Service — Build Plan

Self-hosted replacement for the RapidAPI / Yahoo Finance dependency used by the
ESP8266 stock ticker. Hosted on rozakos.eu, built with Python + FastAPI + yfinance.

---

## Why this exists

The ESP8266 firmware currently calls `yahoo-finance15.p.rapidapi.com` with a
RapidAPI key. The free tier has a hard monthly call cap that runs out.  
Goal: run our own thin proxy on rozakos.eu so the device calls **our** endpoint
instead. No external API key required after this — yfinance scrapes Yahoo Finance
directly from the server side.

---

## Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | yfinance is Python-native |
| Framework | FastAPI | async, automatic OpenAPI docs, tiny footprint |
| Data source | yfinance 0.2.x | stable Yahoo Finance scraper, no key needed |
| Server | uvicorn (ASGI) | pairs with FastAPI, low memory |
| Reverse proxy | nginx | TLS termination, already likely on rozakos.eu |
| TLS | Let's Encrypt / certbot | free, auto-renews |
| Process manager | systemd | keeps the service alive across reboots |
| Python env | venv | no Docker needed for something this small |

---

## Project layout

```
/home/rozakos/stock-api/
├── AGENTS.md          ← this file
├── main.py            ← FastAPI app (single file, ~80 lines)
├── requirements.txt   ← pinned deps
├── .env               ← API_SECRET=<token>  (never commit)
├── .env.example       ← API_SECRET=changeme
├── stock-api.service  ← systemd unit file (copy to /etc/systemd/system/)
└── nginx.conf.snippet ← nginx server block to paste into sites-available
```

No subdirectory nesting — the whole service is small enough to live flat.

---

## API design

Base URL: `https://rozakos.eu/stocks/api/v1`  
(Uses a path prefix so it can coexist with other things on the same domain.)

### GET `/stocks/api/v1/stock/{symbol}`

Returns the last 5 trading days of closing prices for a single ticker.

**Auth:** `Authorization: Bearer <secret>` header.  
If `API_SECRET` env var is empty the check is skipped (useful for LAN-only).

**Path param:** `symbol` — any Yahoo Finance symbol.  
Examples: `AMD`, `NVDA`, `BTC-USD`, `TSM`

**Response 200:**
```json
{
  "symbol": "AMD",
  "closes": [148.32, 151.10, 149.88, 153.44, 155.20],
  "last":   155.20,
  "prev":   153.44,
  "change": 1.76,
  "change_pct": 1.15,
  "cached": false
}
```

- `closes` — oldest → newest, up to 5 entries, all valid (no nulls)
- `last` / `prev` — the two most recent valid closes
- `change` / `change_pct` — pre-computed so the ESP8266 doesn't have to
- `cached` — `true` if this response came from the in-memory cache

**Response 404:** symbol not found or yfinance returned empty data  
**Response 401:** wrong or missing bearer token  
**Response 502:** yfinance threw an exception (upstream problem)

### GET `/stocks/api/v1/health`

No auth required. Returns `{"status": "ok", "cached_symbols": ["AMD", "NVDA"]}`.  
Used by nginx or uptime monitors to confirm the service is alive.

### GET `/stocks/api/v1/docs`  *(FastAPI built-in)*

Auto-generated OpenAPI UI. Only reachable from localhost in production
(nginx blocks it externally — see nginx config section).

---

## Caching strategy

In-memory dict keyed by uppercase symbol. Each entry stores the response
payload and a `datetime` timestamp.

- **TTL: 10 minutes** (600 s). Stock data on a home ticker cycling every 30 s
  doesn't need to be fresher than that.
- Cache is process-local — it resets on service restart. That's fine; there's
  no persistence requirement.
- No Redis, no SQLite. Three tickers × one tiny dict entry each = negligible RAM.

If yfinance fails (network blip, Yahoo rate-limit), the service returns the
**stale cache entry** with `"cached": true` and a `"stale": true` flag rather
than a 502. This keeps the ESP8266 display showing the last known price instead
of blanking out.

```
request in
    │
    ├─ cache hit + fresh?  → return cached (cached=true)
    ├─ yfinance fetch OK?  → store + return (cached=false)
    └─ yfinance failed?
          ├─ stale entry exists? → return stale (cached=true, stale=true)
          └─ no entry at all?   → 502
```

---

## Implementation steps

### Step 1 — create the venv and install deps

```bash
cd /home/rozakos/stock-api
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn[standard] yfinance python-dotenv
pip freeze > requirements.txt
```

### Step 2 — write main.py

Key sections:

1. **Config** — load `API_SECRET` from `.env` via `python-dotenv`
2. **Cache** — `_cache: dict[str, dict]` with `ts` and `data` keys
3. **`_fetch(symbol)`** — calls `yf.Ticker(symbol).history(period="5d", interval="1d")`,
   extracts the `Close` column, drops NaNs, returns the list of floats
4. **`GET /stock/{symbol}`** — auth check → cache lookup → fetch → cache store → return
5. **`GET /health`** — no auth, returns status + cached symbol list

### Step 3 — write the .env file

```
API_SECRET=pick-something-random-here
```

Generate a token: `python3 -c "import secrets; print(secrets.token_hex(24))"`

### Step 4 — write the systemd unit

File: `stock-api.service` (also saved in the repo root for reference).

```ini
[Unit]
Description=Stock API (yfinance proxy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rozakos
WorkingDirectory=/home/rozakos/stock-api
EnvironmentFile=/home/rozakos/stock-api/.env
ExecStart=/home/rozakos/stock-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Install and enable:
```bash
sudo cp stock-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-api
sudo systemctl status stock-api
```

### Step 5 — nginx config

Add a `location` block inside the existing `rozakos.eu` server block.  
The snippet is saved as `nginx.conf.snippet` in the repo root.

```nginx
# Stock API proxy
location /stocks/api/ {
    proxy_pass         http://127.0.0.1:8001/stocks/api/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_read_timeout 15s;
}

# Block external access to the auto-docs UI
location /stocks/api/v1/docs {
    allow 127.0.0.1;
    deny  all;
}
location /stocks/api/v1/redoc {
    allow 127.0.0.1;
    deny  all;
}
location /stocks/api/v1/openapi.json {
    allow 127.0.0.1;
    deny  all;
}
```

After editing, test and reload:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

TLS is handled by the existing certbot cert for rozakos.eu — no extra steps if
the cert already covers the domain.

### Step 6 — smoke test from the server

```bash
# health (no auth)
curl https://rozakos.eu/stocks/api/v1/health

# real ticker (with auth)
curl -H "Authorization: Bearer <your-secret>" \
     https://rozakos.eu/stocks/api/v1/stock/AMD
```

Expected: JSON with `closes`, `last`, `prev`, `change`, `change_pct`.

---

## Security notes

- The bearer token prevents the endpoint from being an open relay for anyone
  who stumbles onto the URL. It is not cryptographic auth — don't reuse a
  password you care about.
- yfinance talks to Yahoo Finance over HTTPS from the server. The ESP8266 talks
  to rozakos.eu over HTTPS. No credentials touch the ESP8266 firmware image
  except the bearer token (which lives in LittleFS, not compiled-in).
- The `/docs` UI is blocked externally so the API surface isn't advertised.
- Rate limiting is not needed for personal use (3 tickers × every 30 s = 6 req/min).
  If you ever open this to more devices, add `slowapi` (FastAPI rate-limit library).

---

## Resume checklist

- [x] `main.py` written
- [x] `requirements.txt` pinned
- [x] `.env.example` created
- [x] `stock-api.service` systemd unit written
- [x] `nginx.conf.snippet` written
- [ ] On the server: create venv and install deps
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- [ ] Create `.env` with a real secret
  ```bash
  cp .env.example .env
  python3 -c "import secrets; print(secrets.token_hex(24))"
  # paste output into .env as API_SECRET
  ```
- [ ] Test locally on the server
  ```bash
  source .venv/bin/activate
  uvicorn main:app --reload --port 8001
  curl http://127.0.0.1:8001/stocks/api/v1/health
  curl -H "Authorization: Bearer <secret>" http://127.0.0.1:8001/stocks/api/v1/stock/AMD
  ```
- [ ] Install and start the systemd service
  ```bash
  sudo cp stock-api.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now stock-api
  sudo systemctl status stock-api
  ```
- [ ] Add nginx snippet and reload
  ```bash
  # paste nginx.conf.snippet into /etc/nginx/sites-available/rozakos.eu
  sudo nginx -t && sudo systemctl reload nginx
  ```
- [ ] Smoke test through the public URL
  ```bash
  curl https://rozakos.eu/stocks/api/v1/health
  curl -H "Authorization: Bearer <secret>" https://rozakos.eu/stocks/api/v1/stock/AMD
  ```
