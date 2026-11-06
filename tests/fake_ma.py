"""In-memory stand-in for ``ma.MaAdapter`` (same interface, no MA client)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncio

from custom_components.kidshub.device import InvalidRequest, MaUnavailable
from custom_components.kidshub.ma import MaItem, QueueSnapshot


def item(
    uri: str,
    kind: str,
    title: str = "",
    subtitle: str = "",
    image: str | None = "http://ma.local:8095/imageproxy/x",
    duration_ms: int = 0,
    published: float | None = None,
) -> MaItem:
    """Build an ``MaItem`` with test defaults."""
    return MaItem(
        uri=uri,
        kind=kind,
        title=title or uri,
        subtitle=subtitle,
        image=image,
        duration_ms=duration_ms,
        published=published,
    )


class FakeMa:
    """Configurable library, recorded playback calls."""

    def __init__(
        self,
        *,
        available: bool = True,
        library: dict[str, list[MaItem]] | None = None,
        children: dict[str, list[MaItem]] | None = None,
        sendspin: dict[str, Any] | None = None,
        players: dict[str, str] | None = None,
        stall_library: bool = False,
    ) -> None:
        self.available = available
        self._stall_library = stall_library  # library() never returns
        self._library = library or {}
        self._children = children or {}
        self._sendspin = sendspin
        self._players = players or {}  # client_id -> player_id
        self.play_calls: list[tuple[str, list[str]]] = []
        self.seek_calls: list[tuple[str, int]] = []
        self.command_calls: list[tuple[str, str]] = []
        self.children_calls: list[str] = []
        self._queue_subs: dict[str, list[Callable[[QueueSnapshot], None]]] = {}

    def _check(self) -> None:
        if not self.available:
            raise MaUnavailable("Music Assistant is not available")

    async def library(self, kind: str) -> list[MaItem]:
        self._check()
        if self._stall_library:
            await asyncio.Event().wait()
        return list(self._library.get(kind, []))

    async def children(self, uri: str) -> list[MaItem]:
        self._check()
        self.children_calls.append(uri)
        return list(self._children.get(uri, []))

    async def item(self, uri: str) -> MaItem:
        self._check()
        for items in [*self._library.values(), *self._children.values()]:
            for it in items:
                if it.uri == uri:
                    return it
        raise InvalidRequest(f"media not found: {uri}")

    def sendspin_info(self, device_id: str, child_name: str) -> dict[str, Any] | None:
        return self._sendspin

    async def player_id_for(self, client_id: str, name: str | None = None) -> str | None:
        self._check()
        return self._players.get(client_id)

    async def play(self, player_id: str, uris: list[str]) -> None:
        self._check()
        self.play_calls.append((player_id, list(uris)))

    async def seek(self, player_id: str, position_ms: int) -> None:
        self._check()
        self.seek_calls.append((player_id, position_ms))

    async def command(self, player_id: str, cmd: str) -> None:
        self._check()
        self.command_calls.append((player_id, cmd))

    def subscribe_queue(
        self, player_id: str, cb: Callable[[QueueSnapshot], None]
    ) -> Callable[[], None]:
        self._check()
        subs = self._queue_subs.setdefault(player_id, [])
        subs.append(cb)
        return lambda: subs.remove(cb)

    def emit_queue(self, player_id: str, snapshot: QueueSnapshot) -> None:
        """Deliver ``snapshot`` to the queue subscribers of ``player_id``."""
        for cb in list(self._queue_subs.get(player_id, [])):
            cb(snapshot)
