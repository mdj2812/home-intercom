"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Rooms live on the writable UI config entry (PWA / Options Flow). A leftover
YAML SOURCE_IMPORT entry is imported once into that UI entry and then removed
(issue #75). The buttons entry is separate and has no rooms.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_interval
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
    FIRMWARE_CACHE_SUBDIR,
    FIRMWARE_POLL_INTERVAL_SECS,
    KEY_BUTTON_ENTRY_ID,
    MAC_PATTERN,
    PLATFORMS,
    PWA_TOKEN_STORAGE_KEY,
    PWA_TOKEN_STORAGE_VERSION,
    SERVICE_ANNOUNCE,
    WWW_DIR,
)
from .device_store import DeviceStore
from .firmware import refresh_cached_firmware

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
# YAML → one-time import into the writable UI entry (issue #75)
# ═══════════════════════════════════════════════════════════════════════


def _entry_room_map(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Combined data + options rooms for one config entry (options win)."""
    data_rooms = dict(entry.data.get(CONF_ROOMS, {}) or {})
    options_rooms = dict(entry.options.get(CONF_ROOMS, {}) or {})
    return {**data_rooms, **options_rooms}


def merge_incoming_rooms(
    current: dict[str, dict[str, Any]], incoming: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Add YAML keys that are not already on the UI entry. Existing keys win."""
    merged = {key: dict(room) for key, room in current.items()}
    added: list[str] = []
    for key, room in incoming.items():
        if key in merged or not isinstance(room, dict):
            continue
        merged[key] = dict(room)
        added.append(key)
    return merged, added


def _find_ui_entry(entries: list[ConfigEntry]) -> ConfigEntry | None:
    """Find the writable UI config entry."""
    for entry in entries:
        if entry.unique_id == UI_UNIQUE_ID:
            return entry
    return None


def _move_room_devices(hass: HomeAssistant, from_entry_id: str, to_entry_id: str) -> None:
    """Re-home YAML room devices onto the UI entry so HA does not drop them."""
    from homeassistant.helpers import device_registry as dr

    if from_entry_id == to_entry_id:
        return
    mac_re = re.compile(MAC_PATTERN)
    registry = dr.async_get(hass)
    for device in list(registry.devices.get_devices_for_config_entry_id(from_entry_id)):
        room_ids = [
            ident
            for domain, ident in device.identifiers
            if domain == DOMAIN and not mac_re.fullmatch(ident)
        ]
        if not room_ids:
            continue
        registry.async_update_device(device.id, add_config_entry_id=to_entry_id)
        registry.async_update_device(device.id, remove_config_entry_id=from_entry_id)


async def _import_yaml_rooms(hass: HomeAssistant, config_rooms: dict[str, Any]) -> None:
    """Copy leftover YAML rooms onto the UI entry, then drop the YAML entry."""
    entries = list(hass.config_entries.async_entries(DOMAIN))
    yaml_entry = _find_yaml_entry(entries)
    ui_entry = _find_ui_entry(entries)
    incoming: dict[str, Any] = {}
    if yaml_entry is not None:
        incoming.update(_entry_room_map(yaml_entry))
    incoming.update(config_rooms)
    if not incoming and yaml_entry is None:
        return

    if ui_entry is None:
        # Do not await the flow here: async_setup still holds the setup lock,
        # so creating the entry would deadlock and never call async_setup_entry.
        yaml_entry_id = yaml_entry.entry_id if yaml_entry is not None else None

        async def _finish_import() -> None:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={CONF_ROOMS: incoming},
            )
            ui = _find_ui_entry(list(hass.config_entries.async_entries(DOMAIN)))
            if yaml_entry_id is None or ui is None:
                return
            _move_room_devices(hass, yaml_entry_id, ui.entry_id)
            await hass.config_entries.async_remove(yaml_entry_id)
            _LOGGER.info("Removed YAML config entry after importing rooms into the PWA catalog")

        hass.async_create_task(_finish_import())
        return

    merged, added = merge_incoming_rooms(_entry_room_map(ui_entry), incoming)
    if added:
        options = dict(ui_entry.options)
        options[CONF_ROOMS] = merged
        hass.config_entries.async_update_entry(ui_entry, options=options)
        _LOGGER.info("Imported YAML rooms into the UI entry: %s", ", ".join(added))

    if yaml_entry is not None:
        _move_room_devices(hass, yaml_entry.entry_id, ui_entry.entry_id)
        await hass.config_entries.async_remove(yaml_entry.entry_id)
        _LOGGER.info("Removed YAML config entry after importing rooms into the PWA catalog")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import deprecated YAML rooms into the writable UI entry."""
    yaml_rooms: dict[str, Any] = {}
    if DOMAIN in config:
        yaml_rooms = dict(config[DOMAIN][CONF_ROOMS])
        _LOGGER.warning(
            "home_intercom YAML is deprecated; rooms are imported into the UI entry. "
            "Add and remove speakers in the PWA, then remove the home_intercom: block "
            "from configuration.yaml"
        )
    await _import_yaml_rooms(hass, yaml_rooms)
    return True


# ═══════════════════════════════════════════════════════════════════════
# Config entry setup — per-entry, merged globally
# ═══════════════════════════════════════════════════════════════════════


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up or update a config entry. Merges all entries' rooms for services."""
    # Button entry: just forward platforms (no rooms, no services)
    if entry.unique_id == BUTTONS_UNIQUE_ID:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # Listen for new device registrations → reload to create entities
        _setup_device_store_listener(hass, entry)
        return True

    data_rooms = entry.data.get(CONF_ROOMS, {})
    options_rooms = entry.options.get(CONF_ROOMS, {})
    room_map = {**data_rooms, **options_rooms}

    # Store per-entry rooms
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("entry_rooms", {})
    hass.data[DOMAIN]["entry_rooms"][entry.entry_id] = room_map

    # Drop HA devices for rooms no longer on this entry (YAML #63, UI PWA delete).
    # Button devices are MAC identifiers on a different entry — do not run this there.
    if entry.unique_id != BUTTONS_UNIQUE_ID:
        _reconcile_room_devices(hass, entry.entry_id, set(room_map.keys()))

    # Full setup with merged rooms from all entries
    await _full_setup(hass, entry)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.unique_id == BUTTONS_UNIQUE_ID:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Room entry (UI): unload platforms so reload can re-forward (#64)
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    if DOMAIN in hass.data:
        hass.data[DOMAIN].setdefault("entry_rooms", {}).pop(entry.entry_id, None)
        remaining = hass.data[DOMAIN].get("entry_rooms", {})
        if not remaining:
            unsub = hass.data[DOMAIN].get("firmware_poll_unsub")
            if unsub:
                unsub()
            hass.services.async_remove(DOMAIN, SERVICE_ANNOUNCE)
            hass.data.pop(DOMAIN, None)
        else:
            # Other entries still loaded — refresh merged room list only
            all_rooms: dict[str, dict[str, Any]] = {}
            for rooms in remaining.values():
                all_rooms.update(rooms)
            hass.data[DOMAIN]["rooms"] = all_rooms
            _register_services(hass, all_rooms)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Config entry removed — YAML leftovers are already imported into the UI entry."""
    return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when Options flow saves changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _schedule_firmware_poll(hass: HomeAssistant) -> None:
    """Refresh GitHub firmware cache shortly after setup, then hourly.

    Skips if a poller is already registered (multiple room entries share one).
    GitHub is contacted only while at least one button is registered.
    """
    if hass.data[DOMAIN].get("firmware_poll_unsub") is not None:
        return

    async def _interval(_now=None) -> None:
        data = hass.data.get(DOMAIN, {})
        directory = data.get("firmware_dir")
        store = data.get("device_store")
        if not directory or not store or not store.devices:
            return
        await hass.async_add_executor_job(refresh_cached_firmware, directory)

    unsubs = [
        async_call_later(hass, 0, _interval),
        async_track_time_interval(hass, _interval, timedelta(seconds=FIRMWARE_POLL_INTERVAL_SECS)),
    ]

    def _unsub() -> None:
        for unsub in unsubs:
            unsub()

    hass.data[DOMAIN]["firmware_poll_unsub"] = _unsub
    _LOGGER.info(
        "firmware cache poller every %ss (%s)",
        FIRMWARE_POLL_INTERVAL_SECS,
        hass.data[DOMAIN]["firmware_dir"],
    )


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
    firmware_dir = os.path.join(audio_dir, FIRMWARE_CACHE_SUBDIR)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(
        {
            "rooms": all_rooms,
            "audio_dir": audio_dir,
            "firmware_dir": firmware_dir,
        }
    )

    await hass.async_add_executor_job(lambda: os.makedirs(audio_dir, exist_ok=True))
    await hass.async_add_executor_job(lambda: os.makedirs(firmware_dir, exist_ok=True))
    hass.data[DOMAIN]["pwa_token"] = await _async_load_pwa_token(hass)

    # Device registry for ESP32 intercom buttons (issue #40)
    device_store = DeviceStore(hass)
    await device_store.async_load()
    hass.data[DOMAIN]["device_store"] = device_store

    _schedule_firmware_poll(hass)

    # Ensure a dedicated config entry for button devices (issue #48)
    button_entry_id = await _ensure_button_entry(hass, device_store)
    hass.data[DOMAIN][KEY_BUTTON_ENTRY_ID] = button_entry_id

    # Initialize error/state tracking
    hass.data[DOMAIN].setdefault("errors", {})
    hass.data[DOMAIN].setdefault("states", {})

    register_api_views(hass)
    _register_services(hass, all_rooms)
    _register_devices(hass, entry.entry_id, entry_rooms.get(entry.entry_id, {}))
    # Button devices registered under their own entry if it exists
    if button_entry_id:
        _register_button_devices(hass, button_entry_id, device_store)

    # First hello has no buttons-entry listener yet — keep HA's device
    # registry in sync from this YAML/UI entry instead (PWA delete, #51).
    _setup_button_registry_sync(hass, entry)

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
    """Allow device deletion for room and button devices.

    Deleting a button device removes it from device_store.json
    (unlike revoke which keeps the record but blocks hello).
    """
    _handle_button_device_delete(hass, device_entry)

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


def _media_player_manufacturer(
    hass: HomeAssistant,
    entity_id: str,
    entity_registry: Any,
    device_registry: Any,
) -> str:
    """Get the manufacturer of the underlying media_player device.

    Follows entity_id → entity entry → device entry → manufacturer.
    Falls back to "Home Intercom" if the chain is broken.
    """
    entry = entity_registry.async_get(entity_id)
    if entry is not None and entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device is not None and device.manufacturer:
            return device.manufacturer
    return "Home Intercom"


def _handle_button_device_delete(hass: HomeAssistant, device_entry: Any) -> None:
    """Delete a button device from device_store.json when removed from HA.

    Unlike revoke (which keeps the record but blocks hello), delete
    removes it entirely.  The device can only come back by sending
    a new /devices/hello.
    """
    import re

    for domain, ident in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if not re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", ident.upper()):
            continue
        store: DeviceStore | None = hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return
        _LOGGER.info("Button %s deleted from HA — removing from device_store", ident)
        hass.async_create_task(store.remove(ident))
        return


def _setup_button_registry_sync(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create/update HA button devices when the store changes.

    The dedicated buttons config entry is only created once a device
    exists, so the first ``/devices/hello`` has no buttons-entry listener.
    Subscribe the YAML/UI entry so Settings → Devices gets the button
    without a restart, and PWA delete can remove that HA device.
    """
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async def _on_store_changed() -> None:
        store = hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return
        button_entry_id = await _ensure_button_entry(hass, store)
        hass.data[DOMAIN][KEY_BUTTON_ENTRY_ID] = button_entry_id
        if button_entry_id:
            _register_button_devices(hass, button_entry_id, store)

    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{DOMAIN}_device_store_changed", _on_store_changed)
    )


