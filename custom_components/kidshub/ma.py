"""Music Assistant adapter: the only place that touches the MA client.

Verified against the client pinned by Home Assistant 2026.10.4
(``homeassistant/components/music_assistant/manifest.json``:
``music-assistant-client==1.5.1``, which pulls ``music-assistant-models==1.1.189``),
and live (2026-11-06) against HA 2026.11.0 (``music-assistant-client==1.6.0``, same
queue/music/player API) with the MA 2.10.9 add-on (``scripts/ma_probe.py``):

- Client: ``entry.runtime_data.mass`` of the loaded ``music_assistant`` config entry
  (``MusicAssistantEntryData.mass``, a ``MusicAssistantClient``); connection state via
  ``mass.connection.connected``. ``entry.data["url"]`` of the add-on is the container
  host from hassio discovery (``http://<slug>-music-assistant:8094``, not reachable from
  the LAN); ``mass.server_info.base_url`` is MA's LAN address and is used instead.
- Library: ``mass.music.get_library_playlists/_albums/_audiobooks/_podcasts(limit=, offset=)``.
- Children: ``mass.music.get_playlist_tracks(item_id, provider)``,
  ``get_album_tracks(item_id, provider)``, ``get_podcast_episodes(item_id, provider)``.
  Audiobooks have no browsable children (chapters are part of the item).
- Single item: ``mass.music.get_item_by_uri(uri)``.
- Images: ``mass.get_media_item_image_url(item)``: absolute (``<base_url>/imageproxy/<id>``,
  fetchable without auth, or the provider's own https URL); relative ones are joined with
  the base URL here.
- Players: ``mass.players.get(player_id)`` / iteration over ``mass.players``
  (``Player.player_id``, ``Player.name``, ``Player.output_protocols``).
- Queues: ``mass.player_queues.play_media(queue_id, media, option=QueueOption.REPLACE)``,
  ``seek(queue_id, position)`` and ``play_index(queue_id, index, seek_position=)``
  (**seconds**; a ``seek_position`` of 0 makes MA resume an audiobook/episode at MA's
  own stored position), ``pause/resume/next/previous/stop/clear(queue_id)``. ``ADD`` on
  a cleared queue does not start playback.
  ``mass.player_queues.get(queue_id)`` -> ``PlayerQueue`` (``current_item``, ``current_index``,
  ``elapsed_time`` in seconds, ``state``, ``sources``). ``sources`` are the enqueued
  tracks/albums/playlists (``ItemMapping`` with ``media_type``), so only a container
  counts as the queue's container. The queue id of a player is its player id.
- Events: ``mass.subscribe(cb, event_filter, id_filter)`` returns an unsubscribe callable;
  ``EventType.QUEUE_ADDED/QUEUE_UPDATED/QUEUE_TIME_UPDATED`` with ``object_id`` = queue id.
- MA URIs have the form ``{provider}://{media_type}/{item_id}``.
- ``Track.album`` is an ``ItemMapping``/``Album`` (``uri``) or None.
- Durations in MA models are seconds; ``PodcastEpisode.metadata.release_date`` is a datetime.

Sendspin players: the app registers with ``client_id`` ``kidshub-<device_id>``. MA 2.10
registers that client as a hidden *protocol* player (not in ``players/all``) and wraps it
in a universal player with a random id (live: ``kidshub-test`` -> ``up5e89c75a``) whose
``output_protocols`` list the client id. ``player_id_for`` therefore tries the client id,
then the output protocols, then the player name (``KidsHub <child>``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlparse

from homeassistant.components.network import async_get_source_ip
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SENDSPIN_URL, DOMAIN, ENTRY_TYPE, ENTRY_TYPE_SETTINGS
from .device import InvalidRequest, MaUnavailable

try:
    from music_assistant_client.exceptions import MusicAssistantClientException
    from music_assistant_models.enums import EventType, PlaybackState, QueueOption
    from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError

    HAS_CLIENT = True
except ImportError:  # Music Assistant integration not installed
    HAS_CLIENT = False

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from music_assistant_client import MusicAssistantClient

_LOGGER = logging.getLogger(__name__)

MA_DOMAIN = "music_assistant"
SENDSPIN_PORT = 8927
PAGE_SIZE = 500
MA_TIMEOUT = 10.0  # seconds per MA request
LIBRARY_TIMEOUT = 30.0  # seconds for a whole (paged) library listing
INFO_TIMEOUT = 5.0  # seconds for MA's ``GET /info`` (Sendspin host fallback)
INFO_TTL = 300.0  # seconds a fallback Sendspin host is reused

SendspinSource = Literal["override", "server_info", "ma_info", "ha_host", "none"]

LibraryKind = Literal["playlist", "album", "audiobook", "podcast"]
Command = Literal["pause", "resume", "next", "previous", "stop", "clear"]

_KIND_BY_MEDIA_TYPE = {
    "track": "track",
    "album": "album",
    "playlist": "playlist",
    "audiobook": "audiobook",
    "podcast": "podcast",
    "podcast_episode": "episode",
}


_LIBRARY_FETCH = {
    "playlist": "get_library_playlists",
    "album": "get_library_albums",
    "audiobook": "get_library_audiobooks",
    "podcast": "get_library_podcasts",
}


_CONTAINER_MEDIA_TYPES = {"album", "playlist", "podcast", "audiobook"}


def _base_url(mass: Any) -> str | None:
    """Return MA's own address (``internal_url``, older MA: ``base_url``)."""
    info = getattr(mass, "server_info", None)
    for attr in ("internal_url", "base_url"):
        url = getattr(info, attr, None)
        if isinstance(url, str) and url:
            return url
    return None


