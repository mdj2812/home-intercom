"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration: YAML or UI (Settings → Devices & Services → Add Integration).

To upgrade from YAML-only: the integration automatically imports your
YAML config as a config entry on next restart. No manual migration needed.

Sidebar: add a Dashboard with Webpage card pointing to /home_intercom.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
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
        vol.Optional(CONF_ANNOUNCE_VOLUME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional(CONF_PAUSE_BUFFER): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        ),
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
# YAML setup (backward compatible — imports as config entry)
# ═══════════════════════════════════════════════════════════════════════


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-based setup: import config as a config entry for unified handling.

    If a UI-created config entry already exists, YAML is ignored.
    """
    if DOMAIN not in config:
        return True

    # If a config entry already exists (created via UI), skip YAML import
    if hass.config_entries.async_entries(DOMAIN):
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={CONF_ROOMS: dict(config[DOMAIN][CONF_ROOMS])},
        )
    )
    return True


# ═══════════════════════════════════════════════════════════════════════
# Config entry setup (unified path for YAML import + UI)
# ═══════════════════════════════════════════════════════════════════════


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Intercom from a config entry.

    Merges rooms from entry.data (YAML import) and entry.options (UI edits).
    Options take priority over data so users can override YAML via UI.
    """
    data_rooms = entry.data.get(CONF_ROOMS, {})
    options_rooms = entry.options.get(CONF_ROOMS, {})
    room_map = {**data_rooms, **options_rooms}

    await _setup(hass, room_map)

    # Store entry reference for OptionsFlow access
    hass.data[DOMAIN]["config_entry"] = entry

    # Listen for Options flow changes → reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: remove services, clear data.

    HTTP views cannot be unregistered in HA, but clearing data references
    effectively disables the integration.
    """
    hass.services.async_remove(DOMAIN, SERVICE_ANNOUNCE)
    hass.data.pop(DOMAIN, None)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when Options flow saves changes."""
    await hass.config_entries.async_reload(entry.entry_id)


# ═══════════════════════════════════════════════════════════════════════
# Core setup (shared by all entry points)
# ═══════════════════════════════════════════════════════════════════════


async def _setup(hass: HomeAssistant, room_map: dict[str, Any]) -> None:
    """Shared setup: register views, services, audio dir."""
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

    _LOGGER.info(
        "Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir
    )


def _register_services(hass: HomeAssistant) -> None:
    """Register home_intercom.announce service."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, _handle_announce)
