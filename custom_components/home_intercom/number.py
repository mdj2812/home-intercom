"""Number platform — per-room config entities (announce_volume, pause_buffer).

Creates CONFIG entities that appear on the device page.
On value change → update config entry options → reload integration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ANNOUNCE_VOLUME,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
    YAML_UNIQUE_ID,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HomeIntercomNumberDescription(NumberEntityDescription):
    """Description for Home Intercom number entities."""

    config_key: str = ""
    room_key: str = ""
    room_name: str = ""


ROOM_NUMBER_DESCRIPTIONS: tuple[HomeIntercomNumberDescription, ...] = (
    HomeIntercomNumberDescription(
        key=CONF_ANNOUNCE_VOLUME,
        config_key=CONF_ANNOUNCE_VOLUME,
        translation_key=CONF_ANNOUNCE_VOLUME,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    HomeIntercomNumberDescription(
        key=CONF_PAUSE_BUFFER,
        config_key=CONF_PAUSE_BUFFER,
        translation_key=CONF_PAUSE_BUFFER,
        native_min_value=0,
        native_max_value=10,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    # YAML rooms are read-only — no interactive config entities
    if entry.unique_id == YAML_UNIQUE_ID:
        return

    rooms: dict[str, dict] = {}
    rooms.update(entry.data.get(CONF_ROOMS, {}))
    rooms.update(entry.options.get(CONF_ROOMS, {}))

    entities: list[HomeIntercomNumber] = []
    for room_id, room_cfg in rooms.items():
        room_name = room_cfg.get("name", room_id)
        for desc in ROOM_NUMBER_DESCRIPTIONS:
            entities.append(
                HomeIntercomNumber(
                    entry=entry,
                    description=desc,
                    room_key=room_id,
                    room_name=room_name,
                    current_value=room_cfg.get(desc.config_key, 0),
                )
            )

    async_add_entities(entities)


class HomeIntercomNumber(NumberEntity):
    """Number entity for a per-room config value."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        description: HomeIntercomNumberDescription,
        room_key: str,
        room_name: str,
        current_value: int | float,
    ) -> None:
        """Initialize the number entity."""
        self._entry = entry
        self.entity_description = description
        self._room_key = room_key
        self._room_name = room_name
        self._attr_native_value = current_value if current_value is not None else 0
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_{description.key}"
        self._attr_translation_placeholders = {"room": room_name}

    @property
    def device_info(self):
        """Associate with the room's device."""

        return {"identifiers": {(DOMAIN, self._room_key)}}

    async def async_set_native_value(self, value: float) -> None:
        """Set the value, persist to config, update in-memory state."""
        self._attr_native_value = value
        self.async_write_ha_state()

        config_key = self.entity_description.config_key
        # Persist to config entry options (bypass update_listener to avoid reload)
        options = dict(self._entry.options)
        rooms_data: dict = options.setdefault(CONF_ROOMS, {})
        room_config = rooms_data.setdefault(self._room_key, {})
        if value == 0:
            room_config.pop(config_key, None)
        else:
            room_config[config_key] = value
        self.hass.config_entries.async_update_entry(
            self._entry, options={**options, CONF_ROOMS: rooms_data}
        )

        # Update in-memory state without full reload
        hass_data = self.hass.data.get(DOMAIN, {})
        all_rooms = hass_data.get("rooms", {})
        if self._room_key in all_rooms:
            if value == 0:
                all_rooms[self._room_key].pop(config_key, None)
            else:
                all_rooms[self._room_key][config_key] = value

        # Force config sensors to re-read
        _LOGGER.info("Dispatching config_update for room=%s key=%s value=%s", self._room_key, config_key, value)
        async_dispatcher_send(self.hass, f"{DOMAIN}_config_update")
