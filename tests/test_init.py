"""Test setup and unload of KidsHub entries."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_and_unload_device_entry(hass: HomeAssistant) -> None:
    """A device entry loads and unloads cleanly."""
    entry = MockConfigEntry(
        domain="kidshub",
        data={
            "entry_type": "device",
            "device_id": "abc",
            "child_name": "Paul",
            "token_hash": "x",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
