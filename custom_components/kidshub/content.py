"""Content requests of one device: ``browse``, ``favorites``, ``rate`` (protocol 3.3)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlparse

from .device import Handler, InvalidRequest, KidDevice, NotAllowed
from .sorting import sort_episodes, sort_tracks
from .tts_cache import PREWARM_PER_CALL

if TYPE_CHECKING:
    from .ma import MaAdapter, MaItem

FAVORITES_PREFIX = "kidshub://favorites/"
FAVORITES_MIN_STARS = 4
CHILDREN_TTL = 60.0
IMAGE_REGISTRY_SIZE = 2000

# section -> (option key, library kinds, favorites kind)
SECTIONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "music": ("playlists", ("playlist",), "track"),
    "audiobooks": ("audiobooks", ("album", "audiobook"), "audiobook"),
    "podcasts": ("podcasts", ("podcast",), "episode"),
}
CONTENT_OPTIONS = tuple(key for key, _, _ in SECTIONS.values())

# favorites kind -> rating kinds it lists (whole albums count as audiobooks)
FAVORITE_KINDS: dict[str, tuple[str, ...]] = {
    "track": ("track",),
    "episode": ("episode",),
    "audiobook": ("audiobook", "album"),
}
# Rateable kinds. Album tracks (Hörspiel chapters) are not rated themselves; the app
# rates the album/audiobook container instead.
RATE_KINDS = {"track", "episode", "audiobook", "album"}
PROGRESS_KINDS = {"episode", "audiobook", "album"}
SORTS = {"default", "stars"}


def image_url(image: str | None, size: int = 256) -> str | None:
    """Rewrite an MA image URL to the KidsHub image proxy (pure, no registration)."""
    if not image:
        return None
    return f"/api/kidshub/img?u={quote(image, safe='')}&s={size}"


def _proxied_source(url: str | None) -> str | None:
    """Return the absolute image URL inside a ``/api/kidshub/img`` URL."""
    if not url:
        return None
    values = parse_qs(urlparse(url).query).get("u")
    return values[0] if values else None


class ImageRegistry:
    """Bounded LRU set of absolute image URLs that were handed to an app."""

    def __init__(self, maxsize: int = IMAGE_REGISTRY_SIZE) -> None:
        self.maxsize = maxsize
        self._urls: OrderedDict[str, None] = OrderedDict()

    def add(self, url: str) -> None:
        """Remember ``url`` (most recently used), evicting the oldest entries."""
        self._urls[url] = None
        self._urls.move_to_end(url)
        while len(self._urls) > self.maxsize:
            self._urls.popitem(last=False)

    def __contains__(self, url: object) -> bool:
        return url in self._urls

    def __len__(self) -> int:
        return len(self._urls)


class ContentService:
    """Browse, favorites and ratings against the content allowlist of a device."""

    def __init__(
        self, device: KidDevice, ma: MaAdapter, images: ImageRegistry | None = None
    ) -> None:
        self.device = device
        self.ma = ma
        self.images = images if images is not None else ImageRegistry()
        # Item currently playing on this device; set by the playback code so that
        # it can be rated even when it is not part of an allowed container.
        self.now_playing: MaItem | None = None
        self._children: dict[str, tuple[float, list[MaItem]]] = {}
        device.register_handler(
            "browse",
            self._prewarming(
                self._handle_browse,
                # top-level lists first; long child lists must not delay them
                lambda msg: not isinstance(msg.get("parent_uri"), str)
                or msg["parent_uri"].startswith(FAVORITES_PREFIX),
            ),
        )
        device.register_handler(
            "favorites", self._prewarming(self._handle_favorites, lambda msg: True)
        )
        device.register_handler("rate", self._handle_rate)

    # -- allowlist --------------------------------------------------------

    def allowed(self, section: str | None = None) -> list[str]:
        """Return the allowed container URIs (of one section or of all)."""
        options = self.device.entry.options
        keys = [SECTIONS[section][0]] if section else CONTENT_OPTIONS
        result: list[str] = []
        for key in keys:
            result.extend(u for u in options.get(key) or [] if u not in result)
        return result

    async def children(self, uri: str, *, fresh: bool = False) -> list[MaItem]:
        """Return the children of a container, cached for ``CHILDREN_TTL``."""
        cached = self._children.get(uri)
        now = time.monotonic()
        if not fresh and cached is not None and now - cached[0] < CHILDREN_TTL:
            return cached[1]
        items = await self.ma.children(uri)
        self._children[uri] = (time.monotonic(), items)
        return items

    async def find_allowed_item(
        self, uri: str, *, include_now_playing: bool = False
    ) -> MaItem | None:
        """Return the item if it is an allowed container or one of their children.

        ``include_now_playing`` also accepts the item playing on the device (it may
        have been started outside KidsHub); only rating uses that, never playback.
        """
        if (
            include_now_playing
            and self.now_playing is not None
            and self.now_playing.uri == uri
        ):
            return self.now_playing
        containers = self.allowed()
        if uri in containers:
            try:
                return await self.ma.item(uri)
            except InvalidRequest:
                return None
        results = await asyncio.gather(
            *(self.children(c) for c in containers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, InvalidRequest):
                    continue
                raise result
            for child in result:
                if child.uri == uri:
                    return child
        return None

    async def is_allowed_item(self, uri: str) -> bool:
        """Return whether ``uri`` may be rated/played on this device."""
        return await self.find_allowed_item(uri) is not None

    # -- images -----------------------------------------------------------

    def image_url(self, image: str | None, size: int = 256) -> str | None:
        """Proxy URL for ``image``; the absolute URL is registered as emitted."""
        if image:
            self.images.add(image)
        return image_url(image, size)

    def is_known_image(self, url: str) -> bool:
        """Return whether ``url`` was emitted to an app (image proxy allowlist)."""
        return url in self.images

    # -- items ------------------------------------------------------------

    def _progress(self, uri: str, duration_ms: int) -> dict[str, Any]:
        rec = self.device.store.get_progress(uri)
        if rec is None:
            return {"position_ms": 0, "duration_ms": duration_ms, "finished": False}
        return {
            "position_ms": rec.position_ms,
            "duration_ms": rec.duration_ms,
            "finished": rec.finished,
        }

    def to_item(self, it: MaItem, size: int = 256) -> dict[str, Any]:
        """Convert an ``MaItem`` to a protocol Item."""
        item: dict[str, Any] = {
            "uri": it.uri,
            "kind": it.kind,
            "title": it.title,
            "subtitle": it.subtitle,
            "image_url": self.image_url(it.image, size),
            "stars": self.device.store.get_rating(it.uri),
        }
        if it.kind in PROGRESS_KINDS:
            item["progress"] = self._progress(it.uri, it.duration_ms)
        return item

    def _favorites_tile(self, kind: str) -> dict[str, Any]:
        return {
            "uri": f"{FAVORITES_PREFIX}{kind}",
            "kind": "favorites",
            "title": "Favoriten",
            "subtitle": "",
            "image_url": None,
            "stars": None,
        }

    def favorites(self, kind: str) -> list[dict[str, Any]]:
        """Return the rated items of ``kind`` with at least four stars."""
        kinds = FAVORITE_KINDS.get(kind)
        if kinds is None:
            raise InvalidRequest("kind must be track, episode or audiobook")
        items = []
        for rec in self.device.store.ratings():
            if rec.kind not in kinds or rec.stars < FAVORITES_MIN_STARS:
                continue
            if source := _proxied_source(rec.image_url):
                self.images.add(source)
            item: dict[str, Any] = {
                "uri": rec.uri,
                "kind": rec.kind,
                "title": rec.title,
                "subtitle": rec.subtitle,
                "image_url": rec.image_url,
                "stars": rec.stars,
            }
            if rec.kind in PROGRESS_KINDS:
                item["progress"] = self._progress(rec.uri, 0)
            items.append(item)
        return items

    # -- handlers ---------------------------------------------------------

    def _prewarming(
        self, handler: Handler, priority: Callable[[dict[str, Any]], bool]
    ) -> Handler:
        """Wrap ``handler`` to pre-warm the speech of the item titles it returns."""

        async def wrapped(msg: dict[str, Any]) -> dict[str, Any]:
            result = await handler(msg)
            self.device.prewarm_tts(
                (i.get("title") for i in result.get("items") or []),
                priority=priority(msg),
                limit=PREWARM_PER_CALL,
            )
            return result

        return wrapped

    async def _handle_browse(self, msg: dict[str, Any]) -> dict[str, Any]:
        section = msg.get("section")
        sort = msg.get("sort", "default")
        parent = msg.get("parent_uri")
        if section not in SECTIONS:
            raise InvalidRequest("section must be music, audiobooks or podcasts")
        if sort not in SORTS:
            raise InvalidRequest("sort must be default or stars")
        if parent is not None and not isinstance(parent, str):
            raise InvalidRequest("parent_uri must be a string")
        if parent is not None and parent.startswith(FAVORITES_PREFIX):
            return {"items": self.favorites(parent.removeprefix(FAVORITES_PREFIX))}
        if parent is None:
            return {"items": await self._browse_root(section)}
        if parent not in self.allowed(section):
            raise NotAllowed("content not allowed")
        children = await self.children(parent, fresh=True)
        items = [self.to_item(c) for c in children]
        if any(c.kind == "episode" for c in children):
            for item, child in zip(items, children, strict=True):
                item["_published"] = (
                    child.published if child.published is not None else float("-inf")
                )
            return {"items": sort_episodes(items, sort)}
        return {"items": sort_tracks(items, sort)}

    async def _browse_root(self, section: str) -> list[dict[str, Any]]:
        _, kinds, fav_kind = SECTIONS[section]
        allowed = set(self.allowed(section))
        libraries = await asyncio.gather(*(self.ma.library(k) for k in kinds))
        items: list[dict[str, Any]] = []
        if self.favorites(fav_kind):
            items.append(self._favorites_tile(fav_kind))
        seen: set[str] = set()
        for library in libraries:
            for it in library:
                if it.uri in allowed and it.uri not in seen:
                    seen.add(it.uri)
                    items.append(self.to_item(it))
        return items

    async def _handle_favorites(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg.get("kind")
        if not isinstance(kind, str):
            raise InvalidRequest("kind must be a string")
        return {"items": self.favorites(kind)}

    async def _handle_rate(self, msg: dict[str, Any]) -> dict[str, Any]:
        uri = msg.get("uri")
        kind = msg.get("kind")
        stars = msg.get("stars")
        if not isinstance(uri, str) or not uri:
            raise InvalidRequest("uri must be a string")
        if kind not in RATE_KINDS:
            raise InvalidRequest("unsupported kind")
        if isinstance(stars, bool) or not isinstance(stars, int) or not 1 <= stars <= 5:
            raise InvalidRequest("stars must be an integer 1..5")
        found = await self.find_allowed_item(uri, include_now_playing=True)
        if found is None:
            raise NotAllowed("content not allowed")
        if found.kind != kind:
            # also rejects album tracks: their container is rated instead
            raise InvalidRequest(f"uri is a {found.kind}, not a {kind}")
        self.device.store.set_rating(
            uri,
            found.kind,
            stars,
            found.title,
            found.subtitle,
            self.image_url(found.image),
        )
        self.device.notify()
        return {}
