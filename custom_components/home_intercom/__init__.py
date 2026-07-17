"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Two config entries:
  - YAML (SOURCE_IMPORT): immutable, read-only in UI
  - UI   (SOURCE_USER):   user-managed, deletable devices

Both coexist under the same domain; announce service merges all rooms.
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
from homeassistant.exceptions import HomeAssistantError
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
    PLATFORMS,
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

YAML_UNIQUE_ID = f"{DOMAIN}_yaml"
UI_UNIQUE_ID = DOMAIN


# ═══════════════════════════════════════════════════════════════════════
# YAML → immutable SOURCE_IMPORT entry
# ═══════════════════════════════════════════════════════════════════════


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Create a read-only YAML config entry. Does NOT merge into UI entry."""
    if DOMAIN not in config:
        return True

    yaml_rooms = dict(config[DOMAIN][CONF_ROOMS])
    entries = hass.config_entries.async_entries(DOMAIN)
    yaml_entry = _find_yaml_entry(entries)

    if yaml_entry:
        # Update existing YAML entry with current config
        hass.config_entries.async_update_entry(yaml_entry, data={CONF_ROOMS: yaml_rooms})
    else:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={CONF_ROOMS: yaml_rooms},
            )
        )
    return True


# ═══════════════════════════════════════════════════════════════════════
# Config entry setup — per-entry, merged globally
# ═══════════════════════════════════════════════════════════════════════


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up or update a config entry. Merges all entries' rooms for services."""
    data_rooms = entry.data.get(CONF_ROOMS, {})
    options_rooms = entry.options.get(CONF_ROOMS, {})
    room_map = {**data_rooms, **options_rooms}

    # Store per-entry rooms
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("entry_rooms", {})
    hass.data[DOMAIN]["entry_rooms"][entry.entry_id] = room_map

    # Full setup with merged rooms from all entries
    await _full_setup(hass, entry)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry. Block YAML entry unloading."""
    if entry.unique_id == YAML_UNIQUE_ID:
        return False  # Keep YAML entry loaded
    if DOMAIN in hass.data:
        hass.data[DOMAIN].setdefault("entry_rooms", {}).pop(entry.entry_id, None)
        remaining = hass.data[DOMAIN].get("entry_rooms", {})
        if not remaining:
            hass.services.async_remove(DOMAIN, SERVICE_ANNOUNCE)
            hass.data.pop(DOMAIN, None)
            return True
        # Reload with remaining rooms
        next_entry_id = next(iter(remaining))
        next_entry = hass.config_entries.async_get_entry(next_entry_id)
        if next_entry:
            await _full_setup(hass, next_entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Block removal of YAML config entry."""
    if entry.unique_id == YAML_UNIQUE_ID:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="yaml_entry_delete_blocked",
            translation_placeholders={"title": entry.title},
        )
    # UI entry — normal cleanup
    return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when Options flow saves changes."""
    await hass.config_entries.async_reload(entry.entry_id)


# ═══════════════════════════════════════════════════════════════════════
# Core setup — merges all entries' rooms
# ═══════════════════════════════════════════════════════════════════════


async def _full_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Full setup: merge all entries, register services + devices."""
    entry_rooms = hass.data.get(DOMAIN, {}).get("entry_rooms", {})
    all_rooms: dict[str, dict[str, Any]] = {}
    for rooms in entry_rooms.values():
        for rid in rooms:
            if rid in all_rooms:
                _LOGGER.warning(
                    "Room key collision: '%s' defined in multiple config entries — "
                    "last entry wins (non-deterministic ordering).",
                    rid,
                )
        all_rooms.update(rooms)

    audio_dir = hass.config.path(WWW_DIR, AUDIO_SUBDIR)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(
        {
            "rooms": all_rooms,
            "audio_dir": audio_dir,
        }
    )

    await hass.async_add_executor_job(lambda: os.makedirs(audio_dir, exist_ok=True))
    hass.data[DOMAIN].setdefault("pwa_token", secrets.token_urlsafe(32))

    # Initialize error/state tracking
    hass.data[DOMAIN].setdefault("errors", {})
    hass.data[DOMAIN].setdefault("states", {})

    register_api_views(hass)
    _register_services(hass, all_rooms)
    _register_devices(hass, entry.entry_id, entry_rooms.get(entry.entry_id, {}))

    # Forward to sensor/number/binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "Home Intercom — %d rooms (%d entries), audio: %s",
        len(all_rooms),
        len(entry_rooms),
        audio_dir,
    )


def _register_services(hass: HomeAssistant, room_map: dict[str, Any]) -> None:
    """Register home_intercom.announce with dynamic room list."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    room_keys = ["all"] + sorted(room_map.keys())
    target_selector = vol.In(room_keys) if room_keys else str
    schema = vol.Schema(
        {
            vol.Required("target", default="all"): target_selector,
            vol.Required("url"): str,
            vol.Optional("volume", default=50): int,
        }
    )

    # Remove old service before re-registering (rooms may have changed)
    if hass.services.has_service(DOMAIN, SERVICE_ANNOUNCE):
        hass.services.async_remove(DOMAIN, SERVICE_ANNOUNCE)
    hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, _handle_announce, schema=schema)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: Any
) -> bool:
    """Allow device deletion only for UI entry, not YAML.

    YAML entry has unique_id=home_intercom_yaml (read-only).
    """
    if entry.unique_id == YAML_UNIQUE_ID:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="yaml_device_delete_blocked",
            translation_placeholders={"name": device_entry.name},
        )

    # Find which room this device belongs to
    room_id = None
    for domain, rid in device_entry.identifiers:
        if domain == DOMAIN:
            room_id = rid
            break
    if room_id is None:
        return False

    # Remove room from UI entry's options
    new_options = {**entry.options}
    if room_id in new_options.get(CONF_ROOMS, {}):
        rooms = dict(new_options[CONF_ROOMS])
        rooms.pop(room_id, None)
        new_options[CONF_ROOMS] = rooms
    hass.config_entries.async_update_entry(entry, options=new_options)
    return True


def _friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Get friendly name from entity registry, fall back to entity_id."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None:
        return entry.original_name or entity_id
    return entity_id


def _register_devices(hass: HomeAssistant, entry_id: str, room_map: dict[str, Any]) -> None:
    """Register devices for ONE config entry. Import is lazy."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    for room_id, room in room_map.items():
        entity_id = room.get(CONF_ENTITY_ID, "")
        name = room.get(CONF_NAME, room_id)
        if not entity_id:
            continue
        device = registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, room_id)},
            name=name,
            manufacturer="Home Intercom",
            model=_friendly_name(hass, entity_id),
        )
        if area_registry.async_get_area(room_id) and device.area_id != room_id:
            registry.async_update_device(device.id, area_id=room_id)


def _find_yaml_entry(entries: list[ConfigEntry]) -> ConfigEntry | None:
    """Find the YAML (SOURCE_IMPORT) entry among existing entries."""
    for entry in entries:
        if entry.unique_id == YAML_UNIQUE_ID:
            return entry
    return None
