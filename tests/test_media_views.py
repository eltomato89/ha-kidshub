"""Tests for the image proxy, the TTS cache and their views."""

import asyncio
import hashlib
import io
import tempfile
from unittest.mock import patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub import images as images_module
from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.device import InvalidRequest, TtsFailed

TOKEN = "tok-a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SHA = "a" * 64


def _png(size: int = 1000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), (200, 30, 30, 128)).save(buf, "PNG")
    return buf.getvalue()


async def _setup(hass: HomeAssistant, settings: bool = True):
    hass.config.config_dir = tempfile.mkdtemp(prefix="kidshub-test-")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
        },
    )
    entry.add_to_hass(hass)
    if settings:
        MockConfigEntry(
            domain=DOMAIN,
            unique_id="settings",
            data={"entry_type": "settings"},
            options={
                "tts_engine": "tts.fake",
                "tts_language": "de-DE",
                "tts_options": {"voice": "x"},
            },
        ).add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return hass.data[DOMAIN]


@pytest.fixture
async def source(aiohttp_server):
    """Local image server counting its hits."""
    hits = {"n": 0}

    async def png(request):
        hits["n"] += 1
        return web.Response(body=_png(), content_type="image/png")

    async def redirect(request):
        hits["n"] += 1
        raise web.HTTPFound("http://127.0.0.1:1/x")

    async def huge(request):
        hits["n"] += 1
        return web.Response(body=b"\0" * 2048, content_type="image/png")

    app = web.Application()
    app.router.add_get("/a.png", png)
    app.router.add_get("/r.png", redirect)
    app.router.add_get("/huge.png", huge)
    server = await aiohttp_server(app)
    server.hits = hits
    return server


def _url(source, path: str) -> str:
    return str(source.make_url(path))


async def test_image_requires_token(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    assert (await client.get("/api/kidshub/img?u=http%3A%2F%2Fx&s=64")).status == 401
    bad = {"Authorization": "Bearer nope"}
    assert (
        await client.get("/api/kidshub/img?u=http%3A%2F%2Fx&s=64", headers=bad)
    ).status == 401


async def test_image_scaled(hass: HomeAssistant, hass_client_no_auth, source) -> None:
    data = await _setup(hass)
    url = _url(source, "/a.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "256"}, headers=AUTH)
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    img = Image.open(io.BytesIO(await resp.read()))
    assert img.format == "JPEG"
    assert img.width <= 256 and img.height <= 256
    # second request is served from the LRU cache
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "256"}, headers=AUTH)
    assert resp.status == 200
    assert source.hits["n"] == 1


async def test_image_size_clamped(
    hass: HomeAssistant, hass_client_no_auth, source
) -> None:
    data = await _setup(hass)
    url = _url(source, "/a.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "5000"}, headers=AUTH)
    img = Image.open(io.BytesIO(await resp.read()))
    assert max(img.size) == 512
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "abc"}, headers=AUTH)
    assert resp.status == 400


async def test_image_unknown_url_no_outbound_request(
    hass: HomeAssistant, hass_client_no_auth, source
) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    resp = await client.get(
        "/api/kidshub/img", params={"u": _url(source, "/a.png"), "s": "64"}, headers=AUTH
    )
    assert resp.status == 404
    assert source.hits["n"] == 0


async def test_image_non_http_scheme(hass: HomeAssistant, hass_client_no_auth) -> None:
    data = await _setup(hass)
    data.images.add("file:///etc/passwd")
    client = await hass_client_no_auth()
    resp = await client.get(
        "/api/kidshub/img", params={"u": "file:///etc/passwd", "s": "64"}, headers=AUTH
    )
    assert resp.status == 404


