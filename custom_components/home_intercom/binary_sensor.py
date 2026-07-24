"""Binary sensor platform — per-room connectivity + per-device online status."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ROOMS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# A device is "online" if it checked in within this window
_ONLINE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, kw_only=True)
class HomeIntercomBinarySensorDescription(BinarySensorEntityDescription):
    """Description for Home Intercom binary sensor entities."""

    room_key: str = ""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor from config entry."""
    rooms: dict[str, dict] = {}
    rooms.update(entry.data.get(CONF_ROOMS, {}))
    rooms.update(entry.options.get(CONF_ROOMS, {}))

    entities: list[ConnectedSensor | ButtonOnlineSensor] = []
    for room_id, room_cfg in rooms.items():
        entity_id = room_cfg.get("entity_id", "")
        if not entity_id:
            continue
        entities.append(
            ConnectedSensor(
                entry=entry,
                room_key=room_id,
                entity_id=entity_id,
                room_name=room_cfg.get("name", room_id),
            )
        )

    # Per-device online sensors (issue #48: native HA device registry)
    # Only created by the first entry to avoid duplicates
    if not hass.data.get(DOMAIN, {}).get("_button_entities_registered"):
        hass.data[DOMAIN]["_button_entities_registered"] = True
        device_store = hass.data.get(DOMAIN, {}).get("device_store")
        if device_store is not None:
            for mac, dev in device_store.devices.items():
                if dev.get("revoked"):
                    continue
                entities.append(
                    ButtonOnlineSensor(
                        entry=entry,
                        mac=mac,
                        device_name=dev.get("name", mac),
                    )
                )

    async_add_entities(entities)


class ConnectedSensor(BinarySensorEntity):
    """Binary sensor showing whether the media player is online."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(
        self,
        entry: ConfigEntry,
        room_key: str,
        entity_id: str,
        room_name: str,
    ) -> None:
        """Initialize."""
        self._entry = entry
        self._room_key = room_key
        self._entity_id = entity_id
        self.entity_description = BinarySensorEntityDescription(
            key="connected",
            translation_key="connected",
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_connected_v1"
        self.entity_id = f"binary_sensor.{room_key}_connected"

    @property
    def device_info(self):
        """Associate with the room's device."""
        return {"identifiers": {(DOMAIN, self._room_key)}}

    @property
    def is_on(self) -> bool:
        """Return True if media player is available."""
        state = self.hass.states.get(self._entity_id)
        if state is None or state.state == "unavailable":
            return False
        return state.state not in ("unavailable", "unknown")


class ButtonOnlineSensor(BinarySensorEntity):
    """Binary sensor: is this intercom button online (seen within 24h)? (issue #48)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, mac: str, device_name: str) -> None:
        """Initialize."""
        self._entry = entry
        self._mac = mac
        self.entity_description = BinarySensorEntityDescription(
            key="online",
            translation_key="online",
        )
        self._attr_unique_id = f"{entry.entry_id}_{mac}_online_v1"
        self._attr_name = "Online"
        self.entity_id = f"binary_sensor.{_safe_entity_id(mac)}_online"

    @property
    def device_info(self):
        """Associate with the button's HA device."""
        return {"identifiers": {(DOMAIN, self._mac)}}

    @property
    def is_on(self) -> bool:
        """Return True if the button checked in within _ONLINE_WINDOW."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return False
        dev = store.devices.get(self._mac)
        if dev is None or dev.get("revoked"):
            return False
        last_seen_str = dev.get("last_seen", "")
        if not last_seen_str:
            return False
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
        except ValueError:
            return False
        return (datetime.now(UTC) - last_seen) <= _ONLINE_WINDOW


def _safe_entity_id(mac: str) -> str:
    """Convert a MAC address to a safe entity id fragment."""
    return mac.lower().replace(":", "_")
