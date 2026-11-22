"""Tests for announcements: the notify entity and ``kidshub.announce``."""

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.kidshub.const import DOMAIN

from .test_media_views import TOKEN, _patch_tts, _setup


async def _connect(client):
    ws = await client.ws_connect("/api/kidshub/ws")
    await ws.send_json(
        {"type": "auth", "device_id": "dev-a", "token": TOKEN, "app_version": "1"}
    )
    for expected in ("auth_ok", "config", "now_playing", "queue"):
        assert (await ws.receive_json())["type"] == expected
    return ws


async def _next(ws, type_):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        if msg["type"] == type_:
            return msg


def _notify_entity(hass) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "notify", DOMAIN, "dev-a_announcement"
    )
    assert entity_id
    return entity_id


def _device_id(hass) -> str:
    entry_id = next(
        e.entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get("device_id") == "dev-a"
    )
    dev = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "dev-a"), entry_id)
    assert dev is not None
    return dev.id


async def test_notify_entity_pushes_announce(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    data = await _setup(hass)
    # speak buttons off and night do not matter for announcements
    await data.devices["dev-a"].update_settings({"speak_enabled": False})
    ws = await _connect(await hass_client_no_auth())
    calls, (p1, p2) = _patch_tts()
    with p1, p2:
        await hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": _notify_entity(hass), "message": "Essen ist fertig!"},
            blocking=True,
        )
    msg = await _next(ws, "announce")
    assert msg["text"] == "Essen ist fertig!"
    assert msg["url"].startswith("/api/kidshub/tts/")
    assert isinstance(msg["id"], str) and len(msg["id"]) == 36
    assert "volume" not in msg
    assert len(calls) == 1
    await ws.close()


async def test_announce_service_volume_capped(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    data = await _setup(hass)
    await data.devices["dev-a"].update_settings({"max_volume": 50})
    ws = await _connect(await hass_client_no_auth())
    long_text = "x" * 500  # longer than a speak button text
    _, (p1, p2) = _patch_tts()
    with p1, p2:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"device_id": [_device_id(hass)], "message": long_text, "volume": 90},
            blocking=True,
        )
        msg = await _next(ws, "announce")
        assert msg["text"] == long_text and msg["volume"] == 50
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"entity_id": _notify_entity(hass), "message": "Hallo", "volume": 20},
            blocking=True,
        )
        msg = await _next(ws, "announce")
        assert msg["text"] == "Hallo" and msg["volume"] == 20
    await ws.close()


async def test_announce_offline_raises(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"device_id": _device_id(hass), "message": "Hallo"},
            blocking=True,
        )
    assert err.value.translation_key == "not_connected"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "notify",
            "send_message",
            {"entity_id": _notify_entity(hass), "message": "Hallo"},
            blocking=True,
        )


async def test_announce_invalid(hass: HomeAssistant) -> None:
    import voluptuous as vol

    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"device_id": _device_id(hass), "message": "x" * 501},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "announce", {"message": "Hallo"}, blocking=True
        )
    assert err.value.translation_key == "no_target"
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "announce",
            {"entity_id": "light.other", "message": "Hallo"},
            blocking=True,
        )
    assert err.value.translation_key == "unknown_target"
