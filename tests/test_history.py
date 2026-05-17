"""Smoke test for /history/{symbol}?range=… — hits the locally running
service so it exercises the real yfinance path. Service must be up on
STOCK_API_BASE (defaults to http://127.0.0.1:8001/stocks/api/v1)."""
import os

import pytest
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("STOCK_API_BASE", "http://127.0.0.1:8001/stocks/api/v1")
TOKEN = os.getenv("API_SECRET", "")
SYMBOL = os.getenv("TEST_SYMBOL", "AMD")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "stock-api-tests/1.0",
}

RANGES: list[tuple[str, str]] = [
    ("1d",  "intraday"),
    ("5d",  "intraday"),
    ("1w",  "intraday"),
    ("1mo", "daily"),
    ("6mo", "daily"),
    ("1y",  "daily"),
    ("max", "daily"),
]


@pytest.mark.parametrize("range_value,expected_interval", RANGES)
def test_history_range(range_value: str, expected_interval: str) -> None:
    r = requests.get(
        f"{BASE}/history/{SYMBOL}",
        params={"range": range_value},
        headers=HEADERS,
        timeout=30,
    )
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["symbol"] == SYMBOL
    assert body["range"] == range_value
    assert body["interval"] == expected_interval
    assert isinstance(body["points"], list)
    assert len(body["points"]) > 0, f"expected non-empty points for range={range_value}"

    sample = body["points"][0]
    assert isinstance(sample["ts"], int), "ts should be epoch seconds (int)"
    assert isinstance(sample["last"], (int, float))


def test_history_invalid_range() -> None:
    r = requests.get(
        f"{BASE}/history/{SYMBOL}",
        params={"range": "bogus"},
        headers=HEADERS,
        timeout=10,
    )
    assert r.status_code == 422


def test_history_legacy_days_still_works() -> None:
    """?days=N must remain ISO-string ts and live under the days key."""
    r = requests.get(
        f"{BASE}/history/{SYMBOL}",
        params={"days": 1},
        headers=HEADERS,
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "days" in body
    assert "range" not in body
    if body["points"]:
        assert isinstance(body["points"][0]["ts"], str), "legacy path keeps ISO ts"
