"""Tests for Music Assistant content: browse, favorites, rate, options."""

import asyncio
import hashlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.ma import MaAdapter

from .fake_ma import FakeMa, item

WS_URL = "/api/kidshub/ws"

PL1 = item("library://playlist/1", "playlist", "Lieder")
PL2 = item("library://playlist/2", "playlist", "Gute Nacht")
PL3 = item("library://playlist/3", "playlist", "Papas Rock")
TRACK_A = item("library://track/a", "track", "A", "Band", duration_ms=1000)
TRACK_B = item("library://track/b", "track", "B", "Band")
TRACK_C = item("library://track/c", "track", "C", "Band")
POD = item("library://podcast/1", "podcast", "Kinderpodcast")
EP_OLD = item("library://podcast_episode/1", "episode", "Alt", published=100.0)
EP_NEW = item("library://podcast_episode/2", "episode", "Neu", published=300.0)
EP_MID = item("library://podcast_episode/3", "episode", "Mitte", published=200.0)

OPTIONS = {
    "playlists": [PL1.uri, PL2.uri],
    "audiobooks": [],
    "podcasts": [POD.uri],
}


def _fake(**kwargs) -> FakeMa:
    kwargs.setdefault(
        "library", {"playlist": [PL1, PL2, PL3], "podcast": [POD]}
    )
    kwargs.setdefault(
        "children",
        {
            PL1.uri: [TRACK_A, TRACK_B, TRACK_C],
            PL3.uri: [item("library://track/z", "track", "Z")],
            POD.uri: [EP_OLD, EP_NEW, EP_MID],
        },
    )
    return FakeMa(**kwargs)


async def _setup(hass: HomeAssistant, fake: FakeMa, options=OPTIONS):
    assert await async_setup_component(hass, DOMAIN, {})
    hass.data[DOMAIN].ma = fake
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _connect(client):
    ws = await client.ws_connect(WS_URL)
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": "tok-a", "app_version": "1"}
    )
    assert (await ws.receive_json())["type"] == "auth_ok"
    config = await ws.receive_json()
    assert config["type"] == "config"
    return ws, config


async def _request(ws, id_, type_, **fields):
    await ws.send_json({"type": type_, "id": id_, **fields})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == "result" and msg["id"] == id_:
            return msg


def _uris(res) -> list[str]:
    assert res["success"] is True, res
    return [i["uri"] for i in res["result"]["items"]]


async def test_browse_music_root(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "browse", section="music", sort="default")
    assert _uris(res) == [PL1.uri, PL2.uri]
    assert res["result"]["items"][0]["kind"] == "playlist"
    assert res["result"]["items"][0]["title"] == "Lieder"

    res = await _request(ws, 2, "rate", uri=TRACK_A.uri, kind="track", stars=4)
    assert res["success"] is True, res
    res = await _request(ws, 3, "browse", section="music", sort="default")
    assert _uris(res) == ["kidshub://favorites/track", PL1.uri, PL2.uri]
    assert res["result"]["items"][0]["kind"] == "favorites"
    await ws.close()


async def test_browse_not_allowed(hass: HomeAssistant, hass_client_no_auth) -> None:
    fake = _fake()
    await _setup(hass, fake)
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, "browse", section="music", parent_uri=PL3.uri, sort="default"
    )
    assert res["success"] is False
    assert res["error"]["code"] == "not_allowed"
    assert PL3.uri not in fake.children_calls
    await ws.close()


async def test_browse_podcast_episodes_sorted(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass, _fake())
    store = hass.data[DOMAIN].devices["dev-a"].store
    store.set_progress(
        EP_OLD.uri, position_ms=5000, duration_ms=60000, finished=False
    )
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(
        ws, 1, "browse", section="podcasts", parent_uri=POD.uri, sort="default"
    )
    assert _uris(res) == [EP_OLD.uri, EP_NEW.uri, EP_MID.uri]
    items = res["result"]["items"]
    assert items[0]["kind"] == "episode"
    assert items[0]["progress"] == {
        "position_ms": 5000,
        "duration_ms": 60000,
        "finished": False,
    }
    assert items[1]["progress"]["position_ms"] == 0
    assert all("_published" not in i for i in items)
    await ws.close()


