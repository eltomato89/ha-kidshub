"""Image proxy: download, scale and cache cover images."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image

MIN_SIZE = 64
MAX_SIZE = 512
MAX_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 10.0  # whole redirect chain
MAX_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
CACHE_SIZE = 200
MAX_PIXELS = 25_000_000
MAX_DECODES = 2
FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]
JPEG_QUALITY = 80
_CACHE_KEY = "kidshub_image_cache"


class ImageError(Exception):
    """The image could not be fetched or processed."""


class ScaledCache:
    """LRU cache of resized image bytes keyed by ``(url, size)``."""

    def __init__(self, maxsize: int = CACHE_SIZE) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self.decodes = asyncio.Semaphore(MAX_DECODES)
        self.inflight: dict[tuple[str, int], asyncio.Task[bytes]] = {}

    def get(self, key: tuple[str, int]) -> bytes | None:
        """Return the cached bytes and mark them most recently used."""
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: tuple[str, int], value: bytes) -> None:
        """Store ``value``, evicting the least recently used entries."""
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def clamp_size(size: int) -> int:
    """Limit ``size`` to the supported range."""
    return max(MIN_SIZE, min(MAX_SIZE, size))


def is_http_url(url: str) -> bool:
    """Return whether ``url`` is an absolute http(s) URL."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


WIKIMEDIA_HOST = "upload.wikimedia.org"
WIKIMEDIA_THUMB_WIDTH = 500  # Wikimedia only renders its standard thumbnail widths


def raster_url(url: str) -> str:
    """Return a raster (PNG) URL for an SVG on Wikimedia, else ``url`` unchanged.

    Pillow cannot decode SVG; Radio Browser station logos are often SVGs on
    Wikimedia Commons, which renders them as PNG thumbnails:
    ``/wikipedia/<project>/<a>/<ab>/<name>.svg`` ->
    ``/wikipedia/<project>/thumb/<a>/<ab>/<name>.svg/500px-<name>.svg.png``.
    """
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    if (
        (parsed.hostname or "").lower() != WIKIMEDIA_HOST
        or not parsed.path.lower().endswith(".svg")
        or parsed.query
        or len(parts) != 6
        or parts[:2] != ["", "wikipedia"]
    ):
        return url
    _, wiki, project, a, ab, name = parts
    path = f"/{wiki}/{project}/thumb/{a}/{ab}/{name}/{WIKIMEDIA_THUMB_WIDTH}px-{name}.png"
    return parsed._replace(scheme="https", path=path).geturl()


def _origin(url: str) -> tuple[str, int]:
    """Return ``(host, port)`` of an http(s) URL, with the scheme's default port."""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (parsed.hostname or "").lower(), port


def _is_public(address: str) -> bool:
    """Return whether ``address`` is a globally routable unicast IP."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(hass: HomeAssistant, host: str, port: int) -> list[str]:
    """Return all addresses ``host`` resolves to (non-blocking)."""
    infos = await hass.loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _check_hop(hass: HomeAssistant, url: str, original: tuple[str, int]) -> None:
    """Reject a redirect target that is not http(s) or points into a private net.

    The host:port of the registered URL is exempt (MA's image proxy on the LAN).
    """
    if not is_http_url(url):
        raise ImageError("redirect to a non-http(s) URL")
    host, port = _origin(url)
    if (host, port) == original:
        return
    try:
        addresses = await _resolve(hass, host, port)
    except OSError as err:
        raise ImageError(f"cannot resolve redirect host: {err}") from err
    if not addresses or not all(_is_public(a) for a in addresses):
        raise ImageError("redirect to a private address")


async def _download(hass: HomeAssistant, url: str) -> bytes:
    """Fetch ``url``, following at most ``MAX_REDIRECTS`` checked redirects."""
    session = async_get_clientsession(hass)
    try:
        original = _origin(url)  # a malformed port raises ValueError
        async with asyncio.timeout(FETCH_TIMEOUT):
            for hop in range(MAX_REDIRECTS + 1):
                if hop:
                    await _check_hop(hass, url, original)
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in REDIRECT_STATUSES:
                        location = resp.headers.get("Location")
                        if not location:
                            raise ImageError("redirect without Location")
                        url = urljoin(url, location)
                        continue
                    if resp.status != 200:
                        raise ImageError(f"upstream status {resp.status}")
                    if (resp.content_length or 0) > MAX_BYTES:
                        raise ImageError("image too large")
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise ImageError("image too large")
                    return bytes(body)
            raise ImageError("too many redirects")
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise ImageError(f"download failed: {err}") from err


def _scale(raw: bytes, size: int) -> bytes:
    try:
        with Image.open(io.BytesIO(raw), formats=FORMATS) as img:
            if img.width * img.height > MAX_PIXELS:
                raise ImageError("image has too many pixels")
            if img.format == "JPEG":
                img.draft("RGB", (size, size))
            img.load()
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((size, size))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY)
            return out.getvalue()
    except ImageError:
        raise
    except Exception as err:  # Pillow raises many types for bad input
        raise ImageError(f"cannot process image: {err}") from err


async def fetch_scaled(hass: HomeAssistant, url: str, size: int) -> bytes:
    """Return ``url`` scaled into ``size`` x ``size`` as JPEG.

    The caller must have checked that ``url`` was emitted by ContentService.
    """
    if not is_http_url(url):
        raise ImageError("only http(s) URLs are supported")
    size = clamp_size(size)
    cache: ScaledCache = hass.data.setdefault(_CACHE_KEY, ScaledCache())
    if (hit := cache.get((url, size))) is not None:
        return hit
    key = (url, size)
    task = cache.inflight.get(key)
    if task is None:
        task = hass.async_create_task(_produce(hass, cache, key))
        cache.inflight[key] = task
    return await asyncio.shield(task)


async def _produce(
    hass: HomeAssistant, cache: ScaledCache, key: tuple[str, int]
) -> bytes:
    try:
        raw = await _download(hass, raster_url(key[0]))
        async with cache.decodes:
            result = await hass.async_add_executor_job(_scale, raw, key[1])
        cache.put(key, result)
        return result
    finally:
        cache.inflight.pop(key, None)
