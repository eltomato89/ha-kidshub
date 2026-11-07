"""Tests for the Sendspin address: never a container host, fallbacks, override.

Live (2026-11-07): the integration sent ``ws://d5369777-music-assistant:8927/sendspin``
(the add-on container host from hassio discovery), unreachable from the Echo.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.ma import MaAdapter, usable_host

CONTAINER = "http://d5369777-music-assistant:8094"
SOURCE_IP = "custom_components.kidshub.ma.async_get_source_ip"


def _mass(base_url=None, internal_url=None, addon=True, version="2.10.9"):
    return SimpleNamespace(
        connection=SimpleNamespace(connected=True),
        disconnect=AsyncMock(),
        server_info=SimpleNamespace(
            base_url=base_url,
            internal_url=internal_url,
            homeassistant_addon=addon,
            server_version=version,
        ),
    )


def _ma_entry(hass: HomeAssistant, mass, url=CONTAINER):
    entry = MockConfigEntry(
        domain="music_assistant", data={"url": url}, state=ConfigEntryState.LOADED
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())
    return entry


def _settings_entry(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="KidsHub",
        data={"entry_type": "settings"},
        options={"tts_engine": "tts.x", "tts_language": "de-DE", **options},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    ("host", "entry_host", "addon", "ok"),
    [
        ("192.168.1.10", "d5369777-music-assistant", True, True),
        ("ma.example.org", "d5369777-music-assistant", True, True),
        ("d5369777-music-assistant", "d5369777-music-assistant", True, False),
        ("music-assistant", "192.168.1.10", False, False),  # no dot, no IP
        ("d5369777-music-assistant.local.hass.io", "x", True, False),
        ("ma.lan", "ma.lan", True, False),  # equals the add-on entry host
        ("192.168.1.10", "192.168.1.10", False, True),  # standalone MA by IP
        ("ma.lan", "ma.lan", False, True),  # standalone MA by name
        ("127.0.0.1", "x", False, False),
        ("0.0.0.0", "x", False, False),
        ("fd00::10", "x", False, True),
        ("localhost", "x", False, False),
        ("", "x", False, False),
        (None, "x", False, False),
    ],
)
def test_usable_host(host, entry_host, addon, ok) -> None:
    assert usable_host(host, entry_host=entry_host, addon=addon) is ok


async def test_server_info_internal_url_preferred(hass: HomeAssistant) -> None:
    _ma_entry(hass, _mass(base_url="http://10.0.0.1:8095", internal_url="http://192.168.1.10:8095"))
    adapter = MaAdapter(hass)
    await adapter.async_refresh_sendspin()
    assert adapter.sendspin_url() == ("ws://192.168.1.10:8927/sendspin", "server_info")
    info = adapter.sendspin_info("abc", "Paul")
    assert info == {
        "url": "ws://192.168.1.10:8927/sendspin",
        "client_id": "kidshub-abc",
        "name": "KidsHub Paul",
    }


async def test_container_base_url_falls_back_to_ma_info(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """server_info carries the container host: ask MA's /info over HTTP."""
    _ma_entry(hass, _mass(base_url="http://d5369777-music-assistant:8095"))
    aioclient_mock.get(
        f"{CONTAINER}/info",
        json={"base_url": "http://192.168.1.10:8095", "homeassistant_addon": True},
    )
    adapter = MaAdapter(hass)
    # before the lookup nothing usable is known: no container host, no block
    assert adapter.sendspin_info("abc", "Paul") is None
    await adapter.async_refresh_sendspin()
    assert adapter.sendspin_url() == ("ws://192.168.1.10:8927/sendspin", "ma_info")
    assert "music-assistant" not in adapter.sendspin_info("abc", "Paul")["url"]
    # cached: a second refresh does not ask again
    await adapter.async_refresh_sendspin()
    assert aioclient_mock.call_count == 1


