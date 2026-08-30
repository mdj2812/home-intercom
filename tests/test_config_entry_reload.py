"""Tests for config-entry unload/reload — platform teardown (#64)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .ha_fakes import install_fake_homeassistant

install_fake_homeassistant()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.home_intercom.__init__ import (  # noqa: E402
    BUTTONS_UNIQUE_ID,
    CONF_ROOMS,
    DOMAIN,
    PLATFORMS,
    UI_UNIQUE_ID,
    YAML_UNIQUE_ID,
    async_unload_entry,
)


def _make_entry(unique_id: str, entry_id: str = "test-entry") -> MagicMock:
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.entry_id = entry_id
    entry.title = "Home Intercom"
    entry.data = {}
    entry.options = {}
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    entry.async_on_unload = MagicMock()
    return entry


@pytest.fixture
def hass():
    h = MagicMock()
    h.data = {
        DOMAIN: {
            "entry_rooms": {
                "ui-entry": {"living": {"name": "Living", "entity_id": "media_player.test"}},
            },
            "rooms": {"living": {"name": "Living", "entity_id": "media_player.test"}},
        }
    }
    h.services = MagicMock()
    h.services.async_remove = MagicMock()
    h.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return h


@pytest.mark.asyncio
async def test_ui_entry_unload_tears_down_platforms(hass):
    """Reload requires platforms to be unloaded first."""
    entry = _make_entry(UI_UNIQUE_ID, "ui-entry")

    ok = await async_unload_entry(hass, entry)

    assert ok is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
    assert DOMAIN not in hass.data
    hass.services.async_remove.assert_called_once()


@pytest.mark.asyncio
async def test_ui_entry_unload_keeps_shared_data_when_other_entries_remain(hass):
    """Unloading one entry while another remains must not wipe hass.data."""
    hass.data[DOMAIN]["entry_rooms"]["yaml-entry"] = {
        "bedroom": {"name": "Bedroom", "entity_id": "media_player.bed"}
    }
    entry = _make_entry(UI_UNIQUE_ID, "ui-entry")

    ok = await async_unload_entry(hass, entry)

    assert ok is True
    assert DOMAIN in hass.data
    assert "ui-entry" not in hass.data[DOMAIN]["entry_rooms"]
    assert "yaml-entry" in hass.data[DOMAIN]["entry_rooms"]


@pytest.mark.asyncio
async def test_ui_entry_unload_fails_when_platform_unload_fails(hass):
    """If platform unload fails, leave hass.data untouched."""
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    entry = _make_entry(UI_UNIQUE_ID, "ui-entry")

    ok = await async_unload_entry(hass, entry)

    assert ok is False
    assert "ui-entry" in hass.data[DOMAIN]["entry_rooms"]


@pytest.mark.asyncio
async def test_button_entry_unload_tears_down_platforms(hass):
    entry = _make_entry(BUTTONS_UNIQUE_ID, "btn-entry")

    ok = await async_unload_entry(hass, entry)

    assert ok is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.asyncio
async def test_yaml_entry_unload_blocked(hass):
    entry = _make_entry(YAML_UNIQUE_ID, "yaml-entry")

    with pytest.raises(HomeAssistantError):
        await async_unload_entry(hass, entry)

    hass.config_entries.async_unload_platforms.assert_not_called()


@pytest.mark.asyncio
async def test_reload_cycle_unloads_before_forward(hass):
    """Simulate async_reload: unload then setup must not double-forward platforms."""
    from custom_components.home_intercom import __init__ as hi

    entry = _make_entry(UI_UNIQUE_ID, "ui-entry")
    entry.data = {CONF_ROOMS: {"living": {"name": "Living", "entity_id": "media_player.test"}}}

    hass = MagicMock()
    hass.data = {}
    hass.config.path = MagicMock(return_value="/config/www/home_intercom_audio")
    hass.async_add_executor_job = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.services = MagicMock()
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_register = MagicMock()
    hass.services.async_remove = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_listen = MagicMock()

    with (
        patch.object(hi, "register_api_views"),
        patch.object(hi, "_register_devices"),
        patch.object(hi, "_ensure_button_entry", AsyncMock(return_value=None)),
        patch.object(hi, "DeviceStore") as mock_store_cls,
    ):
        mock_store = MagicMock()
        mock_store.async_load = AsyncMock()
        mock_store.devices = {}
        mock_store_cls.return_value = mock_store

        await async_unload_entry(hass, entry)
        await hi.async_setup_entry(hass, entry)

    hass.config_entries.async_unload_platforms.assert_awaited_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)
