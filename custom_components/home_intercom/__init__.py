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
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .announce import handle_announce_service
from .api import register_api_views
from .const import (
    AUDIO_SUBDIR,
    BUTTONS_UNIQUE_ID,
    CONF_ANNOUNCE_VOLUME,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
    PLATFORMS,
    PWA_TOKEN_STORAGE_KEY,
    PWA_TOKEN_STORAGE_VERSION,
    SERVICE_ANNOUNCE,
    WWW_DIR,
)
from .device_store import DeviceStore

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

    # Monkey-patch config_entries.async_remove to block YAML entry deletion
    if not hasattr(hass.config_entries, "_hi_patched"):
        _orig_async_remove = hass.config_entries.async_remove

        async def _patched_async_remove(entry_id: str) -> dict:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == DOMAIN and entry.unique_id == YAML_UNIQUE_ID:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="yaml_entry_delete_blocked",
                    translation_placeholders={"title": entry.title},
                )
            return await _orig_async_remove(entry_id)

        hass.config_entries.async_remove = _patched_async_remove  # type: ignore[method-assign]
        hass.config_entries._hi_patched = True

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
    # Button entry: just forward platforms (no rooms, no services)
    if entry.unique_id == BUTTONS_UNIQUE_ID:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

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
    """Unload a config entry. YAML entries cannot be unloaded."""
    if entry.unique_id == YAML_UNIQUE_ID:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="yaml_entry_delete_blocked",
            translation_placeholders={"title": entry.title},
        )
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


async def _async_load_pwa_token(hass: HomeAssistant) -> str:
    """Load the PWA shared token from .storage; generate + persist on first run.

    Issue #54: the token previously lived only in hass.data, so every HA
    restart or config-entry reload rotated it — already-open PWA pages then
    got 401 from RecordView until manually refreshed. Persisting it keeps
    existing pages working across restarts.
    """
    store = Store(hass, PWA_TOKEN_STORAGE_VERSION, PWA_TOKEN_STORAGE_KEY)
    data = await store.async_load()
    if isinstance(data, dict) and data.get("token"):
        return data["token"]
    token = secrets.token_urlsafe(32)
    await store.async_save({"token": token})
    _LOGGER.info("Generated new PWA token (first run or storage reset)")
    return token


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
    hass.data[DOMAIN]["pwa_token"] = await _async_load_pwa_token(hass)

    # Device registry for ESP32 intercom buttons (issue #40)
    device_store = DeviceStore(hass)
    await device_store.async_load()
    hass.data[DOMAIN]["device_store"] = device_store

    # Ensure a dedicated config entry for button devices (issue #48)
    button_entry_id = await _ensure_button_entry(hass, device_store)
    hass.data[DOMAIN]["button_entry_id"] = button_entry_id

    # Initialize error/state tracking
    hass.data[DOMAIN].setdefault("errors", {})
    hass.data[DOMAIN].setdefault("states", {})

    register_api_views(hass)
    _register_services(hass, all_rooms)
    _register_devices(hass, entry.entry_id, entry_rooms.get(entry.entry_id, {}))
    # Button devices registered under their own entry if it exists
    if button_entry_id:
        _register_button_devices(hass, button_entry_id, device_store)

    # Forward to sensor/number/binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "Home Intercom — %d rooms (%d entries), %d buttons, audio: %s",
        len(all_rooms),
        len(entry_rooms),
        len(device_store.devices),
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


def _register_button_devices(
    hass: HomeAssistant, entry_id: str, device_store: DeviceStore
) -> None:
    """Register each non-revoked intercom button as an HA device.

    Device info (name, area) is owned by the HA device registry.
    Changes sync back to device_store via
    _async_device_registry_updated.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)

    for mac, dev in device_store.devices.items():
        if dev.get("revoked"):
            continue
        device = registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, mac)},
            name=dev.get("name", mac),
            manufacturer="Home Intercom",
            model="ESP32 Intercom Button",
            sw_version=dev.get("firmware_version"),
            suggested_area=dev.get("room") or None,
        )
        # Ensure the device is owned by the button entry (not just a room entry)
        if entry_id not in device.config_entries:
            registry.async_update_device(device.id, add_config_entry_id=entry_id)
        # Remove room-entry associations so button entry becomes primary
        for old_eid in list(device.config_entries):
            if old_eid != entry_id:
                registry.async_update_device(device.id, remove_config_entry_id=old_eid)

    # Listen for HA-side edits (rename, area change) → sync back to device_store
    @callback
    def _on_device_registry_updated(event: Any) -> None:
        _async_device_registry_updated(hass, event, device_store)

    hass.bus.async_listen("device_registry_updated", _on_device_registry_updated)


@callback
def _async_device_registry_updated(
    hass: HomeAssistant, event: Any, device_store: DeviceStore
) -> None:
    """Sync HA device registry edits back to device_store.

    Triggered when a user renames a button device or moves it to a
    different area in the HA UI. We update the device_store JSON so
    the binding survives restarts.
    """
    if event.data.get("action") != "update":
        return

    device_id: str | None = event.data.get("device_id")
    if not device_id:
        return

    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    device_entry = registry.async_get(device_id)
    if device_entry is None:
        return

    # Check if this is one of our button devices
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            mac = identifier
            break
    else:
        return  # not our device

    existing = device_store.devices.get(mac)
    if existing is None:
        return

    changes: dict[str, Any] = event.data.get("changes", {})

    # HA device renamed → update device_store name
    new_name = changes.get("name")
    if new_name and new_name != existing.get("name"):
        _LOGGER.info("Button %s renamed via HA UI: %r → %r", mac, existing.get("name"), new_name)
        hass.async_create_task(
            _async_sync_device_field(hass, device_store, mac, "name", new_name)
        )

    # Device moved to a different area → update device_store room
    new_area_id = changes.get("area_id")
    if new_area_id is not None:
        # area_id → area name for room mapping
        from homeassistant.helpers import area_registry as ar

        area_reg = ar.async_get(hass)
        new_room = ""
        if new_area_id:
            area_entry = area_reg.async_get_area(new_area_id)
            new_room = area_entry.name if area_entry else new_area_id
        if new_room != existing.get("room"):
            _LOGGER.info("Button %s moved to area %r via HA UI", mac, new_room)
            hass.async_create_task(
                _async_sync_device_field(hass, device_store, mac, "room", new_room)
            )


async def _async_sync_device_field(
    hass: HomeAssistant, device_store: DeviceStore, mac: str, field: str, value: str
) -> None:
    """Persist a device_store field change (from HA UI edit)."""
    try:
        await device_store.update_field(mac, field, value)
    except ValueError:
        _LOGGER.warning("Cannot update field %r for device %s", field, mac)


async def _ensure_button_entry(
    hass: HomeAssistant, device_store: DeviceStore
) -> str | None:
    """Create a dedicated config entry for intercom buttons if any exist (issue #48).

    Returns the button entry_id, or None if there are no devices yet.
    The buttons entry appears as a separate card in Settings → Devices & Services.
    """
    # Already exists?
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == BUTTONS_UNIQUE_ID:
            return entry.entry_id

    # No devices yet — don't create an empty entry
    active = [d for d in device_store.devices.values() if not d.get("revoked")]
    if not active:
        return None

    # Create the entry via flow (no user interaction needed)
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "buttons"},
        data={},
    )
    # Look up the newly created entry
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == BUTTONS_UNIQUE_ID:
            return entry.entry_id

    _LOGGER.warning("Button entry flow did not create an entry — button devices won't appear")
    return None
