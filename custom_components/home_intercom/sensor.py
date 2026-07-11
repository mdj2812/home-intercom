"""Sensor entities for Home Intercom.

Provides sensor.home_intercom_status — shows current intercom state.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key="status",
        name="Status",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:intercom",
        has_entity_name=True,
    ),
    SensorEntityDescription(
        key="rooms_configured",
        name="Rooms Configured",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:speaker-multiple",
        has_entity_name=True,
    ),
)


class HomeIntercomSensor(SensorEntity):
    """Base sensor for Home Intercom."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Home Intercom",
            "manufacturer": "Home Lab",
            "model": "PWA Intercom System",
        }
        self._entry_id = entry_id

    @property
    def available(self) -> bool:
        """Always available."""
        return True


class IntercomStatusSensor(HomeIntercomSensor):
    """Status sensor — ready, recording, error."""

    def __init__(self, entry_id: str) -> None:
        """Initialize."""
        super().__init__(entry_id, SENSOR_DESCRIPTIONS[0])
        self._attr_native_value = "ready"

    def update_status(self, status: str) -> None:
        """Update status string."""
        self._attr_native_value = status
        self.async_write_ha_state()


class RoomsConfiguredSensor(HomeIntercomSensor):
    """Shows number of configured rooms."""

    def __init__(self, entry_id: str, room_count: int) -> None:
        """Initialize."""
        super().__init__(entry_id, SENSOR_DESCRIPTIONS[1])
        self._attr_native_value = room_count


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Home Intercom sensors."""
    data = hass.data.get(DOMAIN, {})
    room_map = data.get("rooms", {})

    entities = [
        IntercomStatusSensor(entry.entry_id),
        RoomsConfiguredSensor(entry.entry_id, len(room_map)),
    ]
    async_add_entities(entities)
