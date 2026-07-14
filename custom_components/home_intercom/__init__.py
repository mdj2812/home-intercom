"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration: YAML, UI, or both. YAML creates a SOURCE_IMPORT entry
so all rooms can register as HA Devices.

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
# YAML setup — imports config as SOURCE_IMPORT entry
# ═══════════════════════════════════════════════════════════════════════


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-based setup. Creates or updates a config entry so all rooms get devices.

    SOURCE_IMPORT entries are read-only in HA UI — the user can't delete them.
    """
    if DOMAIN not in config:
        return True

    yaml_rooms = dict(config[DOMAIN][CONF_ROOMS])
    existing = hass.config_entries.async_entries(DOMAIN)

    if not existing:
        # No entry yet → create SOURCE_IMPORT entry
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={CONF_ROOMS: yaml_rooms},
            )
        )
    else:
        # Entry exists from UI → merge YAML rooms into entry data
        entry = existing[0]
        merged = {**yaml_rooms, **entry.data.get(CONF_ROOMS, {})}
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ROOMS: merged})
    return True


# ═══════════════════════════════════════════════════════════════════════
# Config entry setup — unified path for YAML import + UI
# ═══════════════════════════════════════════════════════════════════════


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Intercom from a config entry.

    Merges entry.data (YAML import or initial UI) with entry.options (UI edits).
    Options take priority over data.
    """
    data_rooms = entry.data.get(CONF_ROOMS, {})
    options_rooms = entry.options.get(CONF_ROOMS, {})
    room_map = {**data_rooms, **options_rooms}

    # Store entry reference BEFORE setup so _register_devices can find it
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["config_entry"] = entry

    await _setup(hass, room_map)

    # Listen for Options flow changes → reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
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
    _register_services(hass, room_map)
    _register_devices(hass, room_map)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


def _register_services(hass: HomeAssistant, room_map: dict[str, Any]) -> None:
    """Register home_intercom.announce service with dynamic room list."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    # Build dynamic schema with configured room IDs
    room_keys = ["all"] + sorted(room_map.keys())
    target_selector = vol.In(room_keys) if room_keys else str
    schema = vol.Schema(
        {
            vol.Required("target", default="all"): target_selector,
            vol.Required("url"): str,
            vol.Optional("volume", default=50): int,
        }
    )

    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, _handle_announce, schema=schema)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: Any
) -> bool:
    """Handle device deletion from HA UI (⋮ → Delete).

    Removes the corresponding room from the config entry.
    """
    # Find which room this device belongs to
    room_id = None
    for domain, rid in device_entry.identifiers:
        if domain == DOMAIN:
            room_id = rid
            break
    if room_id is None:
        return False  # Not our device

    # Remove room from data and options
    new_data = {**entry.data}
    if room_id in new_data.get(CONF_ROOMS, {}):
        rooms = dict(new_data.get(CONF_ROOMS, {}))
        rooms.pop(room_id, None)
        new_data[CONF_ROOMS] = rooms

    new_options = {**entry.options}
    if room_id in new_options.get(CONF_ROOMS, {}):
        rooms = dict(new_options.get(CONF_ROOMS, {}))
        rooms.pop(room_id, None)
        new_options[CONF_ROOMS] = rooms

    hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
    return True


def _register_devices(hass: HomeAssistant, room_map: dict[str, Any]) -> None:
    """Register each room as a Device in HA's device registry.

    Import is lazy (inside function) to avoid crashing HA at module load time.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    entry = hass.data[DOMAIN]["config_entry"]

    for room_id, room in room_map.items():
        entity_id = room.get(CONF_ENTITY_ID, "")
        name = room.get(CONF_NAME, room_id)
        if not entity_id:
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, room_id)},
            name=name,
            manufacturer="Home Intercom",
            model=entity_id,
        )