def _setup_device_store_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Listen for device_store_changed signal → debounced reload of button entry.

    Multiple hello calls in quick succession trigger only one reload
    after a 2-second quiet period, avoiding race conditions.
    """
    import asyncio

    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    _timer: asyncio.TimerHandle | None = None

    async def _reload_now() -> None:
        _LOGGER.info("Device store changed — reloading button entry %s", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)

    async def _on_store_changed() -> None:
        nonlocal _timer
        if _timer is not None:
            _timer.cancel()
        _timer = hass.loop.call_later(2.0, lambda: hass.async_create_task(_reload_now()))

    # Auto-unsubscribe on entry unload to prevent stacking
    entry.async_on_unload(
        async_dispatcher_connect(hass, f"{DOMAIN}_device_store_changed", _on_store_changed)
    )


def _register_devices(hass: HomeAssistant, entry_id: str, room_map: dict[str, Any]) -> None:
    """Register devices for ONE config entry. Import is lazy."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)

    for room_id, room in room_map.items():
        entity_id = room.get(CONF_ENTITY_ID, "")
        name = room.get(CONF_NAME, room_id)
        if not entity_id:
            continue

        # Copy manufacturer from the underlying media_player device
        manufacturer = _media_player_manufacturer(hass, entity_id, entity_registry, registry)

        device = registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, room_id)},
            name=name,
            manufacturer=manufacturer,
            model=_friendly_name(hass, entity_id),
        )
        if area_registry.async_get_area(room_id) and device.area_id != room_id:
            registry.async_update_device(device.id, area_id=room_id)


