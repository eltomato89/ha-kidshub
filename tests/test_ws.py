"""Tests for the KidsHub WebSocket session and device runtime."""

import asyncio
import hashlib

import pytest
from aiohttp import WSMsgType
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub import ws as ws_module
from custom_components.kidshub.const import DOMAIN

WS_URL = "/api/kidshub/ws"


def _make_entry(hass: HomeAssistant, device_id: str, token: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=device_id,
        data={
            "entry_type": "device",
            "device_id": device_id,
            "child_name": "Paul",
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, device_id="dev-a", token="tok-a"):
    entry = _make_entry(hass, device_id, token)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


# aiohttp re-arms the (harmless) heartbeat timer when the client's close frame
# arrives after a server-initiated close.
lingering_heartbeat = pytest.mark.parametrize("expected_lingering_timers", [True])


async def _closed_by_server(ws) -> None:
    """Wait for the server-side close, then let the server finish."""
    msg = await asyncio.wait_for(ws.receive(), 2)
    if msg.type is WSMsgType.TEXT:  # auth_invalid precedes the close
        assert msg.json() == {"type": "auth_invalid"}
        msg = await asyncio.wait_for(ws.receive(), 2)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    await ws.close()
    await asyncio.sleep(0.05)


async def _auth(client, device_id="dev-a", token="tok-a"):
    ws = await client.ws_connect(WS_URL)
    await ws.send_json(
        {"type": "auth", "device_id": device_id, "token": token, "app_version": "1"}
    )
    return ws


async def test_auth_ok_then_config(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    assert (await ws.receive_json()) == {"type": "auth_ok"}
    config = await ws.receive_json()
    assert config["type"] == "config"
    assert config["child_name"] == "Paul"
    assert config["version"] >= 1
    assert config["sendspin"] is None
    assert hass.data[DOMAIN].devices["dev-a"].connected
    await ws.close()


@lingering_heartbeat
async def test_auth_wrong_token(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth(), token="nope")
    assert (await ws.receive_json()) == {"type": "auth_invalid"}
    await _closed_by_server(ws)


@lingering_heartbeat
async def test_auth_unknown_device(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth(), device_id="ghost")
    assert (await ws.receive_json()) == {"type": "auth_invalid"}
    await _closed_by_server(ws)


@lingering_heartbeat
async def test_auth_token_of_other_device(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass, "dev-a", "tok-a")
    _make_entry(hass, "dev-b", "tok-b")
    await hass.config_entries.async_setup(
        hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, "dev-b").entry_id
    )
    ws = await _auth(await hass_client_no_auth(), device_id="dev-a", token="tok-b")
    assert (await ws.receive_json()) == {"type": "auth_invalid"}
    await _closed_by_server(ws)


@lingering_heartbeat
async def test_auth_malformed_first_message(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    ws = await (await hass_client_no_auth()).ws_connect(WS_URL)
    await ws.send_json({"type": "state"})
    assert (await ws.receive_json()) == {"type": "auth_invalid"}
    await _closed_by_server(ws)


@lingering_heartbeat
async def test_auth_timeout(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "AUTH_TIMEOUT", 0.1)
    await _setup(hass)
    ws = await (await hass_client_no_auth()).ws_connect(WS_URL)
    await _closed_by_server(ws)


async def test_unknown_request(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"type": "foo", "id": 1})
    res = await ws.receive_json()
    assert res["type"] == "result" and res["id"] == 1
    assert res["success"] is False and res["error"]["code"] == "invalid"
    await ws.send_json({"type": "foo", "id": 2})
    assert (await ws.receive_json())["id"] == 2
    await ws.close()


async def test_handler_errors_map_to_codes(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from custom_components.kidshub.device import MaUnavailable, NotAllowed

    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]

    async def deny(msg):
        raise NotAllowed("no")

    async def down(msg):
        raise MaUnavailable("down")

    async def boom(msg):
        raise RuntimeError("x")

    async def ok(msg):
        return {"x": 1}

    for name, fn in (("deny", deny), ("down", down), ("boom", boom), ("ok", ok)):
        device.register_handler(name, fn)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    codes = {}
    for i, name in enumerate(("deny", "down", "boom", "ok"), 1):
        await ws.send_json({"type": name, "id": i})
        res = await ws.receive_json()
        codes[name] = res["error"]["code"] if not res["success"] else res["result"]
    assert codes == {
        "deny": "not_allowed",
        "down": "ma_unavailable",
        "boom": "invalid",
        "ok": {"x": 1},
    }
    await ws.close()


async def test_alarm_update_pushes_config(
    hass: HomeAssistant, hass_client_no_auth, hass_storage
) -> None:
    entry = await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    first = await ws.receive_json()
    await ws.send_json({"type": "alarm_update", "id": 5, "time": "06:30"})
    res = await ws.receive_json()
    assert res["type"] == "result" and res["success"] is True
    config = await ws.receive_json()
    assert config["type"] == "config"
    assert config["alarm"]["time"] == "06:30"
    assert config["version"] > first["version"]
    await ws.close()

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = hass.data[DOMAIN].devices["dev-a"].store
    assert store.settings["alarm"]["time"] == "06:30"


@pytest.mark.parametrize(
    "payload",
    [
        {"time": "25:00"},
        {"time": "6:30"},
        {"time": 630},
        {"enabled": "yes"},
        {"with_light": 1},
        {"weekdays": [0, 0]},
        {"weekdays": [7]},
        {"weekdays": [True]},
        {"weekdays": "1"},
        {"lead_minutes": 5},
        {"bogus": 1},
        {},
    ],
)
async def test_alarm_update_invalid(
    hass: HomeAssistant, hass_client_no_auth, payload
) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"type": "alarm_update", "id": 1, **payload})
    res = await ws.receive_json()
    assert res["success"] is False and res["error"]["code"] == "invalid"
    assert hass.data[DOMAIN].devices["dev-a"].store.settings["alarm"]["time"] == "07:00"
    await ws.close()


