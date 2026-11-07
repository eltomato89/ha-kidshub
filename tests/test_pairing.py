"""Tests for the pairing registry and PairView."""

import hashlib
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.kidshub.const import DOMAIN
from custom_components.kidshub.pairing import PairingRegistry


async def test_registry_codes_unique_and_single_use() -> None:
    reg = PairingRegistry()
    codes = {reg.start("Paul").code for _ in range(50)}
    assert len(codes) == 50
    assert all(len(c) == 6 and c.isdigit() for c in codes)


async def test_registry_complete_returns_token_once() -> None:
    reg = PairingRegistry()
    pending = reg.start("Paul")
    result = await reg.complete(pending.code, "1.0.0")
    assert result is not None
    got, token = result
    assert got is pending and len(token) >= 40
    assert pending.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert pending.done.is_set()
    assert await reg.complete(pending.code, "1.0.0") is None


async def test_registry_cancel() -> None:
    reg = PairingRegistry()
    pending = reg.start("Paul")
    reg.cancel(pending.code)
    assert await reg.complete(pending.code, "1.0.0") is None


async def test_pair_wrong_code(hass: HomeAssistant, hass_client_no_auth) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client_no_auth()
    resp = await client.post("/api/kidshub/pair", json={"code": "000000"})
    assert resp.status == 403
    assert await resp.json() == {"error": "invalid_code"}


async def test_pair_malformed_body(hass: HomeAssistant, hass_client_no_auth) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client_no_auth()
    resp = await client.post("/api/kidshub/pair", data="nope")
    assert resp.status == 403


async def test_pair_code_single_use(hass: HomeAssistant, hass_client_no_auth) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    pending = hass.data[DOMAIN].pairing.start("Paul")
    client = await hass_client_no_auth()
    body = {"code": pending.code, "app_version": "1.0.0", "device_model": "x"}
    assert (await client.post("/api/kidshub/pair", json=body)).status == 200
    assert (await client.post("/api/kidshub/pair", json=body)).status == 403


async def test_pair_expired(hass: HomeAssistant, hass_client_no_auth, freezer) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    pending = hass.data[DOMAIN].pairing.start("Paul")
    client = await hass_client_no_auth()
    freezer.tick(timedelta(seconds=601))
    resp = await client.post("/api/kidshub/pair", json={"code": pending.code})
    assert resp.status == 403


async def test_pair_no_store_header(hass: HomeAssistant, hass_client_no_auth) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client_no_auth()
    resp = await client.post("/api/kidshub/pair", json={"code": "000000"})
    assert resp.headers["Cache-Control"] == "no-store"


async def test_pair_lockout_after_10_failures(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    pending = hass.data[DOMAIN].pairing.start("Paul")
    client = await hass_client_no_auth()
    for _ in range(10):
        resp = await client.post("/api/kidshub/pair", json={"code": "x"})
        assert resp.status == 403
    resp = await client.post("/api/kidshub/pair", json={"code": pending.code})
    assert resp.status == 403
    assert pending.aborted


async def test_pair_nine_failures_then_success(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    pending = hass.data[DOMAIN].pairing.start("Paul")
    client = await hass_client_no_auth()
    for _ in range(9):
        await client.post("/api/kidshub/pair", data="nope")
    resp = await client.post("/api/kidshub/pair", json={"code": pending.code})
    assert resp.status == 200


async def test_start_resets_failures() -> None:
    reg = PairingRegistry()
    reg.start("A")
    for _ in range(9):
        reg.register_failure()
    second = reg.start("B")
    reg.register_failure()
    assert not second.aborted
    assert await reg.complete(second.code, "1") is not None


async def test_no_pending_no_counting() -> None:
    reg = PairingRegistry()
    for _ in range(20):
        assert await reg.complete("000000", "1") is None
    pending = reg.start("A")
    assert await reg.complete(pending.code, "1") is not None


async def test_completed_pairing_token_valid_until_loaded() -> None:
    """The just-issued token counts until the device entry is loaded."""
    reg = PairingRegistry()
    pending = reg.start("Paul")
    _, token = await reg.complete(pending.code, "1")
    assert reg.fresh_token_valid(pending.device_id, token)
    assert not reg.fresh_token_valid(pending.device_id, "nope")
    assert not reg.fresh_token_valid("other", token)
    reg.mark_loaded(pending.device_id)
    assert not reg.fresh_token_valid(pending.device_id, token)


async def test_completed_pairing_expires(freezer) -> None:
    reg = PairingRegistry()
    pending = reg.start("Paul")
    _, token = await reg.complete(pending.code, "1")
    freezer.tick(timedelta(seconds=601))
    assert not reg.fresh_token_valid(pending.device_id, token)


async def test_wait_loaded() -> None:
    import asyncio

    reg = PairingRegistry()
    pending = reg.start("Paul")
    await reg.complete(pending.code, "1")
    assert await reg.wait_loaded(pending.device_id, 0.01) is False
    waiter = asyncio.ensure_future(reg.wait_loaded(pending.device_id, 2))
    await asyncio.sleep(0)
    reg.mark_loaded(pending.device_id)
    assert await waiter is True
    # unknown device: nothing to wait for
    assert await reg.wait_loaded("ghost", 2) is False
