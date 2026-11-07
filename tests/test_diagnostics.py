"""Tests for the config entry diagnostics."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.kidshub.const import DOMAIN

TOKEN_HASH = hashlib.sha256(b"tok-a").hexdigest()


def _ma(hass: HomeAssistant) -> None:
    mass = SimpleNamespace(
        connection=SimpleNamespace(connected=True),
        disconnect=AsyncMock(),
        server_info=SimpleNamespace(
            base_url="http://192.168.1.10:8095",
            internal_url="http://192.168.1.10:8095",
            homeassistant_addon=True,
            server_version="2.10.9",
        ),
    )
    entry = MockConfigEntry(
        domain="music_assistant",
        data={"url": "http://d5369777-music-assistant:8094"},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(mass=mass, listen_task=Mock())


async def test_device_diagnostics(hass: HomeAssistant, hass_client) -> None:
    _ma(hass)
    assert await async_setup_component(hass, "diagnostics", {})
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev-a",
        title="Paul",
        data={
            "entry_type": "device",
            "device_id": "dev-a",
            "child_name": "Paul",
            "token_hash": TOKEN_HASH,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert TOKEN_HASH not in json.dumps(diag)
    assert diag["entry"]["data"]["token_hash"] == "**REDACTED**"
    assert diag["entry"]["data"]["device_id"] == "dev-a"
    assert diag["versions"]["integration"] == "0.1.5"
    assert diag["versions"]["ma_server"] == "2.10.9"
    assert diag["versions"]["home_assistant"]
    assert diag["device"]["connected"] is False
    assert diag["sendspin"]["info"] == {
        "url": "ws://192.168.1.10:8927/sendspin",
        "client_id": "kidshub-dev-a",
        "name": "KidsHub Paul",
    }
    assert diag["sendspin"]["source"] == "server_info"
    ma = diag["sendspin"]["ma"]
    assert ma["connected"] is True
    assert ma["entry_url"] == "http://d5369777-music-assistant:8094"
    assert ma["server_info"]["base_url"] == "http://192.168.1.10:8095"
    assert ma["server_info"]["homeassistant_addon"] is True


async def test_settings_diagnostics(hass: HomeAssistant, hass_client) -> None:
    _ma(hass)
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="KidsHub",
        data={"entry_type": "settings"},
        options={
            "tts_engine": "tts.x",
            "tts_language": "de-DE",
            "tts_options": {"api_key": "secret", "token": "t"},
            "sendspin_url": "ws://10.1.2.3:8927/sendspin",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert "secret" not in json.dumps(diag)
    assert diag["entry"]["options"]["tts_engine"] == "tts.x"
    assert diag["sendspin"]["url"] == "ws://10.1.2.3:8927/sendspin"
    assert diag["sendspin"]["source"] == "override"
    assert diag["versions"]["integration"] == "0.1.5"
    assert "device" not in diag


async def test_settings_diagnostics_prewarm(hass: HomeAssistant, hass_client) -> None:
    _ma(hass)
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="KidsHub",
        data={"entry_type": "settings"},
        options={"tts_engine": "tts.x", "tts_prewarm": False},
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["tts_prewarm"] == {
        "enabled": False,
        "paused_until": None,
        "reason": None,
        "queue": 0,
    }
