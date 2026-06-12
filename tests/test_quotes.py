"""Offline unit tests for the quote payload builder, the batch parser, and
the active-symbol registry that drives the background quote poller. No network
or running service required — yfinance is monkeypatched where needed."""
from datetime import datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import main


def _quote(sym):
    return {"symbol": sym, "closes": [1.0, 2.0, 3.0, 4.0, 5.0],
            "last": 5.0, "prev": 4.0, "change": 1.0, "change_pct": 25.0}


def test_build_quote_basic():
    q = main._build_quote("AMD", [10.0, 11.0, 12.0])
    assert q["symbol"] == "AMD"
    assert q["last"] == 12.0
    assert q["prev"] == 11.0
    assert q["change"] == 1.0
    assert q["change_pct"] == round((1.0 / 11.0) * 100, 4)
    assert q["closes"] == [10.0, 11.0, 12.0]


def test_build_quote_trims_to_five_and_skips_holes():
    q = main._build_quote("AMD", [1, 2, 3, 4, 5, 6, 7, float("nan"), None])
    # NaN/None dropped, then trimmed to the last 5 valid closes.
    assert q["closes"] == [3, 4, 5, 6, 7]
    assert q["last"] == 7
    assert q["prev"] == 6


def test_build_quote_requires_two_points():
    with pytest.raises(ValueError):
        main._build_quote("AMD", [42.0])
    with pytest.raises(ValueError):
        main._build_quote("AMD", [float("nan"), None])


def test_active_symbols_prunes_and_orders_recent_first():
    main._active.clear()
    now = datetime.utcnow()
    main._active["OLD"] = now - timedelta(seconds=main.QUOTE_ACTIVE_WINDOW + 5)
    main._active["A"] = now - timedelta(seconds=2)
    main._active["B"] = now
    out = main._active_symbols()
    assert "OLD" not in out          # outside the active window → pruned
    assert "OLD" not in main._active  # pruning is a side effect
    assert out == ["B", "A"]          # most recently requested first


def test_active_symbols_respects_cap(monkeypatch):
    main._active.clear()
    monkeypatch.setattr(main, "QUOTE_MAX_ACTIVE", 3)
    base = datetime.utcnow()
    for i in range(10):
        main._active[f"S{i}"] = base + timedelta(seconds=i)
    out = main._active_symbols()
    assert out == ["S9", "S8", "S7"]


def test_fetch_batch_parses_and_skips_missing(monkeypatch):
    df = pd.DataFrame({
        ("AAPL", "Close"): [10.0, 11.0, 12.0],
        ("BAD", "Close"): [float("nan")] * 3,
    })
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: df)

    out = main._fetch_batch(["AAPL", "BAD"])
    assert set(out) == {"AAPL"}       # all-NaN symbol dropped, no crash
    assert out["AAPL"]["last"] == 12.0
    assert out["AAPL"]["prev"] == 11.0