async def test_image_redirect_not_followed(
    hass: HomeAssistant, hass_client_no_auth, source
) -> None:
    data = await _setup(hass)
    url = _url(source, "/r.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH)
    assert resp.status == 502
    assert source.hits["n"] == 1


async def test_image_too_large(
    hass: HomeAssistant, hass_client_no_auth, source, monkeypatch
) -> None:
    monkeypatch.setattr(images_module, "MAX_BYTES", 1024)
    data = await _setup(hass)
    url = _url(source, "/huge.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH)
    assert resp.status == 502


async def test_image_lru_bounded(hass: HomeAssistant) -> None:
    cache = images_module.ScaledCache(maxsize=2)
    cache.put(("a", 64), b"1")
    cache.put(("b", 64), b"2")
    cache.put(("c", 64), b"3")
    assert cache.get(("a", 64)) is None
    assert cache.get(("c", 64)) == b"3"


# -- TTS ------------------------------------------------------------------


def _patch_tts(result=("mp3", b"ID3audio"), delay: float = 0):
    calls = []

    async def audio(hass, media_source_id):
        calls.append(media_source_id)
        if delay:
            await asyncio.sleep(delay)
        if isinstance(result, Exception):
            raise result
        return result

    return calls, (
        patch(
            "homeassistant.components.tts.async_get_media_source_audio", audio
        ),
        patch(
            "homeassistant.components.tts.generate_media_source_id",
            lambda hass, text, engine, language, options: (
                f"media-source://tts/{engine}?m={text}&l={language}"
            ),
        ),
    )


async def _tts(device, text):
    return await device.handle_request({"type": "tts", "id": 1, "text": text})


async def test_tts_cached(hass: HomeAssistant, hass_client_no_auth) -> None:
    data = await _setup(hass)
    device = data.devices["dev-a"]
    calls, (p1, p2) = _patch_tts()
    with p1, p2:
        first = await _tts(device, "Guten Morgen Paul")
        second = await _tts(device, "Guten Morgen Paul")
    assert first == second
    assert first["url"].startswith("/api/kidshub/tts/")
    assert first["url"].endswith(".mp3")
    assert len(calls) == 1
    client = await hass_client_no_auth()
    resp = await client.get(first["url"], headers=AUTH)
    assert resp.status == 200
    assert resp.content_type == "audio/mpeg"
    assert await resp.read() == b"ID3audio"
    assert (await client.get(first["url"])).status == 401
    assert (
        await client.get(first["url"], headers={"Authorization": "Bearer x"})
    ).status == 401


async def test_tts_key_per_protocol(hass: HomeAssistant) -> None:
    data = await _setup(hass)
    device = data.devices["dev-a"]
    _, (p1, p2) = _patch_tts()
    with p1, p2:
        res = await _tts(device, "Hallo")
    key = hashlib.sha256(
        '|'.join(["tts.fake", "de-DE", '{"voice":"x"}', "Hallo"]).encode()
    ).hexdigest()
    assert res["url"] == f"/api/kidshub/tts/{key}.mp3"
    assert (hass.config.path("kidshub_tts") + f"/{key}.mp3")


async def test_tts_concurrent_single_generation(hass: HomeAssistant) -> None:
    data = await _setup(hass)
    device = data.devices["dev-a"]
    calls, (p1, p2) = _patch_tts(delay=0.1)
    with p1, p2:
        a, b = await asyncio.gather(_tts(device, "Hi"), _tts(device, "Hi"))
    assert a == b
    assert len(calls) == 1


async def test_tts_path_traversal(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/tts/..%2Fsecrets.yaml", headers=AUTH)
    # HA's security filter answers 400 before the view; either way no file is served
    assert resp.status in (400, 404)
    for name in ("..", f"{SHA}.txt", f"{'A' * 64}.mp3", f"{SHA}.mp3", "x.mp3"):
        resp = await client.get(f"/api/kidshub/tts/{name}", headers=AUTH)
        assert resp.status == 404


async def test_tts_no_engine(hass: HomeAssistant) -> None:
    data = await _setup(hass, settings=False)
    with pytest.raises(TtsFailed):
        await _tts(data.devices["dev-a"], "Hallo")


async def test_tts_engine_error(hass: HomeAssistant) -> None:
    data = await _setup(hass)
    _, (p1, p2) = _patch_tts(result=ValueError("boom"))
    with p1, p2, pytest.raises(TtsFailed):
        await _tts(data.devices["dev-a"], "Hallo")


async def test_tts_timeout(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.kidshub import tts_cache

    monkeypatch.setattr(tts_cache, "TTS_TIMEOUT", 0.05)
    data = await _setup(hass)
    _, (p1, p2) = _patch_tts(delay=1)
    with p1, p2, pytest.raises(TtsFailed):
        await _tts(data.devices["dev-a"], "Hallo")


async def test_tts_unsupported_extension(hass: HomeAssistant) -> None:
    data = await _setup(hass)
    _, (p1, p2) = _patch_tts(result=("../x", b"data"))
    with p1, p2, pytest.raises(TtsFailed):
        await _tts(data.devices["dev-a"], "Hallo")


@pytest.mark.parametrize("text", ["", 5, None, "x" * 201])
async def test_tts_invalid_text(hass: HomeAssistant, text) -> None:
    data = await _setup(hass)
    with pytest.raises(InvalidRequest):
        await _tts(data.devices["dev-a"], text)


async def test_tts_error_code_over_ws(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, settings=False)
    client = await hass_client_no_auth()
    ws = await client.ws_connect("/api/kidshub/ws")
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": TOKEN, "app_version": "1"}
    )
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"type": "tts", "id": 7, "text": "Hallo"})
    res = await ws.receive_json()
    assert res["error"]["code"] == "tts_failed"
    await ws.close()
