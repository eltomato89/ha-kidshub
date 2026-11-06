"""Image proxy: download, scale and cache cover images."""

from __future__ import annotations

import io
from collections import OrderedDict
from urllib.parse import urlparse

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image

MIN_SIZE = 64
MAX_SIZE = 512
MAX_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT = 10.0
CACHE_SIZE = 200
JPEG_QUALITY = 80
_CACHE_KEY = "kidshub_image_cache"


class ImageError(Exception):
    """The image could not be fetched or processed."""


class ScaledCache:
    """LRU cache of resized image bytes keyed by ``(url, size)``."""

    def __init__(self, maxsize: int = CACHE_SIZE) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[tuple[str, int], bytes] = OrderedDict()

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


async def _download(hass: HomeAssistant, url: str) -> bytes:
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
        ) as resp:
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
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ImageError(f"download failed: {err}") from err


def _scale(raw: bytes, size: int) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as img:
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
    raw = await _download(hass, url)
    result = await hass.async_add_executor_job(_scale, raw, size)
    cache.put((url, size), result)
    return result
