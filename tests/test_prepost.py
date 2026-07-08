"""Offline unit tests for the range=1d&prepost=1 extended-hours path.

These fake the yfinance Ticker so they exercise _fetch_range's shaping (session
vs window bounds, market_state, prev_close) and the helper functions without
hitting the network."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import main

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 8, tzinfo=ET)  # a regular weekday


def _et(hour, minute=0):
    return datetime(2026, 7, 8, hour, minute, tzinfo=ET)


def _epoch(dt):
    return int(dt.timestamp())


class FakeTicker:
    """Minimal stand-in for yf.Ticker: .history(...) returns a preset frame and
    records the prepost kwarg; .history_metadata carries the chart meta."""

    def __init__(self, symbol, frame, meta, calls):
        self._frame = frame
        self.history_metadata = meta
        self._calls = calls

    def history(self, period=None, interval=None, prepost=False):
        self._calls["prepost"] = prepost
        return self._frame


def _install_ticker(monkeypatch, times_et, closes, meta):
    calls = {}
    idx = pd.DatetimeIndex([t.astimezone(timezone.utc) for t in times_et])
    frame = pd.DataFrame({"Close": closes}, index=idx)
    monkeypatch.setattr(main.yf, "Ticker",
                        lambda symbol: FakeTicker(symbol, frame, meta, calls))
    return calls


def _meta():
    return {
        "exchangeTimezoneName": "America/New_York",
        "chartPreviousClose": 516.11,
        "tradingPeriods": [{
            "start": _epoch(_et(9, 30)),
            "end":   _epoch(_et(16, 0)),
        }],
    }


def test_prepost_1d_adds_window_state_prevclose(monkeypatch):
    monkeypatch.setattr(main, "_market_state", lambda *a, **k: "POST")
    calls = _install_ticker(
        monkeypatch,
        [_et(4, 0), _et(9, 30), _et(16, 0), _et(17, 0)],
        [510.0, 512.0, 515.0, 514.0],
        _meta(),
    )
    d = main._fetch_range("AMD", "1d", prepost=True)

    assert calls["prepost"] is True
    assert d["count"] == 4
    # session_* stay the REGULAR bounds
    assert d["session_open"] == _epoch(_et(9, 30))
    assert d["session_close"] == _epoch(_et(16, 0))
    # window_* are the fixed 04:00–20:00 ET extended bounds
    assert d["window_open"] == _epoch(_et(4, 0))
    assert d["window_close"] == _epoch(_et(20, 0))
    assert d["market_state"] == "POST"
    assert d["prev_close"] == 516.11


def test_prepost_omitted_is_regular_session_only(monkeypatch):
    calls = _install_ticker(
        monkeypatch,
        [_et(9, 30), _et(16, 0)],
        [512.0, 515.0],
        _meta(),
    )
    d = main._fetch_range("AMD", "1d", prepost=False)

    assert calls["prepost"] is False
    for k in ("window_open", "window_close", "market_state", "prev_close"):
        assert k not in d, f"{k} must be absent when prepost is omitted"
    assert d["session_open"] == _epoch(_et(9, 30))


def test_prepost_ignored_for_non_1d_range(monkeypatch):
    calls = _install_ticker(
        monkeypatch,
        [_et(9, 30), _et(16, 0)],
        [512.0, 515.0],
        _meta(),
    )
    d = main._fetch_range("AMD", "1w", prepost=True)
    # 1w never opts into prepost, and carries none of the 1d-only extras.
    assert calls["prepost"] is False
    for k in ("window_open", "window_close", "market_state", "prev_close",
              "session_open", "session_close"):
        assert k not in d


def test_prev_close_from_meta_falls_back_and_skips_nan():
    class T:
        history_metadata = {"previousClose": 99.5}
    assert main._prev_close_from_meta(T()) == 99.5

    class TNan:
        history_metadata = {"chartPreviousClose": float("nan"), "previousClose": 42.0}
    assert main._prev_close_from_meta(TNan()) == 42.0

    class TNone:
        history_metadata = {}
    assert main._prev_close_from_meta(TNone()) is None


def test_extended_window_empty_points_uses_current_period():
    class T:
        history_metadata = {
            "exchangeTimezoneName": "America/New_York",
            "currentTradingPeriod": {"regular": {"start": _epoch(_et(9, 30))}},
        }
    win = main._extended_window_for(T(), [])
    assert win == (_epoch(_et(4, 0)), _epoch(_et(20, 0)))


def test_effective_ttl_shortens_only_while_live():
    assert main._effective_ttl("1d", {"market_state": "POST"}) == main.LIVE_RANGE_TTL
    assert main._effective_ttl("1d", {"market_state": "REGULAR"}) == main.LIVE_RANGE_TTL
    assert main._effective_ttl("1d", {"market_state": "CLOSED"}) == main.RANGE_TTL["1d"]
    # default (prepost-omitted) payloads carry no market_state -> unchanged
    assert main._effective_ttl("1d", {}) == main.RANGE_TTL["1d"]