async def test_rate_and_favorites(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    assert (await _request(ws, 1, "rate", uri=TRACK_A.uri, kind="track", stars=5))[
        "success"
    ]
    assert (await _request(ws, 2, "rate", uri=TRACK_B.uri, kind="track", stars=3))[
        "success"
    ]
    res = await _request(ws, 3, "favorites", kind="track")
    assert _uris(res) == [TRACK_A.uri]
    fav = res["result"]["items"][0]
    assert fav["stars"] == 5 and fav["title"] == "A" and fav["kind"] == "track"

    res = await _request(
        ws, 4, "browse", section="music", parent_uri=PL1.uri, sort="stars"
    )
    assert _uris(res) == [TRACK_A.uri, TRACK_B.uri, TRACK_C.uri]
    assert [i["stars"] for i in res["result"]["items"]] == [5, 3, None]

    res = await _request(
        ws, 5, "browse", section="music", parent_uri="kidshub://favorites/track"
    )
    assert _uris(res) == [TRACK_A.uri]
    await ws.close()


async def test_rate_not_allowed(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "rate", uri="library://track/z", kind="track", stars=5)
    assert res["success"] is False
    assert res["error"]["code"] == "not_allowed"
    await ws.close()


async def test_rate_invalid_stars(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    for id_, stars in enumerate((0, 6, "5", 4.5, True), start=1):
        res = await _request(ws, id_, "rate", uri=TRACK_A.uri, kind="track", stars=stars)
        assert res["success"] is False
        assert res["error"]["code"] == "invalid"
    await ws.close()


async def test_ma_unavailable(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake(available=False))
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "browse", section="music", sort="default")
    assert res["success"] is False
    assert res["error"]["code"] == "ma_unavailable"
    res = await _request(ws, 2, "alarm_update", enabled=True)
    assert res["success"] is True
    await ws.close()


async def test_image_url_rewritten(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "browse", section="music", sort="default")
    url = res["result"]["items"][0]["image_url"]
    assert url.startswith("/api/kidshub/img?u=")
    assert url == (
        "/api/kidshub/img?u=http%3A%2F%2Fma.local%3A8095%2Fimageproxy%2Fx&s=256"
    )
    await ws.close()


async def test_sendspin_info(hass: HomeAssistant, hass_client_no_auth) -> None:
    adapter = MaAdapter(hass)
    assert adapter.sendspin_info("abc", "Paul") is None
    MockConfigEntry(
        domain="music_assistant", data={"url": "http://192.168.1.10:8095"}
    ).add_to_hass(hass)
    info = adapter.sendspin_info("abc", "Paul")
    assert info == {
        "url": "ws://192.168.1.10:8927/sendspin",
        "client_id": "kidshub-abc",
        "name": "KidsHub Paul",
    }
    # the config pushed to the app carries the sendspin block
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    ws, config = await _connect(await hass_client_no_auth())
    assert config["sendspin"]["client_id"] == "kidshub-dev-a"
    assert config["sendspin"]["url"] == "ws://192.168.1.10:8927/sendspin"
    await ws.close()


async def test_ma_adapter_unavailable_without_entry(hass: HomeAssistant) -> None:
    adapter = MaAdapter(hass)
    assert adapter.available is False


async def test_options_content(hass: HomeAssistant) -> None:
    fake = _fake(
        library={
            "playlist": [PL1, PL2, PL3],
            "album": [item("library://album/1", "album", "Hörspiel")],
            "audiobook": [item("library://audiobook/1", "audiobook", "Buch")],
            "podcast": [POD],
        }
    )
    entry = await _setup(hass, fake)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "content" in flow["menu_options"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "content"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "content"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "playlists": [PL3.uri],
            "audiobooks": ["library://album/1", "library://audiobook/1"],
            "podcasts": [],
        },
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["playlists"] == [PL3.uri]
    assert entry.options["audiobooks"] == ["library://album/1", "library://audiobook/1"]
    assert entry.options["podcasts"] == []


async def test_options_content_ma_unavailable(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _fake(available=False))
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "content"}
    )
    assert flow["type"] is FlowResultType.FORM
    assert flow["errors"] == {"base": "ma_unavailable"}