def usable_host(host: str | None, *, entry_host: str | None, addon: bool) -> bool:
    """Return whether the app can reach MA's Sendspin port at ``host``.

    Rejected: empty, loopback/unspecified/link-local addresses, ``localhost``,
    single-label names (add-on container names like ``d5369777-music-assistant``),
    ``*.hass.io`` (Supervisor-internal DNS) and, for the add-on, the host of the
    MA entry URL (hassio discovery hands out the container host). A standalone
    MA may well be reached under the entry host.
    """
    if not host:
        return False
    name = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local:
            return False
    elif "." not in name or name.endswith(".hass.io"):
        return False
    return not (addon and entry_host is not None and name == entry_host.lower())


def _ws_url(host: str) -> str:
    if ":" in host:
        host = f"[{host}]"
    return f"ws://{host}:{SENDSPIN_PORT}/sendspin"


@dataclass(frozen=True)
class MaItem:
    """A piece of MA content as KidsHub needs it."""

    uri: str
    kind: str
    title: str
    subtitle: str
    image: str | None
    duration_ms: int
    published: float | None
    album_uri: str | None = None  # album of a track (``Track.album``)


@dataclass(frozen=True)
class QueueSnapshot:
    """State of a player queue."""

    current: MaItem | None
    container_uri: str | None
    index: int
    elapsed_ms: int
    playing: bool


def parse_uri(uri: str) -> tuple[str, str, str]:
    """Split an MA URI into ``(provider, media_type, item_id)``."""
    provider, sep, rest = uri.partition("://")
    media_type, sep2, item_id = rest.partition("/")
    if not (sep and sep2 and provider and media_type and item_id):
        raise InvalidRequest(f"invalid uri: {uri}")
    return provider, media_type, item_id


def _names(values: Any) -> str:
    """Join artist/author names (objects with ``name`` or plain strings)."""
    names = [v if isinstance(v, str) else getattr(v, "name", "") for v in values or []]
    return ", ".join(n for n in names if n)


def _subtitle(obj: Any, kind: str) -> str:
    if kind in ("track", "album_track", "album"):
        return _names(getattr(obj, "artists", None))
    if kind == "audiobook":
        return _names(getattr(obj, "authors", None))
    if kind == "podcast":
        return getattr(obj, "publisher", None) or ""
    if kind == "episode":
        podcast = getattr(obj, "podcast", None)
        return getattr(podcast, "name", "") or ""
    if kind == "playlist":
        return getattr(obj, "owner", "") or ""
    return ""


def _published(obj: Any) -> float | None:
    metadata = getattr(obj, "metadata", None)
    release = getattr(metadata, "release_date", None)
    if release is None:
        return None
    try:
        return float(release.timestamp())
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