async def test_addon_without_usable_info_uses_ha_host_ip(
    hass: HomeAssistant, aioclient_mock
) -> None:
    _ma_entry(hass, _mass(base_url="http://d5369777-music-assistant:8095"))
    aioclient_mock.get(f"{CONTAINER}/info", exc=TimeoutError())
    adapter = MaAdapter(hass)
    with patch(SOURCE_IP, AsyncMock(return_value="192.168.1.20")):
        await adapter.async_refresh_sendspin()
    assert adapter.sendspin_url() == ("ws://192.168.1.20:8927/sendspin", "ha_host")


async def test_standalone_ma_without_usable_url_gives_none(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """Not an add-on: the HA host is not MA's host, so no guess at all."""
    _ma_entry(hass, _mass(base_url="http://music:8095", addon=False), url="http://ma.lan:8095")
    aioclient_mock.get("http://ma.lan:8095/info", json={"base_url": "http://music:8095"})
    adapter = MaAdapter(hass)
    with patch(SOURCE_IP, AsyncMock(return_value="192.168.1.20")) as source_ip:
        await adapter.async_refresh_sendspin()
    source_ip.assert_not_called()
    assert adapter.sendspin_url() == (None, "none")
    assert adapter.sendspin_info("abc", "Paul") is None


async def test_ha_host_ip_never_used_when_not_connected(hass: HomeAssistant) -> None:
    mass = _mass(base_url="http://d5369777-music-assistant:8095")
    mass.connection.connected = False
    _ma_entry(hass, mass)
    adapter = MaAdapter(hass)
    await adapter.async_refresh_sendspin()
    assert adapter.sendspin_info("abc", "Paul") is None


async def test_override_wins(hass: HomeAssistant) -> None:
    _ma_entry(hass, _mass(base_url="http://192.168.1.10:8095"))
    settings = _settings_entry(hass, sendspin_url="ws://10.1.2.3:9000/sendspin")
    adapter = MaAdapter(hass)
    assert adapter.sendspin_url() == ("ws://10.1.2.3:9000/sendspin", "override")
    assert adapter.sendspin_info("abc", "Paul")["url"] == "ws://10.1.2.3:9000/sendspin"
    hass.config_entries.async_update_entry(
        settings, options={**settings.options, "sendspin_url": ""}
    )
    assert adapter.sendspin_url() == ("ws://192.168.1.10:8927/sendspin", "server_info")


async def test_settings_options_flow_sets_and_clears_override(
    hass: HomeAssistant,
) -> None:
    settings = _settings_entry(hass)
    result = await hass.config_entries.options.async_init(settings.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"sendspin_url": "http://nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"sendspin_url": "invalid_sendspin_url"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"sendspin_url": " ws://192.168.1.10:8927/sendspin "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert settings.options["sendspin_url"] == "ws://192.168.1.10:8927/sendspin"
    assert settings.options["tts_engine"] == "tts.x"  # other options kept

    result = await hass.config_entries.options.async_init(settings.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "sendspin_url" not in settings.options
    assert settings.options["tts_language"] == "de-DE"


async def test_override_change_pushes_config(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    _ma_entry(hass, _mass(base_url="http://192.168.1.10:8095"))
    settings = _settings_entry(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    device = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": hashlib.sha256(b"tok-a").hexdigest(),
        },
    )
    device.add_to_hass(hass)
    assert await hass.config_entries.async_setup(device.entry_id)
    await hass.async_block_till_done()
    ws = await (await hass_client_no_auth()).ws_connect("/api/kidshub/ws")
    await ws.send_json({"type": "auth", "device_id": "dev-a", "token": "tok-a"})
    assert (await ws.receive_json())["type"] == "auth_ok"
    config = await ws.receive_json()
    assert config["sendspin"]["url"] == "ws://192.168.1.10:8927/sendspin"

    result = await hass.config_entries.options.async_init(settings.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"sendspin_url": "ws://10.1.2.3:8927/sendspin"}
    )
    await hass.async_block_till_done()
    while (msg := await ws.receive_json())["type"] != "config":
        pass
    assert msg["sendspin"]["url"] == "ws://10.1.2.3:8927/sendspin"
    assert msg["version"] > config["version"]
    await ws.close()