def _reconcile_room_devices(
    hass: HomeAssistant,
    entry_id: str,
    current_rooms: set[str],
) -> None:
    """Remove HA devices owned by this entry whose room is gone.

    ``_register_devices`` only calls ``async_get_or_create``. Without this,
    deleting a room (YAML edit or PWA) leaves an empty card in
    Settings → Devices. Called on every setup/reload so cold restarts
    also drop stale entries.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    for device in list(registry.devices.get_devices_for_config_entry_id(entry_id)):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier not in current_rooms:
                _LOGGER.info(
                    "Removing orphaned device '%s' (room '%s')",
                    device.name or device.id,
                    identifier,
                )
                registry.async_remove_device(device.id)
                break


_reconcile_yaml_devices = _reconcile_room_devices


def _find_yaml_entry(entries: list[ConfigEntry]) -> ConfigEntry | None:
    """Find the YAML (SOURCE_IMPORT) entry among existing entries."""
    for entry in entries:
        if entry.unique_id == YAML_UNIQUE_ID:
            return entry
    return None


def _register_button_devices(hass: HomeAssistant, entry_id: str, device_store: DeviceStore) -> None:
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
            manufacturer="Espressif",
            model="ESP32 Intercom Button",
            serial_number=mac,
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
        # Update manufacturer and serial if they changed (existing devices from 2.0.2)
        updates: dict[str, Any] = {}
        if not device.name:
            updates["name"] = dev.get("name", mac)
        if device.manufacturer != "Espressif":
            updates["manufacturer"] = "Espressif"
        if not device.model:
            updates["model"] = "ESP32 Intercom Button"
        if device.serial_number != mac:
            updates["serial_number"] = mac
        if updates:
            registry.async_update_device(device.id, **updates)

    _reconcile_button_devices(hass, entry_id, device_store)

    # Listen once for HA-side edits (rename, area change) → sync back to store.
    # Look up the live store so reloads do not stack stale closures.
    if not hass.data.get(DOMAIN, {}).get("_button_devreg_unsub"):

        @callback
        def _on_device_registry_updated(event: Any) -> None:
            store = hass.data.get(DOMAIN, {}).get("device_store")
            if store is not None:
                _async_device_registry_updated(hass, event, store)

        hass.data[DOMAIN]["_button_devreg_unsub"] = hass.bus.async_listen(
            "device_registry_updated", _on_device_registry_updated
        )


def _reconcile_button_devices(
    hass: HomeAssistant, entry_id: str, device_store: DeviceStore
) -> None:
    """Remove HA devices for buttons that are gone from the store.

    ``_register_button_devices`` only creates; without this, a PWA/API
    delete leaves an empty card in Settings → Devices.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    known = set(device_store.devices)
    mac_re = re.compile(MAC_PATTERN)

    for device in list(registry.devices.get_devices_for_config_entry_id(entry_id)):
        for domain, ident in device.identifiers:
            if domain != DOMAIN:
                continue
            mac = ident.upper()
            if not mac_re.match(mac):
                continue
            if mac not in known:
                _LOGGER.info("Removing HA device for deleted button %s", mac)
                registry.async_remove_device(device.id)
            break


