"""Sensor platform — per-room diagnostic sensors (error, state, volume, media, player_type, config)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ANNOUNCE_VOLUME,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Error codes
ERROR_OK = "ok"
ERROR_UNREACHABLE = "unreachable"
ERROR_TTS_FAILED = "tts_failed"
ERROR_PLAYBACK_FAILED = "playback_failed"
ERROR_UNKNOWN = "unknown"

ERROR_OPTIONS = [
    ERROR_OK,
    ERROR_UNREACHABLE,
    ERROR_TTS_FAILED,
    ERROR_PLAYBACK_FAILED,
    ERROR_UNKNOWN,
]

# States
STATE_IDLE = "idle"
STATE_ANNOUNCING = "announcing"
STATE_PLAYING = "playing"

STATE_OPTIONS = [STATE_IDLE, STATE_ANNOUNCING, STATE_PLAYING]

# Player types
PLAYER_TYPE_MA = "music_assistant"
PLAYER_TYPE_STANDARD = "standard"
PLAYER_TYPE_BASIC = "basic"

PLAYER_TYPE_OPTIONS = [PLAYER_TYPE_MA, PLAYER_TYPE_STANDARD, PLAYER_TYPE_BASIC]

# Bit flags
SUPPORT_REPEAT_SET = 1 << 18


def _get_player_type(attrs: dict) -> str:
    """Determine player type from entity attributes."""
    if attrs.get("app_id") == "music_assistant":
        return PLAYER_TYPE_MA
    if attrs.get("supported_features", 0) & SUPPORT_REPEAT_SET:
        return PLAYER_TYPE_STANDARD
    return PLAYER_TYPE_BASIC


@dataclass(frozen=True, kw_only=True)
class HomeIntercomSensorDescription(SensorEntityDescription):
    """Description for Home Intercom sensor entities."""

    room_key: str = ""
    source_entity: str = ""
    poll_sensor: bool = True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from config entry."""
    rooms: dict[str, dict] = {}
    rooms.update(entry.data.get(CONF_ROOMS, {}))
    rooms.update(entry.options.get(CONF_ROOMS, {}))

    entities: list[SensorEntity] = []
    for room_id, room_cfg in rooms.items():
        entity_id = room_cfg.get("entity_id", "")
        room_name = room_cfg.get("name", room_id)
        if not entity_id:
            continue

        # Error sensor
        entities.append(ErrorSensor(entry=entry, room_key=room_id, room_name=room_name))
        # State sensor
        entities.append(StateSensor(entry=entry, room_key=room_id, room_name=room_name))
        # Volume sensor
        entities.append(
            VolumeSensor(
                entry=entry, room_key=room_id, source_entity=entity_id, room_name=room_name
            )
        )
        # Media info sensor
        entities.append(
            MediaSensor(entry=entry, room_key=room_id, source_entity=entity_id, room_name=room_name)
        )
        # Player type diagnostic
        entities.append(
            PlayerTypeSensor(
                entry=entry, room_key=room_id, source_entity=entity_id, room_name=room_name
            )
        )
        # Config value sensors (read-only display for all rooms)
        entities.append(
            ConfigSensor(
                entry=entry,
                room_key=room_id,
                config_key=CONF_ANNOUNCE_VOLUME,
                room_name=room_name,
            )
        )
        entities.append(
            ConfigSensor(
                entry=entry,
                room_key=room_id,
                config_key=CONF_PAUSE_BUFFER,
                room_name=room_name,
            )
        )

    async_add_entities(entities)


def _device_info(room_key: str) -> dict:
    return {"identifiers": {(DOMAIN, room_key)}}