def _fake_client(**music):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    return SimpleNamespace(
        connection=SimpleNamespace(connected=True),
        disconnect=AsyncMock(),
        music=SimpleNamespace(**{k: AsyncMock(return_value=v) for k, v in music.items()}),
        get_media_item_image_url=lambda obj: "/imageproxy/abc?size=0",
    )


async def test_ma_adapter_converts_client_items(hass: HomeAssistant) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import Mock

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.kidshub.device import InvalidRequest

    playlist = SimpleNamespace(
        uri="spotify://playlist/p1", name="Lieder", owner="Mama", media_type="playlist"
    )
    track = SimpleNamespace(
        uri="spotify://track/t1",
        name="Lied",
        media_type="track",
        duration=183,
        artists=[SimpleNamespace(name="Band"), SimpleNamespace(name="Chor")],
    )
    episode = SimpleNamespace(
        uri="library://podcast_episode/9",
        name="Folge",
        media_type="podcast_episode",
        duration=60,
        podcast=SimpleNamespace(name="Sendung"),
        metadata=SimpleNamespace(release_date=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    mass = _fake_client(
        get_library_playlists=[playlist],
        get_playlist_tracks=[track],
        get_album_tracks=[track],
        get_podcast_episodes=[episode],
    )
    entry = MockConfigEntry(
        domain="music_assistant",
        data={"url": "http://192.168.1.10:8095"},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())
    adapter = MaAdapter(hass)
    assert adapter.available is True

    (pl,) = await adapter.library("playlist")
    assert (pl.uri, pl.kind, pl.title, pl.subtitle) == (
        "spotify://playlist/p1", "playlist", "Lieder", "Mama"
    )
    assert pl.image == "http://192.168.1.10:8095/imageproxy/abc?size=0"

    (tr,) = await adapter.children("spotify://playlist/p1")
    assert (tr.kind, tr.subtitle, tr.duration_ms) == ("track", "Band, Chor", 183000)
    mass.music.get_playlist_tracks.assert_awaited_with("p1", "spotify")
    (atr,) = await adapter.children("library://album/7")
    assert atr.kind == "album_track"
    (ep,) = await adapter.children("library://podcast/3")
    assert (ep.kind, ep.subtitle) == ("episode", "Sendung")
    assert ep.published == datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    assert await adapter.children("library://audiobook/1") == []
    with pytest.raises(InvalidRequest):
        await adapter.children("not-a-uri")

    mass.connection.connected = False
    assert adapter.available is False


ALBUM = item("library://album/1", "album", "Hörspiel", "Erzähler")
CHAPTER = item("library://track/k1", "album_track", "Kapitel 1")


def _fake_with_album() -> FakeMa:
    fake = _fake()
    fake._library["album"] = [ALBUM]
    fake._children[ALBUM.uri] = [CHAPTER]
    return fake


async def test_rate_spoofed_kind_invalid(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass, _fake())
    ws, _ = await _connect(await hass_client_no_auth())
    res = await _request(ws, 1, "rate", uri=TRACK_A.uri, kind="episode", stars=5)
    assert res["error"]["code"] == "invalid"
    # a playlist is no track
    res = await _request(ws, 2, "rate", uri=PL1.uri, kind="track", stars=5)
    assert res["error"]["code"] == "invalid"
    assert hass.data[DOMAIN].devices["dev-a"].store.ratings() == []
    await ws.close()


async def test_rate_album_track_invalid_album_container_ok(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass, _fake_with_album(), {**OPTIONS, "audiobooks": [ALBUM.uri]})
    ws, _ = await _connect(await hass_client_no_auth())
    for id_, kind in ((1, "album_track"), (2, "track")):
        res = await _request(ws, id_, "rate", uri=CHAPTER.uri, kind=kind, stars=5)
        assert res["error"]["code"] == "invalid"
    res = await _request(ws, 3, "rate", uri=ALBUM.uri, kind="album", stars=5)
    assert res["success"] is True, res
    (rec,) = hass.data[DOMAIN].devices["dev-a"].store.ratings()
    assert (rec.kind, rec.title, rec.subtitle) == ("album", "Hörspiel", "Erzähler")
    res = await _request(ws, 4, "favorites", kind="audiobook")
    assert _uris(res) == [ALBUM.uri]
    assert res["result"]["items"][0]["kind"] == "album"
    assert _uris(await _request(ws, 5, "favorites", kind="track")) == []
    await ws.close()


async def test_ma_adapter_times_out(hass: HomeAssistant) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.kidshub import ma as ma_module
    from custom_components.kidshub.device import MaUnavailable

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    mass = _fake_client()
    mass.music.get_library_playlists = never
    mass.music.get_playlist_tracks = never
    entry = MockConfigEntry(
        domain="music_assistant",
        data={"url": "http://192.168.1.10:8095"},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())
    adapter = MaAdapter(hass)
    with (
        patch.object(ma_module, "MA_TIMEOUT", 0.05),
        patch.object(ma_module, "LIBRARY_TIMEOUT", 0.05),
    ):
        async with asyncio.timeout(2):
            with pytest.raises(MaUnavailable):
                await adapter.library("playlist")
            with pytest.raises(MaUnavailable):
                await adapter.children("spotify://playlist/p1")


async def test_browse_ma_stall_gives_ma_unavailable(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A stalled MA call ends as ma_unavailable instead of hanging the request."""
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.kidshub import ma as ma_module

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    mass = _fake_client()
    mass.music.get_library_playlists = never
    ma_entry = MockConfigEntry(
        domain="music_assistant",
        data={"url": "http://192.168.1.10:8095"},
        state=ConfigEntryState.LOADED,
    )
    ma_entry.add_to_hass(hass)
    ma_entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())
    await _setup(hass, MaAdapter(hass))
    ws, _ = await _connect(await hass_client_no_auth())
    with patch.object(ma_module, "LIBRARY_TIMEOUT", 0.05):
        res = await _request(ws, 1, "browse", section="music", sort="default")
    assert res["error"]["code"] == "ma_unavailable"
    await ws.close()


async def test_options_content_stall_ma_unavailable(hass: HomeAssistant) -> None:
    from unittest.mock import patch

    from custom_components.kidshub import config_flow

    entry = await _setup(hass, _fake(stall_library=True))
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(config_flow, "LIBRARY_TIMEOUT", 0.05):
        async with asyncio.timeout(2):
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "content"}
            )
    assert flow["errors"] == {"base": "ma_unavailable"}


def test_image_registry_bounded() -> None:
    from custom_components.kidshub.content import (
        IMAGE_REGISTRY_SIZE,
        ImageRegistry,
    )

    assert IMAGE_REGISTRY_SIZE == 2000
    reg = ImageRegistry(maxsize=3)
    for url in ("a", "b", "c"):
        reg.add(url)
    reg.add("a")  # refresh "a"; "b" is now the oldest
    reg.add("d")
    assert len(reg) == 3
    assert "b" not in reg
    assert all(u in reg for u in ("a", "c", "d"))
    full = ImageRegistry()
    for i in range(IMAGE_REGISTRY_SIZE + 10):
        full.add(f"http://x/{i}")
    assert len(full) == IMAGE_REGISTRY_SIZE
    assert "http://x/0" not in full


async def test_emitted_images_are_known(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass, _fake())
    content = hass.data[DOMAIN].devices["dev-a"].content
    assert content is not None
    assert not content.is_known_image("http://ma.local:8095/imageproxy/x")
    ws, _ = await _connect(await hass_client_no_auth())
    await _request(ws, 1, "browse", section="music", sort="default")
    assert content.is_known_image("http://ma.local:8095/imageproxy/x")
    assert "http://ma.local:8095/imageproxy/x" in hass.data[DOMAIN].images
    assert not content.is_known_image("http://evil.example/x")
    await ws.close()


async def test_favorites_register_stored_images(hass: HomeAssistant) -> None:
    """Images of stored ratings (e.g. after a restart) become known again."""
    await _setup(hass, _fake())
    device = hass.data[DOMAIN].devices["dev-a"]
    device.store.set_rating(
        TRACK_A.uri, "track", 5, "A", "Band",
        "/api/kidshub/img?u=http%3A%2F%2Fcdn.example%2Fa.jpg&s=256",
    )
    assert not device.content.is_known_image("http://cdn.example/a.jpg")
    device.content.favorites("track")
    assert device.content.is_known_image("http://cdn.example/a.jpg")
