"""Offline tests for the /logo and /logos endpoints.

Uses FastAPI's TestClient with the logo resolver monkeypatched out so the
tests never touch the network or yfinance. Lifespan is intentionally NOT
entered (no `with TestClient(...)` block), so the background refresh and
history loops don't run.
"""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main


def _png_bytes(color: tuple[int, int, int, int] = (0, 128, 255, 255), size: int = 64) -> bytes:
    img = Image.new("RGBA", (size, size), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "API_SECRET", "")
    monkeypatch.setattr(main, "LOGO_CACHE_DIR", tmp_path)
    monkeypatch.setattr(main, "_logo_overrides", {})
    monkeypatch.setattr(main, "_logo_locks", {})
    return TestClient(main.app)


def test_symbol_is_uppercased(client, monkeypatch, tmp_path):
    seen: list[str] = []

    def fake_resolve(sym: str) -> bytes:
        seen.append(sym)
        return _png_bytes()

    monkeypatch.setattr(main, "_resolve_logo_sync", fake_resolve)
    r = client.get("/stocks/api/v1/logo/ionq")
    assert r.status_code == 200, r.text
    assert seen == ["IONQ"]
    assert r.headers["content-type"] == "image/png"
    assert "max-age=2592000" in r.headers["cache-control"]
    assert (tmp_path / "IONQ.png").exists()


def test_cached_logo_served_without_refetching(client, monkeypatch, tmp_path):
    (tmp_path / "AAPL.png").write_bytes(_png_bytes((255, 0, 0, 255)))

    called: list[str] = []
    monkeypatch.setattr(
        main,
        "_resolve_logo_sync",
        lambda s: called.append(s) or None,
    )
    r = client.get("/stocks/api/v1/logo/AAPL")
    assert r.status_code == 200
    assert called == [], "should not invoke resolver for cached logo"
    assert r.content == (tmp_path / "AAPL.png").read_bytes()


def test_missing_logo_returns_404_json(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_resolve_logo_sync", lambda s: None)
    r = client.get("/stocks/api/v1/logo/NOPE")
    assert r.status_code == 404
    body = r.json()
    assert "no logo" in body["detail"].lower()
    # Failure marker should be written
    assert (tmp_path / "NOPE.miss.json").exists()


def test_override_bypasses_yfinance(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_logo_overrides", {"IONQ": "https://example.test/ionq.png"})
    fetched: list[str] = []

    def fake_http_get(url: str, timeout: int = 10) -> bytes:
        fetched.append(url)
        return _png_bytes((10, 20, 30, 255))

    def must_not_be_called(symbol: str) -> str | None:
        raise AssertionError(f"yfinance should not be consulted; called for {symbol}")

    monkeypatch.setattr(main, "_http_get", fake_http_get)
    monkeypatch.setattr(main, "_ticker_domain", must_not_be_called)

    r = client.get("/stocks/api/v1/logo/IONQ")
    assert r.status_code == 200, r.text
    assert fetched == ["https://example.test/ionq.png"]
    assert (tmp_path / "IONQ.png").exists()


def test_manifest_reports_cache_status(client, tmp_path):
    (tmp_path / "AAPL.png").write_bytes(_png_bytes())
    r = client.get("/stocks/api/v1/logos", params={"symbols": "aapl,IONQ"})
    assert r.status_code == 200
    body = r.json()
    assert body["logos"]["AAPL"] == {
        "url": "/stocks/api/v1/logo/AAPL",
        "cached": True,
    }
    assert body["logos"]["IONQ"] == {
        "url": "/stocks/api/v1/logo/IONQ",
        "cached": False,
    }


def test_manifest_skips_empties_and_dedupes(client, tmp_path):
    r = client.get("/stocks/api/v1/logos", params={"symbols": "AAPL,,aapl, ,NVDA"})
    assert r.status_code == 200
    logos = r.json()["logos"]
    assert set(logos.keys()) == {"AAPL", "NVDA"}


def test_miss_marker_short_circuits_retries(client, monkeypatch, tmp_path):
    calls: list[str] = []

    def fake(sym: str) -> bytes | None:
        calls.append(sym)
        return None

    monkeypatch.setattr(main, "_resolve_logo_sync", fake)
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert calls == ["WAT"], "second hit must read miss marker, not re-resolve"