async def test_alarm_update_weekdays_sorted(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json(
        {"type": "alarm_update", "id": 1, "weekdays": [6, 2, 0], "enabled": True}
    )
    assert (await ws.receive_json())["success"] is True
    config = await ws.receive_json()
    assert config["alarm"]["weekdays"] == [0, 2, 6]
    assert config["alarm"]["enabled"] is True
    await ws.close()


async def test_second_connection_replaces_first(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    first = await _auth(client)
    await first.receive_json()
    await first.receive_json()
    second = await _auth(client)
    assert (await second.receive_json()) == {"type": "auth_ok"}
    msg = await asyncio.wait_for(first.receive(), 2)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    assert hass.data[DOMAIN].devices["dev-a"].connected
    await second.close()


async def test_offline_changes_delivered_on_connect(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    await device.update_settings({"max_volume": 50})
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    assert (await ws.receive_json())["max_volume"] == 50
    await ws.close()


async def test_state_stored_and_signalled(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    calls = []
    async_dispatcher_connect(hass, "kidshub_dev-a_updated", lambda: calls.append(1))
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    await hass.async_block_till_done()
    n = len(calls)
    assert n >= 1  # attach
    state = {"type": "state", "playback": "playing", "volume": 45}
    await ws.send_json(state)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if device.last_state:
            break
    assert device.last_state == state
    assert len(calls) > n
    await ws.close()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not device.connected:
            break
    assert not device.connected


async def test_unload_closes_session(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _setup(hass)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    assert await hass.config_entries.async_unload(entry.entry_id)
    msg = await asyncio.wait_for(ws.receive(), 2)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    assert "dev-a" not in hass.data[DOMAIN].devices


@lingering_heartbeat
async def test_auth_deadline_is_absolute(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "AUTH_TIMEOUT", 0.3)
    await _setup(hass)
    ws = await (await hass_client_no_auth()).ws_connect(WS_URL)

    async def spam() -> None:
        while True:
            await ws.ping()
            await ws.send_bytes(b"x")
            await asyncio.sleep(0.05)

    task = asyncio.create_task(spam())
    try:
        msg = await asyncio.wait_for(ws.receive(), 2)
        if msg.type is WSMsgType.TEXT:
            assert msg.json() == {"type": "auth_invalid"}
            msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await ws.close()
    await asyncio.sleep(0.05)


async def test_slow_handler_does_not_block(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    release = asyncio.Event()

    async def slow(msg):
        await release.wait()
        return {}

    async def fast(msg):
        return {"fast": True}

    device.register_handler("slow", slow)
    device.register_handler("fast", fast)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    calls = []
    async_dispatcher_connect(hass, device.signal, lambda: calls.append(1))
    await ws.send_json({"type": "slow", "id": 1})
    await ws.send_json({"type": "state", "playback": "idle"})
    await ws.send_json({"type": "fast", "id": 2})
    res = await asyncio.wait_for(ws.receive_json(), 2)
    assert res["id"] == 2 and res["result"] == {"fast": True}
    assert device.last_state == {"type": "state", "playback": "idle"}
    assert calls
    release.set()
    assert (await asyncio.wait_for(ws.receive_json(), 2))["id"] == 1
    await ws.close()


async def test_close_cancels_inflight_handler(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(msg):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    device.register_handler("hang", hang)
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()
    await ws.send_json({"type": "hang", "id": 1})
    await asyncio.wait_for(started.wait(), 2)
    await ws.close()
    await asyncio.wait_for(cancelled.wait(), 2)


async def test_push_on_dead_transport_does_not_raise(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    ws = await _auth(await hass_client_no_auth())
    await ws.receive_json()
    await ws.receive_json()

    async def broken(_msg):
        raise ConnectionResetError("gone")

    session = device._session
    session._ws.send_json = broken
    await device.push({"type": "config"})
    await device.update_settings({"max_volume": 40})
    await ws.close()


async def _closed_without_auth_invalid(ws) -> None:
    msg = await asyncio.wait_for(ws.receive(), 2)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED), msg
    await ws.close()
    await asyncio.sleep(0.05)


@lingering_heartbeat
async def test_auth_known_entry_not_loaded_closes_without_invalid(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Reload/startup race: entry exists, token matches, device not loaded."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "dev-a" not in hass.data[DOMAIN].devices
    ws = await _auth(await hass_client_no_auth())
    await _closed_without_auth_invalid(ws)


@lingering_heartbeat
async def test_auth_known_entry_not_loaded_wrong_token_is_invalid(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    ws = await _auth(await hass_client_no_auth(), token="nope")
    assert (await ws.receive_json()) == {"type": "auth_invalid"}
    await _closed_by_server(ws)


async def test_failing_connected_callback_is_logged_not_fatal(
    hass: HomeAssistant, hass_client_no_auth, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(hass)
    device = hass.data[DOMAIN].devices["dev-a"]
    ran: list[str] = []

    async def boom() -> None:
        raise RuntimeError("boom")

    async def after() -> None:
        ran.append("after")

    device._connected_callbacks.insert(0, boom)
    device.register_connected_callback(after)
    ws = await _auth(await hass_client_no_auth())
    assert (await ws.receive_json())["type"] == "auth_ok"
    assert (await ws.receive_json())["type"] == "config"
    await ws.send_json({"type": "foo", "id": 7})
    while (msg := await asyncio.wait_for(ws.receive_json(), 2))["type"] != "result":
        pass
    assert msg["id"] == 7
    assert ran == ["after"]
    assert device.connected
    assert "boom" in caplog.text
    await ws.close()
