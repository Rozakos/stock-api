"""Offline unit tests for the quote payload builder, the batch parser, and
the active-symbol registry that drives the background quote poller. No network
or running service required — yfinance is monkeypatched where needed."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

import main


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
