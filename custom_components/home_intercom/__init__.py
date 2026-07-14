"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration: YAML, UI, or both. YAML config is independent of UI —
deleting the UI config entry leaves YAML rooms intact.

Sidebar: add a Dashboard with Webpage card pointing to /home_intercom.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .announce import handle_announce_service
from .api import register_api_views
from .const import (
    AUDIO_SUBDIR,
    CONF_ANNOUNCE_VOLUME,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
    SERVICE_ANNOUNCE,
    WWW_DIR,
)

_LOGGER = logging.getLogger(__name__)

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ENTITY_ID): cv.string,
        vol.Optional(CONF_ANNOUNCE_VOLUME): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional(CONF_PAUSE_BUFFER): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_ROOMS): vol.Schema({cv.string: ROOM_SCHEMA}),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


# ═══════════════════════════════════════════════════════════════════════
# YAML setup — independent of config entries
# ═══════════════════════════════════════════════════════════════════════


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-based setup. Does NOT create a config entry.

    YAML rooms live in hass.data[DOMAIN]["yaml_rooms"] and survive
    UI config entry deletion.
    """
    if DOMAIN not in config:
        return True

    yaml_rooms = dict(config[DOMAIN][CONF_ROOMS])
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["yaml_rooms"] = yaml_rooms

    # If no UI config entry exists, do full setup with YAML rooms
    if not hass.config_entries.async_entries(DOMAIN):
        await _setup(hass, yaml_rooms)

    return True


# ═══════════════════════════════════════════════════════════════════════
# Config entry setup — UI-driven
# ═══════════════════════════════════════════════════════════════════════


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Intercom from a UI config entry.

    Merges YAML rooms (if any) with entry rooms. Entry rooms override
    YAML rooms with the same key.
    """
    yaml_rooms = hass.data.get(DOMAIN, {}).get("yaml_rooms", {})
    entry_rooms = {
        **entry.data.get(CONF_ROOMS, {}),
        **entry.options.get(CONF_ROOMS, {}),
    }
    room_map = {**yaml_rooms, **entry_rooms}

    await _setup(hass, room_map)

    # Store entry reference for OptionsFlow access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["config_entry"] = entry

    # Listen for Options flow changes → reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a UI config entry. Restore YAML-only setup if YAML exists."""
    hass.services.async_remove(DOMAIN, SERVICE_ANNOUNCE)
    hass.data[DOMAIN].pop("config_entry", None)

    yaml_rooms = hass.data.get(DOMAIN, {}).get("yaml_rooms", {})
    if yaml_rooms:
        # Re-setup with YAML rooms only
        await _setup(hass, yaml_rooms)
    else:
        hass.data.pop(DOMAIN, None)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when Options flow saves changes."""
    await hass.config_entries.async_reload(entry.entry_id)


# ═══════════════════════════════════════════════════════════════════════
# Core setup (shared by all entry points)
# ═══════════════════════════════════════════════════════════════════════


async def _setup(hass: HomeAssistant, room_map: dict[str, Any]) -> None:
    """Shared setup: register views, services, devices, audio dir."""
    audio_dir = hass.config.path(WWW_DIR, AUDIO_SUBDIR)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(
        {
            "rooms": room_map,
            "audio_dir": audio_dir,
        }
    )

    await hass.async_add_executor_job(lambda: os.makedirs(audio_dir, exist_ok=True))

    # Generate a shared secret token for PWA ↔ backend auth.
    # The Companion App's WebView doesn't carry HA auth for /api/ calls;
    # we use a simple bearer token injected into the HTML instead.
    hass.data[DOMAIN]["pwa_token"] = secrets.token_urlsafe(32)

    register_api_views(hass)
    _register_services(hass)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


def _register_services(hass: HomeAssistant) -> None:
    """Register home_intercom.announce service."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, _handle_announce)
