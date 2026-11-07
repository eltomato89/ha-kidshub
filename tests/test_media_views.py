"""Tests for the image proxy, the TTS cache and their views."""

import asyncio
import hashlib
import io
import os
import tempfile
from unittest.mock import patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from PIL import Image, ImageFile
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

    async def chunked(request):
        hits["n"] += 1
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(4):
            await resp.write(b"\0" * 512)
        return resp

    async def bomb(request):
        hits["n"] += 1
        buf = io.BytesIO()
        Image.new("L", (6000, 6000)).save(buf, "PNG", compress_level=9)
        return web.Response(body=buf.getvalue(), content_type="image/png")

    async def eps(request):
        hits["n"] += 1
        return web.Response(body=b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n")

    async def to(request):
        """Redirect to the ``to`` query parameter (absolute or relative)."""
        hits["n"] += 1
        raise web.HTTPMovedPermanently(request.query["to"])

    async def hop(request):
        """``/hop/<n>`` redirects n more times on the same host, then to /a.png."""
        hits["n"] += 1
        n = int(request.match_info["n"])
        raise web.HTTPFound(f"/hop/{n - 1}" if n > 1 else "/a.png")

    app = web.Application()
    app.router.add_get("/to", to)
    app.router.add_get("/hop/{n}", hop)
    app.router.add_get("/chunked.png", chunked)
    app.router.add_get("/bomb.png", bomb)
    app.router.add_get("/x.eps", eps)
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
    # the app keeps covers in its disk cache for a day
    assert resp.headers["Cache-Control"] == "private, max-age=86400"
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


async def test_image_redirect_to_other_loopback_port_rejected(
    hass: HomeAssistant, hass_client_no_auth, source
) -> None:
    data = await _setup(hass)
    url = _url(source, "/r.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH)
    assert resp.status == 502
    assert source.hits["n"] == 1


@pytest.fixture
async def other(aiohttp_server):
    """A second image server: another host:port than the registered URL."""
    hits = {"n": 0}

    async def png(request):
        hits["n"] += 1
        return web.Response(body=_png(), content_type="image/png")

    app = web.Application()
    app.router.add_get("/a.png", png)
    server = await aiohttp_server(app)
    server.hits = hits
    return server


def _resolve_as(monkeypatch, address: str) -> list[str]:
    """Make the SSRF guard resolve every redirect host to ``address``."""
    hosts: list[str] = []

    async def resolve(hass, host, port):
        hosts.append(host)
        return [address]

    monkeypatch.setattr(images_module, "_resolve", resolve)
    return hosts


async def _img(hass_client_no_auth, url: str):
    client = await hass_client_no_auth()
    return await client.get(
        "/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH
    )


async def test_image_redirect_to_public_host(
    hass: HomeAssistant, hass_client_no_auth, source, other, monkeypatch
) -> None:
    hosts = _resolve_as(monkeypatch, "93.184.216.34")
    data = await _setup(hass)
    url = _url(source, "/to") + "?to=" + _url(other, "/a.png")
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    assert other.hits["n"] == 1
    assert hosts == ["127.0.0.1"]  # only the redirect hop is resolved


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.1.20", "169.254.169.254", "::1", "0.0.0.0",
     "224.0.0.1", "::ffff:10.0.0.1"],
)
async def test_image_redirect_to_private_rejected(
    hass: HomeAssistant, hass_client_no_auth, source, other, monkeypatch, address
) -> None:
    _resolve_as(monkeypatch, address)
    data = await _setup(hass)
    url = _url(source, "/to") + "?to=" + _url(other, "/a.png")
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 502
    assert other.hits["n"] == 0  # never fetched


async def test_image_redirect_to_loopback_real_resolution(
    hass: HomeAssistant, hass_client_no_auth, source, other
) -> None:
    """Without patching, another port on 127.0.0.1 resolves to loopback."""
    data = await _setup(hass)
    url = _url(source, "/to") + "?to=" + _url(other, "/a.png")
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 502
    assert other.hits["n"] == 0


@pytest.mark.parametrize(("hops", "status"), [(3, 200), (4, 502)])
async def test_image_redirect_hop_limit(
    hass: HomeAssistant, hass_client_no_auth, source, hops, status
) -> None:
    """Same-host redirects on the LAN are followed, at most 3 of them."""
    data = await _setup(hass)
    url = _url(source, f"/hop/{hops}")  # /hop/n answers with n redirects in total
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == status


@pytest.mark.parametrize("target", ["ftp://example.com/a.png", "file:///etc/passwd"])
async def test_image_redirect_non_http_rejected(
    hass: HomeAssistant, hass_client_no_auth, source, monkeypatch, target
) -> None:
    hosts = _resolve_as(monkeypatch, "93.184.216.34")
    data = await _setup(hass)
    url = _url(source, "/to") + "?to=" + target
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 502
    assert hosts == []


async def test_image_relative_redirect_same_host(
    hass: HomeAssistant, hass_client_no_auth, source, monkeypatch
) -> None:
    hosts = _resolve_as(monkeypatch, "10.0.0.1")  # would be rejected if checked
    data = await _setup(hass)
    url = _url(source, "/to") + "?to=/a.png"
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 200
    assert hosts == []  # same host:port as the registered URL: not resolved


async def test_image_private_registered_url_served(
    hass: HomeAssistant, hass_client_no_auth, source, monkeypatch
) -> None:
    """The registered URL itself may be private (MA's /imageproxy on the LAN)."""
    hosts = _resolve_as(monkeypatch, "192.168.1.5")
    data = await _setup(hass)
    url = _url(source, "/a.png")
    data.images.add(url)
    resp = await _img(hass_client_no_auth, url)
    assert resp.status == 200
    assert hosts == []


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


async def test_image_chunked_too_large(
    hass: HomeAssistant, hass_client_no_auth, source, monkeypatch
) -> None:
    monkeypatch.setattr(images_module, "MAX_BYTES", 1024)
    data = await _setup(hass)
    url = _url(source, "/chunked.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    resp = await client.get("/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH)
    assert resp.status == 502


@pytest.mark.parametrize("path", ["/bomb.png", "/x.eps"])
async def test_image_rejected_without_decode(
    hass: HomeAssistant, hass_client_no_auth, source, path
) -> None:
    data = await _setup(hass)
    url = _url(source, path)
    data.images.add(url)
    client = await hass_client_no_auth()
    with patch.object(ImageFile.ImageFile, "load", autospec=True) as load:
        resp = await client.get(
            "/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH
        )
    assert resp.status == 502
    load.assert_not_called()  # rejected from the header, never fully decoded


async def test_image_concurrent_single_fetch(
    hass: HomeAssistant, hass_client_no_auth, source
) -> None:
    data = await _setup(hass)
    url = _url(source, "/a.png")
    data.images.add(url)
    client = await hass_client_no_auth()
    rs = await asyncio.gather(
        *[
            client.get("/api/kidshub/img", params={"u": url, "s": "64"}, headers=AUTH)
            for _ in range(4)
        ]
    )
    assert [r.status for r in rs] == [200] * 4
    assert source.hits["n"] == 1


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
    assert resp.headers["Cache-Control"] == "private, max-age=86400"
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
    assert os.path.isfile(hass.config.path("kidshub_tts") + f"/{key}.mp3")


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
    await ws.receive_json()  # now_playing
    await ws.send_json({"type": "tts", "id": 7, "text": "Hallo"})
    res = await ws.receive_json()
    assert res["error"]["code"] == "tts_failed"
    await ws.close()