def test_fetch_batch_empty_is_noop(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("yf.download should not be called for empty input")

    monkeypatch.setattr(main.yf, "download", _boom)
    assert main._fetch_batch([]) == {}


# --- GET /stocks batch endpoint -------------------------------------------

STOCKS_URL = "/stocks/api/v1/stocks"


def _stocks_client(monkeypatch, allow=lambda s: True):
    monkeypatch.setattr(main, "API_SECRET", "")          # disable auth
    monkeypatch.setattr(main, "_is_allowed", allow)
    main._cache.clear()
    main._active.clear()
    return TestClient(main.app)


def test_stocks_batch_basic_shape_and_order(monkeypatch):
    c = _stocks_client(monkeypatch)
    monkeypatch.setattr(main, "_fetch_batch", lambda syms: {s: _quote(s) for s in syms})
    r = c.get(STOCKS_URL, params={"symbols": "amd,nvda"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [q["symbol"] for q in body["quotes"]] == ["AMD", "NVDA"]  # order preserved
    q = body["quotes"][0]
    # exactly the four fields the firmware parser expects, same types
    assert set(q.keys()) == {"symbol", "last", "change_pct", "closes"}
    assert q["last"] == 5.0 and q["change_pct"] == 25.0
    assert q["closes"] == [1.0, 2.0, 3.0, 4.0, 5.0] and len(q["closes"]) == 5
    # compact JSON + Content-Length, no chunked streaming
    assert b", " not in r.content and b'": ' not in r.content
    assert "content-length" in r.headers
    assert r.headers.get("transfer-encoding") != "chunked"


def test_stocks_registers_active_set(monkeypatch):
    c = _stocks_client(monkeypatch)
    monkeypatch.setattr(main, "_fetch_batch", lambda syms: {s: _quote(s) for s in syms})
    c.get(STOCKS_URL, params={"symbols": "AMD,NVDA,AAPL"})
    assert {"AMD", "NVDA", "AAPL"} <= set(main._active)


def test_stocks_dedupes_and_uppercases(monkeypatch):
    c = _stocks_client(monkeypatch)
    monkeypatch.setattr(main, "_fetch_batch", lambda syms: {s: _quote(s) for s in syms})
    r = c.get(STOCKS_URL, params={"symbols": "amd,AMD,nvda"})
    assert [q["symbol"] for q in r.json()["quotes"]] == ["AMD", "NVDA"]


def test_stocks_omits_unknown_never_fails(monkeypatch):
    c = _stocks_client(monkeypatch, allow=lambda s: s != "BAD")
    monkeypatch.setattr(main, "_fetch_batch", lambda syms: {s: _quote(s) for s in syms})
    r = c.get(STOCKS_URL, params={"symbols": "AMD,BAD,NVDA"})
    assert r.status_code == 200
    assert [q["symbol"] for q in r.json()["quotes"]] == ["AMD", "NVDA"]


def test_stocks_omits_symbol_with_no_data(monkeypatch):
    c = _stocks_client(monkeypatch)
    # NVDA returns no data from the batch and has no stale cache -> omitted
    monkeypatch.setattr(main, "_fetch_batch", lambda syms: {"AMD": _quote("AMD")})
    r = c.get(STOCKS_URL, params={"symbols": "AMD,NVDA"})
    assert r.status_code == 200
    assert [q["symbol"] for q in r.json()["quotes"]] == ["AMD"]


def test_stocks_serves_from_cache_without_fetch(monkeypatch):
    c = _stocks_client(monkeypatch)
    main._cache["AMD"] = {"ts": datetime.utcnow(), "data": _quote("AMD")}

    def boom(syms):
        raise AssertionError("cache is warm; must not hit yfinance")

    monkeypatch.setattr(main, "_fetch_batch", boom)
    r = c.get(STOCKS_URL, params={"symbols": "AMD"})
    assert r.status_code == 200
    assert r.json()["quotes"][0]["last"] == 5.0


def test_stocks_caps_at_16(monkeypatch):
    c = _stocks_client(monkeypatch)
    syms = ",".join(f"S{i}" for i in range(17))
    r = c.get(STOCKS_URL, params={"symbols": syms})
    assert r.status_code == 400


def test_stocks_empty_is_400(monkeypatch):
    c = _stocks_client(monkeypatch)
    r = c.get(STOCKS_URL, params={"symbols": " , , "})
    assert r.status_code == 400


def test_single_stock_endpoint_unchanged(monkeypatch):
    """/stock/{symbol} still returns its full shape incl cached/stale flags."""
    c = _stocks_client(monkeypatch)
    monkeypatch.setattr(main, "_fetch", lambda s: _quote(s))
    r = c.get("/stocks/api/v1/stock/AMD")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AMD" and body["cached"] is False and body["stale"] is False
    assert {"symbol", "closes", "last", "prev", "change", "change_pct"} <= set(body)
