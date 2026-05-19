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


def _image_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _alpha_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    alpha = img.convert("RGBA").getchannel("A")
    mask = alpha.point(lambda a: 255 if a > 8 else 0)
    bbox = mask.getbbox()
    assert bbox is not None
    return bbox


def _normalized_img(data: bytes) -> Image.Image:
    return Image.open(BytesIO(main._normalize_to_png(data))).convert("RGBA")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "API_SECRET", "")
    monkeypatch.setattr(main, "LOGO_CACHE_DIR", tmp_path)
    monkeypatch.setattr(main, "_logo_overrides", {})
    monkeypatch.setattr(main, "_logo_locks", {})
    return TestClient(main.app)


def test_normalize_crops_transparent_padding_and_enlarges_logo():
    src = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    mark = Image.new("RGBA", (40, 40), (0, 128, 255, 255))
    src.paste(mark, (44, 44), mark)

    out = _normalized_img(_image_bytes(src))
    bbox = _alpha_bbox(out)

    assert out.mode == "RGBA"
    assert out.size == (64, 64)
    assert bbox == (2, 2, 62, 62)


def test_normalize_preserves_wide_logo_aspect_ratio_and_centers():
    src = Image.new("RGBA", (160, 40), (255, 80, 0, 255))

    out = _normalized_img(_image_bytes(src))
    left, top, right, bottom = _alpha_bbox(out)

    assert (right - left, bottom - top) == (60, 15)
    assert left == 2
    assert right == 62
    assert abs(top - (64 - bottom)) <= 1


def test_normalize_preserves_tall_logo_aspect_ratio_and_centers():
    src = Image.new("RGBA", (40, 160), (0, 160, 80, 255))

    out = _normalized_img(_image_bytes(src))
    left, top, right, bottom = _alpha_bbox(out)

    assert (right - left, bottom - top) == (15, 60)
    assert top == 2
    assert bottom == 62
    assert abs(left - (64 - right)) <= 1


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


def test_test_mode_returns_synthetic_png(client, monkeypatch, tmp_path):
    def must_not_be_called(symbol: str) -> bytes | None:
        raise AssertionError(f"resolver should not be called in test mode; got {symbol}")

    monkeypatch.setattr(main, "_resolve_logo_sync", must_not_be_called)
    r = client.get("/stocks/api/v1/logo/ASML", params={"test": 1})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    img = Image.open(BytesIO(r.content))
    assert img.mode == "RGBA"
    assert img.size == (64, 64)
    pixels = img.load()
    # corner = red background, center = blue dot, diagonal mid = green stripe
    assert pixels[0, 0] == (255, 0, 0, 255)
    assert pixels[32, 32] == (0, 0, 255, 255)
    assert pixels[16, 16] == (0, 255, 0, 255)
    # test mode must not write to cache
    assert not (tmp_path / "ASML.png").exists()


def test_miss_marker_short_circuits_retries(client, monkeypatch, tmp_path):
    calls: list[str] = []

    def fake(sym: str) -> bytes | None:
        calls.append(sym)
        return None

    monkeypatch.setattr(main, "_resolve_logo_sync", fake)
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert calls == ["WAT"], "second hit must read miss marker, not re-resolve"
