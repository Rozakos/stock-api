"""Offline tests for the CoinGecko crypto branch (quotes, history, logo).
yfinance and CoinGecko HTTP are monkeypatched — no network or running service."""
from PIL import Image

import main


def _market(symbol="btc", cid="bitcoin", price=100.0, pct=10.0, rank=1, spark=None):
    return {
        "id": cid, "symbol": symbol, "current_price": price,
        "price_change_percentage_24h": pct, "market_cap_rank": rank,
        "sparkline_in_7d": {"price": spark if spark is not None else [80, 85, 90, 95, 100]},
        "image": "https://coin-images.coingecko.com/x.png",
    }


def test_is_crypto():
    assert main._is_crypto("BTC-USD") and main._is_crypto("ETH-USD")
    assert not main._is_crypto("AMD") and not main._is_crypto("SPCX")


def test_is_allowed_crypto_bypasses_universe(monkeypatch):
    monkeypatch.setattr(main, "_symbols", {"AMD"})
    assert main._is_allowed("DOGE-USD") is True   # crypto always allowed
    assert main._is_allowed("AMD") is True
    assert main._is_allowed("ZZZZ") is False


def test_round_price_keeps_subdollar_precision():
    assert main._round_price(66514.123456) == 66514.1235
    assert main._round_price(0.000012345678) == round(0.000012345678, 8)


def test_build_crypto_quote_shape():
    q = main._build_crypto_quote("BTC-USD", _market(price=110.0, pct=10.0))
    assert q["symbol"] == "BTC-USD"
    assert q["last"] == 110.0 and q["change_pct"] == 10.0
    assert q["prev"] == 100.0 and q["change"] == 10.0   # 110 / 1.10 = 100
    assert q["closes"] == [80, 85, 90, 95, 100]
    assert q["market_state"] == "REGULAR"
    assert q["pre_market"] is None and q["post_market"] is None


def test_fetch_crypto_batch_builds_and_caches_id(monkeypatch):
    main._crypto_ids.clear()
    data = [_market("btc", "bitcoin", 100.0, 5.0, 1),
            _market("eth", "ethereum", 50.0, 2.0, 2)]
    monkeypatch.setattr(main, "_coingecko_get", lambda path: data)
    out = main._fetch_crypto_batch(["BTC-USD", "ETH-USD"])
    assert set(out) == {"BTC-USD", "ETH-USD"}
    assert out["BTC-USD"]["last"] == 100.0
    assert main._crypto_ids["btc"] == "bitcoin"   # cached for history/logo


def test_fetch_crypto_batch_picks_best_market_cap_rank(monkeypatch):
    main._crypto_ids.clear()
    data = [_market("uni", "unicorn-token", 1.0, 0.0, 5000),
            _market("uni", "uniswap", 9.0, 1.0, 20)]
    monkeypatch.setattr(main, "_coingecko_get", lambda path: data)
    out = main._fetch_crypto_batch(["UNI-USD"])
    assert out["UNI-USD"]["last"] == 9.0          # lowest rank wins


def test_fetch_batch_routes_crypto_and_equity(monkeypatch):
    import pandas as pd
    monkeypatch.setattr(main, "_fetch_crypto_batch",
                        lambda syms: {s: {"symbol": s, "last": 1.0} for s in syms})
    df = pd.DataFrame({("AMD", "Close"): [10.0, 11.0, 12.0]})
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: df)
    monkeypatch.setattr(main, "_attach_extended", lambda q: None)
    out = main._fetch_batch(["AMD", "BTC-USD"])
    assert set(out) == {"AMD", "BTC-USD"}
    assert out["AMD"]["last"] == 12.0 and out["BTC-USD"]["last"] == 1.0


def test_fetch_range_crypto_shape(monkeypatch):
    monkeypatch.setattr(main, "_coingecko_id", lambda s: "bitcoin")
    monkeypatch.setattr(main, "_coingecko_get",
                        lambda path: {"prices": [[1781000000000, 100.0], [1781003600000, 101.0]]})
    d = main._fetch_range("BTC-USD", "1d")
    assert d["interval"] == "intraday" and d["count"] == 2
    assert d["points"][0] == {"ts": 1781000000, "last": 100.0}
    assert "session_open" not in d and "session_close" not in d   # 24/7, no session


def test_fetch_range_crypto_clamps_long_ranges(monkeypatch):
    monkeypatch.setattr(main, "_coingecko_id", lambda s: "bitcoin")
    captured = {}

    def fake_get(path):
        captured["path"] = path
        return {"prices": []}

    monkeypatch.setattr(main, "_coingecko_get", fake_get)
    main._fetch_range("BTC-USD", "max")
    assert "days=365" in captured["path"]   # free-tier 365-day cap
    main._fetch_range("BTC-USD", "5y")
    assert "days=365" in captured["path"]


def test_crypto_logo_preferred_over_equity_path(monkeypatch):
    monkeypatch.setattr(main, "_logo_overrides", {})
    sentinel = Image.new("RGBA", (250, 250), (1, 2, 3, 255))
    monkeypatch.setattr(main, "_coingecko_logo_image", lambda s: sentinel)

    def boom(s):
        raise AssertionError("equity logo path must not run for crypto")

    monkeypatch.setattr(main, "_ticker_domain", boom)
    assert main._best_source_image("BTC-USD") is sentinel
