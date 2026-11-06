"""Tests for the KidsHub config flow."""

import hashlib

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kidshub.const import DOMAIN


async def test_pair_flow_success(hass: HomeAssistant, hass_client_no_auth) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["device", "settings"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"child_name": "Paul"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_pairing"
    code = result["description_placeholders"]["code"]
    assert len(code) == 6 and code.isdigit()

    client = await hass_client_no_auth()
    resp = await client.post(
        "/api/kidshub/pair",
        json={"code": code, "app_version": "1.0.0", "device_model": "x"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body["token"]) >= 40
    assert body["child_name"] == "Paul"

    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["entry_type"] == "device"
    assert data["device_id"] == body["device_id"]
    assert data["child_name"] == "Paul"
    assert data["token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()
    assert body["token"] not in data.values()
    assert result["result"].unique_id == body["device_id"]


async def test_settings_entry_once(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"tts_engine": "tts.home_assistant_cloud", "tts_language": "de-DE"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"entry_type": "settings"}
    assert result["options"]["tts_language"] == "de-DE"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_pairing_timeout(hass: HomeAssistant, monkeypatch) -> None:
    import custom_components.kidshub.config_flow as cf

    monkeypatch.setattr(cf, "PAIRING_TIMEOUT", 0.01)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"child_name": "Paul"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_timeout"
    assert not hass.data[DOMAIN].pairing._pending
