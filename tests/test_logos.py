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
from PIL import Image, ImageDraw

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


def _normalized_img(data: bytes, size: int = 64) -> Image.Image:
    src = Image.open(BytesIO(data)).convert("RGBA")
    return Image.open(BytesIO(main._fit_square(src, size))).convert("RGBA")


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

    monkeypatch.setattr(main, "_resolve_master_sync", fake_resolve)
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
        "_resolve_master_sync",
        lambda s: called.append(s) or None,
    )
    r = client.get("/stocks/api/v1/logo/AAPL")
    assert r.status_code == 200
    assert called == [], "should not invoke resolver for cached logo"
    # Served size is now derived from the (high-res) master, not the raw master
    # bytes; the point is no re-resolution happened.
    img = Image.open(BytesIO(r.content))
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


def test_missing_logo_returns_404_json(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_resolve_master_sync", lambda s: None)
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


def test_size_param_returns_resized_png(client, tmp_path):
    (tmp_path / "AAPL.png").write_bytes(_png_bytes((10, 200, 50, 255), size=64))
    r = client.get("/stocks/api/v1/logo/AAPL", params={"size": 48})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert "max-age=2592000" in r.headers["cache-control"]
    img = Image.open(BytesIO(r.content))
    assert img.size == (48, 48)
    assert img.mode == "RGBA"


def test_size_64_matches_default(client, tmp_path):
    raw = _png_bytes((10, 200, 50, 255), size=64)
    (tmp_path / "AAPL.png").write_bytes(raw)
    default = client.get("/stocks/api/v1/logo/AAPL")
    sized = client.get("/stocks/api/v1/logo/AAPL", params={"size": 64})
    assert default.status_code == 200
    assert sized.status_code == 200
    # default size is 64, so both paths render the same 64px variant
    assert default.content == sized.content
    img = Image.open(BytesIO(default.content))
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


def test_size_rejects_unsupported_values(client, tmp_path):
    (tmp_path / "AAPL.png").write_bytes(_png_bytes(size=64))
    r = client.get("/stocks/api/v1/logo/AAPL", params={"size": 96})
    assert r.status_code == 400
    assert "size" in r.json()["detail"].lower()


def test_test_mode_returns_synthetic_png(client, monkeypatch, tmp_path):
    def must_not_be_called(symbol: str) -> bytes | None:
        raise AssertionError(f"resolver should not be called in test mode; got {symbol}")

    monkeypatch.setattr(main, "_resolve_master_sync", must_not_be_called)
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

    monkeypatch.setattr(main, "_resolve_master_sync", fake)
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert client.get("/stocks/api/v1/logo/WAT").status_code == 404
    assert calls == ["WAT"], "second hit must read miss marker, not re-resolve"


# --- quality of the master / resize / fallback -----------------------------

def _semi_alpha_dark_count(img: Image.Image, rgb_floor: int = 180) -> tuple[int, int]:
    """(semi-transparent pixels, of those that bled darker than rgb_floor)."""
    px = img.convert("RGBA").load()
    semi = dark = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if 0 < a < 250:
                semi += 1
                if min(r, g, b) < rgb_floor:
                    dark += 1
    return semi, dark


def test_master_keeps_high_res_source(monkeypatch):
    big = Image.new("RGBA", (200, 200), (12, 200, 90, 255))
    monkeypatch.setattr(main, "_best_source_image", lambda s: big)
    master = Image.open(BytesIO(main._resolve_master_sync("AAA"))).convert("RGBA")
    # source <= 256, so it's preserved (not crushed down to 64 like before)
    assert max(master.size) == 200


def test_master_caps_at_256(monkeypatch):
    huge = Image.new("RGBA", (512, 512), (200, 50, 50, 255))
    monkeypatch.setattr(main, "_best_source_image", lambda s: huge)
    master = Image.open(BytesIO(main._resolve_master_sync("AAA"))).convert("RGBA")
    assert max(master.size) == main.LOGO_MASTER_SIZE  # 256


def test_best_source_picks_largest(monkeypatch):
    small = Image.new("RGBA", (48, 48), (0, 0, 0, 255))
    large = Image.new("RGBA", (192, 192), (0, 0, 0, 255))
    monkeypatch.setattr(main, "_logo_overrides", {})
    monkeypatch.setattr(main, "_ticker_domain", lambda s: "example.com")
    seq = iter([small, large])
    monkeypatch.setattr(main, "_fetch_image", lambda url: next(seq))
    best = main._best_source_image("AAA")
    assert max(best.size) == 192  # largest native wins, not first-hit


def test_premultiplied_resize_has_no_dark_halo():
    # opaque white disc on transparent (RGB 0). Downscaling creates
    # semi-transparent edge pixels; with premultiplied alpha they must stay
    # white instead of bleeding toward the black transparent background.
    src = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(src).ellipse([8, 8, 192, 192], fill=(255, 255, 255, 255))
    out = Image.open(BytesIO(main._fit_square(src, 48))).convert("RGBA")
    semi, dark = _semi_alpha_dark_count(out)
    assert semi > 0, "expected antialiased edge pixels from the downscale"
    assert dark == 0, f"{dark}/{semi} edge pixels bled toward black (halo)"


def test_monogram_when_source_too_small(monkeypatch):
    tiny = Image.new("RGBA", (16, 16), (255, 255, 255, 255))
    monkeypatch.setattr(main, "_best_source_image", lambda s: tiny)
    master = Image.open(BytesIO(main._resolve_master_sync("XY"))).convert("RGBA")
    assert master.size == (main.LOGO_MASTER_SIZE, main.LOGO_MASTER_SIZE)
    # the monogram tile has opaque pixels (a real drawn mark, not blank)
    assert master.getextrema()[3][1] == 255


def test_size_variant_is_cached_per_size(client, tmp_path, monkeypatch):
    (tmp_path / "AAPL.png").write_bytes(_png_bytes((10, 200, 50, 255), size=200))
    r1 = client.get("/stocks/api/v1/logo/AAPL", params={"size": 48})
    assert r1.status_code == 200
    variant = tmp_path / "AAPL.48.png"
    assert variant.exists(), "per-(symbol,size) variant should be cached on disk"
    # second request serves the cached variant verbatim, single-digit KB
    r2 = client.get("/stocks/api/v1/logo/AAPL", params={"size": 48})
    assert r2.content == variant.read_bytes()
    assert len(r2.content) < 8000