@callback
def _async_device_registry_updated(
    hass: HomeAssistant, event: Any, device_store: DeviceStore
) -> None:
    """Sync HA device registry edits back to device_store.

    Triggered when a user renames a button device or moves it to a
    different area in the HA UI. (Deletion is handled separately in
    async_remove_config_entry_device.)
    """
    action = event.data.get("action")
    device_id: str | None = event.data.get("device_id")
    if not device_id:
        return

    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    device_entry = registry.async_get(device_id)
    if device_entry is None:
        return

    # Check if this is one of our button devices
    mac: str | None = None
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            mac = identifier
            break
    if mac is None:
        return  # not our device

    existing = device_store.devices.get(mac)
    if existing is None:
        return

    if action == "update":
        changes: dict[str, Any] = event.data.get("changes", {})
        _async_handle_device_update(hass, device_store, mac, existing, changes)


async def _async_sync_device_field(
    hass: HomeAssistant, device_store: DeviceStore, mac: str, field: str, value: str
) -> None:
    """Persist a device_store field change (from HA UI edit)."""
    try:
        await device_store.update_field(mac, field, value)
    except ValueError:
        _LOGGER.warning("Cannot update field %r for device %s", field, mac)


def _async_handle_device_update(
    hass: HomeAssistant,
    device_store: DeviceStore,
    mac: str,
    existing: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    """Handle device_registry_updated with action=update for a button device."""
    # HA device renamed → update device_store name
    new_name = changes.get("name")
    if new_name and new_name != existing.get("name"):
        _LOGGER.info("Button %s renamed via HA UI: %r → %r", mac, existing.get("name"), new_name)
        hass.async_create_task(_async_sync_device_field(hass, device_store, mac, "name", new_name))

    # Device moved to a different area → update device_store room
    new_area_id = changes.get("area_id")
    if new_area_id is not None:
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


async def _ensure_button_entry(hass: HomeAssistant, device_store: DeviceStore) -> str | None:
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