class MaAdapter:
    """Access Music Assistant through the client of the ``music_assistant`` entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Sendspin fallback (MA's /info or the HA host): (url, source, info, time)
        self._fallback: tuple[str | None, SendspinSource, dict[str, Any], float] | None = (
            None
        )

    # -- entry / client -------------------------------------------------

    def _loaded_entry(self) -> ConfigEntry | None:
        for entry in self.hass.config_entries.async_loaded_entries(MA_DOMAIN):
            if getattr(entry, "runtime_data", None) is not None:
                return entry
        return None

    def _client(self) -> tuple[MusicAssistantClient, str]:
        """Return the connected client and the server URL or raise ``MaUnavailable``."""
        if not HAS_CLIENT:
            raise MaUnavailable("Music Assistant is not installed")
        entry = self._loaded_entry()
        if entry is None:
            raise MaUnavailable("Music Assistant is not set up")
        mass = getattr(entry.runtime_data, "mass", None)
        if mass is None or not mass.connection.connected:
            raise MaUnavailable("Music Assistant is not connected")
        return mass, _base_url(mass) or str(entry.data.get("url", ""))

    @property
    def available(self) -> bool:
        """Return whether a connected MA client exists."""
        try:
            self._client()
        except MaUnavailable:
            return False
        return True

    @asynccontextmanager
    async def _guard(self, timeout: float | None = None) -> AsyncIterator[None]:
        """Bound the MA calls in time and map client errors to request errors."""
        try:
            async with asyncio.timeout(MA_TIMEOUT if timeout is None else timeout):
                yield
        except MediaNotFoundError as err:
            raise InvalidRequest(str(err) or "media not found") from err
        except TimeoutError as err:
            raise MaUnavailable("Music Assistant did not answer in time") from err
        except (
            MusicAssistantClientException,
            MusicAssistantError,
            ConnectionError,
        ) as err:
            raise MaUnavailable(str(err) or "Music Assistant request failed") from err

    # -- conversion -----------------------------------------------------

    def _image(self, mass: MusicAssistantClient, base_url: str, obj: Any) -> str | None:
        try:
            url = mass.get_media_item_image_url(obj)
        except (AssertionError, AttributeError, TypeError, ValueError):
            return None
        if not url:
            return None
        if not urlparse(url).scheme:
            url = urljoin(base_url.rstrip("/") + "/", url)
        return url

    def _to_item(
        self,
        mass: MusicAssistantClient,
        base_url: str,
        obj: Any,
        kind: str | None = None,
    ) -> MaItem:
        media_type = str(getattr(obj, "media_type", "") or "")
        kind = kind or _KIND_BY_MEDIA_TYPE.get(media_type, media_type)
        duration = getattr(obj, "duration", None) or 0
        album_uri = getattr(getattr(obj, "album", None), "uri", None)
        return MaItem(
            uri=str(obj.uri),
            kind=kind,
            title=getattr(obj, "name", "") or "",
            subtitle=_subtitle(obj, kind),
            image=self._image(mass, base_url, obj),
            duration_ms=int(duration * 1000),
            published=_published(obj) if kind == "episode" else None,
            album_uri=album_uri if isinstance(album_uri, str) and album_uri else None,
        )

    # -- content --------------------------------------------------------

    async def library(self, kind: LibraryKind) -> list[MaItem]:
        """Return all library items of ``kind``."""
        mass, base_url = self._client()
        if kind not in _LIBRARY_FETCH:
            raise InvalidRequest(f"unknown library kind: {kind}")
        fetch = getattr(mass.music, _LIBRARY_FETCH[kind])
        result: list[MaItem] = []
        async with self._guard(LIBRARY_TIMEOUT):
            offset = 0
            while True:
                page = await fetch(limit=PAGE_SIZE, offset=offset)
                result.extend(self._to_item(mass, base_url, o, kind) for o in page)
                if len(page) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
        return result

    async def children(self, uri: str) -> list[MaItem]:
        """Return the tracks of a playlist/album or the episodes of a podcast."""
        provider, media_type, item_id = parse_uri(uri)
        mass, base_url = self._client()
        async with self._guard():
            if media_type == "playlist":
                objs = await mass.music.get_playlist_tracks(item_id, provider)
                kind = "track"
            elif media_type == "album":
                objs = await mass.music.get_album_tracks(item_id, provider)
                kind = "album_track"
            elif media_type == "podcast":
                objs = await mass.music.get_podcast_episodes(item_id, provider)
                kind = "episode"
            elif media_type == "audiobook":
                return []
            else:
                raise InvalidRequest(f"{media_type} has no children")
        return [self._to_item(mass, base_url, o, kind) for o in objs]

    async def item(self, uri: str) -> MaItem:
        """Return one item by its URI."""
        parse_uri(uri)
        mass, base_url = self._client()
        async with self._guard():
            obj = await mass.music.get_item_by_uri(uri)
        return self._to_item(mass, base_url, obj)

    # -- sendspin / players ---------------------------------------------

    def _override_url(self) -> str | None:
        """Return the Sendspin URL set in the KidsHub settings entry, if any."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_SETTINGS:
                url = str(entry.options.get(CONF_SENDSPIN_URL) or "").strip()
                return url or None
        return None

    def _server_view(self) -> tuple[Any, str | None, bool] | None:
        """Return ``(mass, entry host, is add-on)`` of a connected MA, else None."""
        try:
            mass, _ = self._client()
        except MaUnavailable:
            return None
        entry = self._loaded_entry()
        entry_url = str(entry.data.get("url", "")) if entry is not None else ""
        entry_host = urlparse(entry_url).hostname
        info = getattr(mass, "server_info", None)
        addon = bool(getattr(info, "homeassistant_addon", False)) or (
            entry_host is not None
            and not usable_host(entry_host, entry_host=None, addon=False)
        )
        return mass, entry_host, addon

    def sendspin_url(self) -> tuple[str | None, SendspinSource]:
        """Return the Sendspin URL for the app and where it came from.

        Order: the override of the settings entry; MA's own address from
        ``server_info``; MA's ``GET /info`` and, for the add-on, the HA host
        (both looked up by ``async_refresh_sendspin``). A host the app cannot
        reach (``usable_host``) is never returned.
        """
        override = self._override_url()
        if override:
            return override, "override"
        view = self._server_view()
        if view is None:
            return None, "none"
        mass, entry_host, addon = view
        host = urlparse(_base_url(mass) or "").hostname
        if usable_host(host, entry_host=entry_host, addon=addon):
            assert host is not None
            return _ws_url(host), "server_info"
        if self._fallback is not None:
            return self._fallback[0], self._fallback[1]
        return None, "none"

    async def async_refresh_sendspin(self) -> None:
        """Look up a fallback Sendspin host if ``server_info`` has no usable one."""
        if self._override_url():
            return
        view = self._server_view()
        if view is None:
            return
        mass, entry_host, addon = view
        if usable_host(
            urlparse(_base_url(mass) or "").hostname, entry_host=entry_host, addon=addon
        ):
            return
        if self._fallback is not None and time.monotonic() - self._fallback[3] < INFO_TTL:
            return
        entry = self._loaded_entry()
        entry_url = str(entry.data.get("url", "")) if entry is not None else ""
        info = await self._fetch_info(entry_url)
        addon = addon or info.get("homeassistant_addon") is True
        url: str | None = None
        source: SendspinSource = "none"
        for key in ("internal_url", "base_url"):
            host = urlparse(str(info.get(key) or "")).hostname
            if usable_host(host, entry_host=entry_host, addon=addon):
                assert host is not None
                url, source = _ws_url(host), "ma_info"
                break
        if url is None and addon:
            # the add-on runs on the HA host (host network): same address
            try:
                ip = await async_get_source_ip(self.hass)
            except Exception:  # noqa: BLE001 - network integration missing/failing
                _LOGGER.debug("No HA host address for the Sendspin fallback")
                ip = None
            if usable_host(ip, entry_host=entry_host, addon=addon):
                assert ip is not None
                url, source = _ws_url(ip), "ha_host"
        _LOGGER.debug("Sendspin fallback: %s (%s)", url, source)
        self._fallback = (url, source, info, time.monotonic())

    async def _fetch_info(self, entry_url: str) -> dict[str, Any]:
        """Return MA's public ``GET /info`` (empty on any failure)."""
        if not entry_url:
            return {}
        try:
            async with asyncio.timeout(INFO_TIMEOUT):
                resp = await async_get_clientsession(self.hass).get(
                    entry_url.rstrip("/") + "/info"
                )
                resp.raise_for_status()
                info = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - any failure means no info
            _LOGGER.debug("MA /info at %s failed: %s", entry_url, err)
            return {}
        return info if isinstance(info, dict) else {}

    def sendspin_info(self, device_id: str, child_name: str) -> dict[str, Any] | None:
        """Return the ``config.sendspin`` block, ``None`` without a reachable address.

        The entry URL is never used: for the add-on it is the container host
        (hassio discovery, e.g. ``http://d5369777-music-assistant:8094``), which
        the app cannot resolve. The block is pushed again on MA entry changes.
        """
        url, _ = self.sendspin_url()
        if url is None:
            return None
        return {
            "url": url,
            "client_id": f"kidshub-{device_id}",
            "name": f"KidsHub {child_name}",
        }

    def diagnostics(self) -> dict[str, Any]:
        """Return what the Sendspin address was computed from (no secrets)."""
        entry = self._loaded_entry()
        mass = getattr(getattr(entry, "runtime_data", None), "mass", None)
        info = getattr(mass, "server_info", None)
        url, source = self.sendspin_url()
        fallback = self._fallback
        return {
            "connected": self.available,
            "entry_url": str(entry.data.get("url", "")) if entry is not None else None,
            "server_info": None
            if info is None
            else {
                key: getattr(info, key, None)
                for key in (
                    "server_version",
                    "base_url",
                    "internal_url",
                    "homeassistant_addon",
                )
            },
            "fallback": None
            if fallback is None
            else {"url": fallback[0], "source": fallback[1], "info": fallback[2]},
            "sendspin_url": url,
            "source": source,
        }

    async def player_id_for(self, client_id: str, name: str | None = None) -> str | None:
        """Return the MA player id of a Sendspin client.

        Order: the client id itself, the player that lists the client id as an
        output protocol (MA 2.10 universal player), then the player name.
        """
        mass, _ = self._client()
        if mass.players.get(client_id) is not None:
            return client_id
        for player in mass.players:
            protocols = getattr(player, "output_protocols", None) or []
            if any(getattr(p, "output_protocol_id", None) == client_id for p in protocols):
                return player.player_id
        if name:
            for player in mass.players:
                if player.name == name:
                    return player.player_id
        return None

    # -- playback (used by the player task) -------------------------------

    async def play(
        self,
        player_id: str,
        uris: list[str],
        start_index: int = 0,
        seek_ms: int = 0,
    ) -> None:
        """Replace the queue of ``player_id`` with ``uris`` and start playback.

        The whole list is queued (so "previous" reaches earlier items); playback
        starts at ``uris[start_index]``, ``seek_ms`` into that item.
        ``REPLACE`` always starts at index 0, so a later start clears the queue,
        adds the items without starting (``ADD``) and starts with ``play_index``.
        """
        mass, _ = self._client()
        queues = mass.player_queues
        async with self._guard():
            if start_index <= 0 and seek_ms <= 0:
                await queues.play_media(player_id, list(uris), option=QueueOption.REPLACE)
                return
            await queues.clear(player_id)
            await queues.play_media(player_id, list(uris), option=QueueOption.ADD)
            await queues.play_index(
                player_id, max(0, start_index), seek_position=max(0, seek_ms) // 1000
            )

    async def seek(self, player_id: str, position_ms: int) -> None:
        """Seek within the current item (MA takes seconds)."""
        mass, _ = self._client()
        async with self._guard():
            await mass.player_queues.seek(player_id, max(0, position_ms) // 1000)

    async def command(self, player_id: str, cmd: Command) -> None:
        """Send a transport command to the queue of ``player_id``."""
        mass, _ = self._client()
        queues = mass.player_queues
        func = {
            "pause": queues.pause,
            "resume": queues.resume,
            "next": queues.next,
            "previous": queues.previous,
            "stop": queues.stop,
            "clear": queues.clear,
        }.get(cmd)
        if func is None:
            raise InvalidRequest(f"unknown command: {cmd}")
        async with self._guard():
            await func(player_id)

    def subscribe_queue(
        self, player_id: str, cb: Callable[[QueueSnapshot], None]
    ) -> Callable[[], None]:
        """Call ``cb`` with a snapshot on every queue change of ``player_id``."""
        mass, base_url = self._client()

        def _on_event(event: Any) -> None:
            queue = mass.player_queues.get(player_id)
            if queue is None:
                return
            cb(self._snapshot(mass, base_url, queue))

        return mass.subscribe(
            _on_event,
            (EventType.QUEUE_ADDED, EventType.QUEUE_UPDATED, EventType.QUEUE_TIME_UPDATED),
            player_id,
        )

    def _snapshot(
        self, mass: MusicAssistantClient, base_url: str, queue: Any
    ) -> QueueSnapshot:
        playing = queue.state == PlaybackState.PLAYING
        elapsed = float(queue.elapsed_time or 0)
        if playing:
            elapsed += max(0.0, time.time() - queue.elapsed_time_last_updated) * (
                queue.playback_speed or 1.0
            )
        current: MaItem | None = None
        queue_item = queue.current_item
        if queue_item is not None and queue_item.media_item is not None:
            current = self._to_item(mass, base_url, queue_item.media_item)
        sources = getattr(queue, "sources", None) or []
        container = sources[0] if sources else None
        if str(getattr(container, "media_type", "")) not in _CONTAINER_MEDIA_TYPES:
            container = None  # MA records enqueued plain tracks as sources too
        return QueueSnapshot(
            current=current,
            container_uri=getattr(container, "uri", None),
            index=queue.current_index or 0,
            elapsed_ms=int(elapsed * 1000),
            playing=playing,
        )
