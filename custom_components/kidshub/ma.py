"""Music Assistant adapter: the only place that touches the MA client.

Verified against the client pinned by Home Assistant 2026.10.4
(``homeassistant/components/music_assistant/manifest.json``:
``music-assistant-client==1.5.1``, which pulls ``music-assistant-models==1.1.189``):

- Client: ``entry.runtime_data.mass`` of the loaded ``music_assistant`` config entry
  (``MusicAssistantEntryData.mass``, a ``MusicAssistantClient``); connection state via
  ``mass.connection.connected``; server URL in ``entry.data["url"]``.
- Library: ``mass.music.get_library_playlists/_albums/_audiobooks/_podcasts(limit=, offset=)``.
- Children: ``mass.music.get_playlist_tracks(item_id, provider)``,
  ``get_album_tracks(item_id, provider)``, ``get_podcast_episodes(item_id, provider)``.
  Audiobooks have no browsable children (chapters are part of the item).
- Single item: ``mass.music.get_item_by_uri(uri)``.
- Images: ``mass.get_media_item_image_url(item)`` (may be relative to the MA server, made
  absolute with the entry URL here).
- Players: ``mass.players.get(player_id)`` / iteration over ``mass.players``
  (``Player.player_id``, ``Player.name``).
- Queues: ``mass.player_queues.play_media(queue_id, media, option=QueueOption.REPLACE)``,
  ``seek(queue_id, position)`` (**seconds**), ``pause/resume/next/previous/stop(queue_id)``,
  ``mass.player_queues.get(queue_id)`` -> ``PlayerQueue`` (``current_item``, ``current_index``,
  ``elapsed_time`` in seconds, ``state``, ``sources``). The queue id of a player is its
  player id.
- Events: ``mass.subscribe(cb, event_filter, id_filter)`` returns an unsubscribe callable;
  ``EventType.QUEUE_ADDED/QUEUE_UPDATED/QUEUE_TIME_UPDATED`` with ``object_id`` = queue id.
- MA URIs have the form ``{provider}://{media_type}/{item_id}``.
- Durations in MA models are seconds; ``PodcastEpisode.metadata.release_date`` is a datetime.

Sendspin players: the app registers with ``client_id`` ``kidshub-<device_id>`` and MA keeps
the player under exactly that id. ``player_id_for`` falls back to a match on the player
name (``KidsHub <child>``) in case an MA version derives the id differently.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlparse

from homeassistant.core import HomeAssistant

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

LibraryKind = Literal["playlist", "album", "audiobook", "podcast"]
Command = Literal["pause", "resume", "next", "previous", "stop"]

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
        return mass, str(entry.data.get("url", ""))

    @property
    def available(self) -> bool:
        """Return whether a connected MA client exists."""
        try:
            self._client()
        except MaUnavailable:
            return False
        return True

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        """Map client errors to the KidsHub request errors."""
        try:
            yield
        except MediaNotFoundError as err:
            raise InvalidRequest(str(err) or "media not found") from err
        except (
            MusicAssistantClientException,
            MusicAssistantError,
            ConnectionError,
            TimeoutError,
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
        return MaItem(
            uri=str(obj.uri),
            kind=kind,
            title=getattr(obj, "name", "") or "",
            subtitle=_subtitle(obj, kind),
            image=self._image(mass, base_url, obj),
            duration_ms=int(duration * 1000),
            published=_published(obj) if kind == "episode" else None,
        )

    # -- content --------------------------------------------------------

    async def library(self, kind: LibraryKind) -> list[MaItem]:
        """Return all library items of ``kind``."""
        mass, base_url = self._client()
        if kind not in _LIBRARY_FETCH:
            raise InvalidRequest(f"unknown library kind: {kind}")
        fetch = getattr(mass.music, _LIBRARY_FETCH[kind])
        result: list[MaItem] = []
        async with self._guard():
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

    def sendspin_info(self, device_id: str, child_name: str) -> dict[str, Any] | None:
        """Return the ``config.sendspin`` block, ``None`` without an MA entry."""
        entries = self.hass.config_entries.async_entries(MA_DOMAIN)
        if not entries:
            return None
        url = (self._loaded_entry() or entries[0]).data.get("url")
        host = urlparse(str(url or "")).hostname
        if not host:
            return None
        if ":" in host:
            host = f"[{host}]"
        return {
            "url": f"ws://{host}:{SENDSPIN_PORT}/sendspin",
            "client_id": f"kidshub-{device_id}",
            "name": f"KidsHub {child_name}",
        }

    async def player_id_for(self, client_id: str, name: str | None = None) -> str | None:
        """Return the MA player id of a Sendspin client (id first, then name)."""
        mass, _ = self._client()
        if mass.players.get(client_id) is not None:
            return client_id
        if name:
            for player in mass.players:
                if player.name == name:
                    return player.player_id
        return None

    # -- playback (used by the player task) -------------------------------

    async def play(self, player_id: str, uris: list[str]) -> None:
        """Replace the queue of ``player_id`` with ``uris`` and start playback."""
        mass, _ = self._client()
        async with self._guard():
            await mass.player_queues.play_media(
                player_id, list(uris), option=QueueOption.REPLACE
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
        return QueueSnapshot(
            current=current,
            container_uri=getattr(sources[0], "uri", None) if sources else None,
            index=queue.current_index or 0,
            elapsed_ms=int(elapsed * 1000),
            playing=playing,
        )
