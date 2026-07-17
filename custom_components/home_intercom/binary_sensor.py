"""Binary sensor platform — per-room connectivity status."""

from __future__ import annotations

import logging
from dataclasses import dataclass

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

    entities: list[ConnectedSensor] = []
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
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_connected"
        self._attr_translation_placeholders = {"room": room_name}

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