class ErrorSensor(SensorEntity):
    """Sensor showing the last announcement error code."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ERROR_OPTIONS

    def __init__(self, entry: ConfigEntry, room_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self.entity_description = SensorEntityDescription(
            key="error",
            translation_key="error",
            device_class=SensorDeviceClass.ENUM,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_error"
        self._attr_native_value = ERROR_OK
        self.entity_id = f"sensor.{room_key}_error"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @callback
    def set_error(self, code: str) -> None:
        """Set the error code."""
        self._attr_native_value = code
        self.async_write_ha_state()

    @callback
    def clear_error(self) -> None:
        """Reset to ok."""
        self._attr_native_value = ERROR_OK
        self.async_write_ha_state()


class StateSensor(SensorEntity):
    """Sensor showing current state (idle/announcing/playing)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATE_OPTIONS

    def __init__(self, entry: ConfigEntry, room_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self.entity_description = SensorEntityDescription(
            key="state",
            translation_key="state",
            device_class=SensorDeviceClass.ENUM,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_state"
        self._attr_native_value = STATE_IDLE
        self.entity_id = f"sensor.{room_key}_state"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @callback
    def set_state(self, state: str) -> None:
        """Set the current state."""
        self._attr_native_value = state
        self.async_write_ha_state()


class VolumeSensor(SensorEntity):
    """Sensor polling the media_player's current volume level."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = True

    def __init__(
        self, entry: ConfigEntry, room_key: str, source_entity: str, room_name: str
    ) -> None:
        self._entry = entry
        self._room_key = room_key
        self._source_entity = source_entity
        self.entity_description = SensorEntityDescription(
            key="volume",
            translation_key="volume",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_volume"
        self.entity_id = f"sensor.{room_key}_volume"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @property
    def native_value(self) -> float | None:
        """Return the media player's current volume (0-100)."""
        state = self.hass.states.get(self._source_entity)
        if state is None or state.state == STATE_UNAVAILABLE:
            return None
        vol = state.attributes.get("volume_level")
        if vol is not None:
            return round(vol * 100, 1)
        return None


class MediaSensor(SensorEntity):
    """Sensor showing now playing media title/artist."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self, entry: ConfigEntry, room_key: str, source_entity: str, room_name: str
    ) -> None:
        self._entry = entry
        self._room_key = room_key
        self._source_entity = source_entity
        self.entity_description = SensorEntityDescription(
            key="media",
            translation_key="media",
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_media"
        self.entity_id = f"sensor.{room_key}_media"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @property
    def native_value(self) -> str | None:
        """Return the current media title/artist."""
        state = self.hass.states.get(self._source_entity)
        if state is None or state.state == STATE_UNAVAILABLE:
            return None
        title = state.attributes.get("media_title", "")
        artist = state.attributes.get("media_artist", "")
        if title or artist:
            return f"{title} - {artist}" if title and artist else (title or artist)
        return state.attributes.get("app_name") or None


class PlayerTypeSensor(SensorEntity):
    """Diagnostic sensor showing the player type (music_assistant/standard/basic)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PLAYER_TYPE_OPTIONS

    def __init__(
        self, entry: ConfigEntry, room_key: str, source_entity: str, room_name: str
    ) -> None:
        self._entry = entry
        self._room_key = room_key
        self._source_entity = source_entity
        self.entity_description = SensorEntityDescription(
            key="player_type",
            translation_key="player_type",
            device_class=SensorDeviceClass.ENUM,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_player_type_v2"
        self.entity_id = f"sensor.{room_key}_player_type"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @property
    def native_value(self) -> str | None:
        """Determine player type from media_player attributes."""
        state = self.hass.states.get(self._source_entity)
        if state is None:
            return None
        return _get_player_type(dict(state.attributes))


class ConfigSensor(SensorEntity):
    """Diagnostic sensor displaying a configured value from the entry."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, room_key: str, config_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self._config_key = config_key
        # Read configured value from entry data + options
        rooms = {**entry.data.get(CONF_ROOMS, {}), **entry.options.get(CONF_ROOMS, {})}
        cfg = rooms.get(room_key, {})
        value = cfg.get(config_key, 0)
        self.entity_description = SensorEntityDescription(
            key=f"config_{config_key}",
            translation_key=f"config_{config_key}",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_config_{config_key}"
        self._attr_native_value = value if value is not None else 0
        # Consistent entity_id across locales
        self.entity_id = f"sensor.{room_key}_{config_key}"

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)
