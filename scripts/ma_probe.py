"""Live probe of Music Assistant through Home Assistant (not part of the integration).

Checks the assumptions of ``custom_components/kidshub/ma.py`` and ``playback.py``
against a real HA + MA installation, using only the HA websocket/REST API with an
admin long-lived token read from a file (never printed).

    export KIDSHUB_HA_URL=http://192.168.1.10:8123 KIDSHUB_MA_URL=http://192.168.1.10:8095
    .venv/bin/python -I scripts/ma_probe.py                      # read-only checks
    .venv/bin/python -I scripts/ma_probe.py --play               # + playback tests
    .venv/bin/python -I scripts/ma_probe.py --tts                # + TTS format test

``--ha``/``--ma`` (or ``KIDSHUB_HA_URL``/``KIDSHUB_MA_URL``) are required.

``--play`` plays, seeks, clears and stops media **only** on the MA player named by
``--player`` (default ``KidsHub Test``) at volume ``--volume`` and stops it afterwards.
``--tts [ENGINE ...]`` renders one short German phrase with each given TTS entity
(default ``tts.google_ai_tts``; HA caches the result).
Nothing else is changed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

LIBRARY_TIMEOUT = 30.0  # ma.py LIBRARY_TIMEOUT
PAGE_SIZE = 500  # ma.py PAGE_SIZE
SENDSPIN_PORT = 8927


class Ha:
    """Minimal HA websocket client."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._id = 0
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None

    async def __aenter__(self) -> Ha:
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url + "/api/websocket", max_msg_size=0)
        await self.ws.receive_json()
        await self.ws.send_json({"type": "auth", "access_token": self._token})
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise SystemExit("HA authentication failed")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()

    async def call(self, msg: dict[str, Any], timeout: float = 60.0) -> Any:
        """Send one command and return its result (raises on error)."""
        assert self.ws is not None
        self._id += 1
        await self.ws.send_json({"id": self._id, **msg})
        async with asyncio.timeout(timeout):
            while True:
                res = await self.ws.receive_json()
                if res.get("id") == self._id and res.get("type") == "result":
                    if not res.get("success"):
                        raise RuntimeError(json.dumps(res.get("error")))
                    return res.get("result")

    async def service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        response: bool = False,
        timeout: float = 60.0,
    ) -> Any:
        msg: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": data or {},
        }
        if target:
            msg["target"] = target
        if response:
            msg["return_response"] = True
        result = await self.call(msg, timeout)
        return (result or {}).get("response")

    async def rest(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        assert self.session is not None
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self.session.request(
            method, self.url + path, json=body, headers=headers
        ) as resp:
            try:
                return resp.status, await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                return resp.status, None


def out(title: str, value: Any) -> None:
    print(f"- {title}: {json.dumps(value, ensure_ascii=False, default=str)}")


# -- read-only sections --------------------------------------------------------


async def probe_info(ha: Ha) -> dict[str, Any]:
    print("## HA / MA entry")
    cfg = await ha.call({"type": "get_config"})
    out("HA version", cfg.get("version"))
    entries = await ha.call({"type": "config_entries/get", "domain": "music_assistant"})
    for e in entries:
        out("MA entry", {k: e.get(k) for k in ("entry_id", "state", "source", "title")})
    # The entry data (url) is not exposed by HA; the Supervisor discovery message is what
    # the add-on flow writes into entry.data["url"] (async_step_hassio).
    try:
        disc = await ha.call(
            {"type": "supervisor/api", "endpoint": "/discovery", "method": "get"}
        )
        for m in disc.get("discovery", []):
            if m.get("service") == "music_assistant":
                c = m.get("config", {})
                url = f"http://{c.get('host')}:{c.get('port')}"
                out("hassio discovery url (-> entry.data[url])", url)
                out(
                    "sendspin url derived from entry host",
                    f"ws://{urlparse(url).hostname}:{SENDSPIN_PORT}/sendspin",
                )
    except RuntimeError as err:
        out("supervisor discovery", f"not available: {err}")
    info: dict[str, Any] = {}
    if entries:
        diag_status, diag = await ha.rest(
            "GET", f"/api/diagnostics/config_entry/{entries[0]['entry_id']}"
        )
        if diag_status == 200 and diag:
            out("MA client requirement", diag.get("integration_manifest", {}).get("requirements"))
            server = diag.get("data", {}).get("server_info", {})
            out("MA server (diagnostics)", server)
            info["players"] = diag.get("data", {}).get("players", [])
    info["entry_id"] = entries[0]["entry_id"] if entries else None
    return info


async def ma_server_info(base: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as s, s.get(base.rstrip("/") + "/info") as r:
        return await r.json()


async def probe_players(ha: Ha, info: dict[str, Any], name: str) -> str | None:
    print("## Sendspin player")
    reg = await ha.call({"type": "config/entity_registry/list"})
    states = {s["entity_id"]: s for s in await ha.call({"type": "get_states"})}
    entity_id: str | None = None
    for e in reg:
        if e["platform"] != "music_assistant" or not e["entity_id"].startswith("media_player."):
            continue
        st = states.get(e["entity_id"], {})
        if st.get("attributes", {}).get("friendly_name") == name or e.get("original_name") == name:
            entity_id = e["entity_id"]
        elif e["entity_id"] == "media_player." + name.lower().replace(" ", "_"):
            entity_id = e["entity_id"]
        if entity_id == e["entity_id"]:
            out("HA entity", entity_id)
            out("MA player_id (entity unique_id)", e["unique_id"])
            out("state", st.get("state"))
            break
    for p in info.get("players", []):
        if p.get("name") == name or p.get("provider") == "sendspin":
            out(
                "MA player (diagnostics)",
                {k: p.get(k) for k in ("provider", "type", "name", "available", "expose_to_ha")},
            )
    return entity_id


async def probe_library(ha: Ha, entry_id: str) -> dict[str, list[dict[str, Any]]]:
    print("## Library listing (HA service music_assistant.get_library, pages of 500)")
    result: dict[str, list[dict[str, Any]]] = {}
    for kind in ("playlist", "album", "audiobook", "podcast"):
        items: list[dict[str, Any]] = []
        start = time.monotonic()
        offset = 0
        while True:
            resp = await ha.service(
                "music_assistant",
                "get_library",
                {
                    "config_entry_id": entry_id,
                    "media_type": kind,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
                response=True,
                timeout=120,
            )
            page = resp.get("items", [])
            items.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        took = time.monotonic() - start
        result[kind] = items
        out(
            f"{kind}s",
            {
                "count": len(items),
                "seconds": round(took, 2),
                "within_LIBRARY_TIMEOUT": took < LIBRARY_TIMEOUT,
                "uri_examples": [i["uri"] for i in items[:2]],
            },
        )
    return result


async def probe_images(library: dict[str, list[dict[str, Any]]]) -> None:
    print("## Image URLs (fetched without any auth)")
    urls = [i["image"] for items in library.values() for i in items if i.get("image")]
    proxy = sorted({u for u in urls if "/imageproxy" in u})[:3]
    other = sorted({urlparse(u).hostname or "" for u in urls if "/imageproxy" not in u})
    out("image hosts besides MA imageproxy", other)
    async with aiohttp.ClientSession() as s:
        for url in proxy:
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    body = await r.read()
                    out(
                        "imageproxy " + urlparse(url).path[:30] + "…",
                        {"status": r.status, "type": r.content_type, "bytes": len(body)},
                    )
            except (aiohttp.ClientError, TimeoutError) as err:
                out("imageproxy", f"failed: {err!r}")


async def queue(ha: Ha, entity_id: str) -> dict[str, Any]:
    resp = await ha.service(
        "music_assistant", "get_queue", target={"entity_id": entity_id}, response=True
    )
    return resp[entity_id]


def summarize_queue(q: dict[str, Any]) -> dict[str, Any]:
    cur = q.get("current_item") or {}
    media = cur.get("media_item") or {}
    return {
        "queue_id": q.get("queue_id"),
        "items": q.get("items"),
        "current_index": q.get("current_index"),
        "elapsed_time": q.get("elapsed_time"),
        "current_media_type": media.get("media_type"),
        "current_uri": media.get("uri"),
        "current_album_uri": (media.get("album") or {}).get("uri"),
        "duration": cur.get("duration"),
    }


# -- minimal Sendspin player (audio is discarded) ---------------------------------


async def sendspin_client(
    url: str, client_id: str, name: str, ready: asyncio.Event, volume: int = 5
) -> None:
    """Keep a Sendspin player connected (docs/sendspin.md of the app: transition mode)."""

    def msg(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": kind, "payload": payload}

    async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=20) as ws:
        await ws.send_json(
            msg(
                "client/hello",
                {
                    "client_id": client_id,
                    "name": name,
                    "version": 1,
                    "supported_roles": ["player@v1"],
                    "player@v1_support": {
                        "supported_formats": [
                            {"codec": "pcm", "channels": 2, "sample_rate": 48000, "bit_depth": 16}
                        ],
                        "buffer_capacity": 1048576,
                    },
                },
            )
        )

        async def keepalive() -> None:
            for _ in range(5):
                await ws.send_json(msg("client/time", {"client_transmitted": time.monotonic_ns() // 1000}))
                await asyncio.sleep(0.2)
            await ws.send_json(
                msg(
                    "client/state",
                    {
                        "available": True,
                        "player": {
                            "volume": volume,
                            "output_delay_ms": 0,
                            "required_lead_time_ms": 500,
                            "min_buffer_ms": 300,
                            "supported_commands": ["volume"],
                        },
                    },
                )
            )
            ready.set()
            while True:
                await asyncio.sleep(5)
                await ws.send_json(msg("client/time", {"client_transmitted": time.monotonic_ns() // 1000}))

        task = asyncio.create_task(keepalive())
        try:
            async for m in ws:  # audio (binary) and state messages are ignored
                if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            task.cancel()
            if not ws.closed:
                await ws.send_json(msg("client/goodbye", {"reason": "shutdown"}))


# -- playback (only on the test player) -----------------------------------------


async def probe_play(
    ha: Ha,
    entity_id: str,
    library: dict[str, list[dict[str, Any]]],
    volume: float,
) -> None:
    print(f"## Playback on {entity_id} (volume {volume})")
    target = {"entity_id": entity_id}

    async def state() -> str:
        st = await ha.call({"type": "get_states"})
        return next(s["state"] for s in st if s["entity_id"] == entity_id)

    async def play(uri: str, enqueue: str, media_type: str | None = None) -> None:
        data = {"media_id": uri, "enqueue": enqueue}
        if media_type:
            data["media_type"] = media_type
        await ha.service("music_assistant", "play_media", data, target=target)

    await ha.service("media_player", "volume_set", {"volume_level": volume}, target=target)
    try:
        for kind in ("playlist", "album"):
            if not library.get(kind):
                out(kind, "library empty, skipped")
                continue
            uri = None
            for cand in library[kind][:8]:
                try:
                    await play(cand["uri"], "replace")
                except RuntimeError as err:  # e.g. an empty playlist
                    out(f"REPLACE {kind} {cand['uri']}", f"refused: {err}")
                    continue
                uri = cand["uri"]
                break
            if uri is None:
                continue
            await asyncio.sleep(5)
            out(f"REPLACE {kind} {uri}", {"state": await state(), **summarize_queue(await queue(ha, entity_id))})

        for kind in ("podcast", "audiobook"):
            if not library.get(kind):
                out(kind, "library empty, skipped")
                continue
            uri = library[kind][0]["uri"]
            await play(uri, "replace", kind)
            await asyncio.sleep(6)
            q = await queue(ha, entity_id)
            out(f"REPLACE {kind} {uri}", {"state": await state(), **summarize_queue(q)})
            await ha.service("media_player", "media_seek", {"seek_position": 60}, target=target)
            await asyncio.sleep(3)
            out(
                "  after media_seek 60 (HA seconds -> player_queues.seek -> play_index)",
                {"state": await state(), **summarize_queue(await queue(ha, entity_id))},
            )

        if library.get("album"):
            uri = library["album"][0]["uri"]
            await ha.service("media_player", "media_stop", target=target)
            await ha.service("media_player", "clear_playlist", target=target)
            await asyncio.sleep(2)
            out("after stop + clear", {"state": await state(), **summarize_queue(await queue(ha, entity_id))})
            await play(uri, "add")
            await asyncio.sleep(5)
            out(f"ADD album on cleared queue {uri} (autostart?)",
                {"state": await state(), **summarize_queue(await queue(ha, entity_id))})
    finally:
        await ha.service("media_player", "media_stop", target=target)
        await ha.service("media_player", "clear_playlist", target=target)
        await asyncio.sleep(2)
        out("cleanup stop + clear", {"state": await state(), **summarize_queue(await queue(ha, entity_id))})


# -- TTS -------------------------------------------------------------------------


async def probe_tts(ha: Ha, render: list[str]) -> None:
    print("## TTS")
    engines = await ha.call({"type": "tts/engine/list", "language": "de-DE"})
    for p in engines.get("providers", []):
        out(
            "engine " + p["engine_id"],
            {"de-DE supported": bool(p.get("supported_languages")), "languages": p.get("supported_languages")},
        )
        if p["engine_id"] not in render or not p.get("supported_languages"):
            continue
        status, body = await ha.rest(
            "POST",
            "/api/tts_get_url",
            {"engine_id": p["engine_id"], "message": "Hallo, hier ist KidsHub.", "language": "de-DE"},
        )
        if status != 200 or not body:
            out("  tts_get_url", {"status": status})
            continue
        assert ha.session is not None
        start = time.monotonic()
        async with ha.session.get(body["url"]) as r:
            data = await r.read()
        out(
            "  rendered",
            {
                "status": r.status,
                "content_type": r.content_type,
                "extension": urlparse(body["url"]).path.rsplit(".", 1)[-1],
                "bytes": len(data),
                "magic": data[:4].hex(),
                "seconds": round(time.monotonic() - start, 2),
            },
        )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--ha",
        default=os.environ.get("KIDSHUB_HA_URL"),
        help="HA URL, e.g. http://192.168.1.10:8123 (env KIDSHUB_HA_URL)",
    )
    ap.add_argument(
        "--ma",
        default=os.environ.get("KIDSHUB_MA_URL"),
        help="MA base URL for /info, e.g. http://192.168.1.10:8095 (env KIDSHUB_MA_URL)",
    )
    ap.add_argument("--token-file", default="~/.kidshub_ha_token")
    ap.add_argument("--player", default="KidsHub Test", help="MA player name used by --play")
    ap.add_argument("--volume", type=float, default=0.05)
    ap.add_argument("--play", action="store_true")
    ap.add_argument(
        "--sendspin",
        metavar="URL",
        help="connect a minimal Sendspin player (e.g. ws://192.168.1.10:8927/sendspin) "
        "as --client-id/--player for the playback tests (audio is discarded)",
    )
    ap.add_argument("--client-id", default="kidshub-test")
    ap.add_argument(
        "--tts",
        nargs="*",
        default=None,
        metavar="ENGINE",
        help="render one phrase with these TTS entities (default tts.google_ai_tts)",
    )
    args = ap.parse_args()
    if not args.ha or not args.ma:
        ap.error("--ha and --ma (or KIDSHUB_HA_URL / KIDSHUB_MA_URL) are required")
    with open(os.path.expanduser(args.token_file), encoding="utf-8") as f:
        token = f.read().strip()
    async with Ha(args.ha, token) as ha:
        info = await probe_info(ha)
        try:
            server = await ma_server_info(args.ma)
            out("MA /info base_url (client image URLs use this)", server.get("base_url"))
        except (aiohttp.ClientError, TimeoutError) as err:
            out("MA /info", f"failed: {err!r}")
        spin: asyncio.Task[None] | None = None
        if args.sendspin:
            ready = asyncio.Event()
            spin = asyncio.create_task(
                sendspin_client(args.sendspin, args.client_id, args.player, ready)
            )
            await asyncio.wait_for(ready.wait(), 15)
            await asyncio.sleep(3)  # let MA mark the player available
        entity_id = await probe_players(ha, info, args.player)
        library = await probe_library(ha, info["entry_id"])
        await probe_images(library)
        if entity_id:
            out("queue now", summarize_queue(await queue(ha, entity_id)))
        if args.play:
            if entity_id is None:
                raise SystemExit(f"player {args.player!r} not found, no playback test")
            await probe_play(ha, entity_id, library, args.volume)
        if args.tts is None:
            render = []
        else:
            render = args.tts or ["tts.google_ai_tts"]
        await probe_tts(ha, render)
        if spin is not None:
            spin.cancel()


if __name__ == "__main__":
    asyncio.run(main())
