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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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

    # Per-device sensors (issue #48: native HA device registry)
    # Only created by the first entry to avoid duplicates
    if not hass.data.get(DOMAIN, {}).get("_button_entities_registered"):
        hass.data[DOMAIN]["_button_entities_registered"] = True
        device_store = hass.data.get(DOMAIN, {}).get("device_store")
        if device_store is not None:
            for mac, dev in device_store.devices.items():
                if dev.get("revoked"):
                    continue
                entities.append(
                    ButtonLastSeenSensor(entry=entry, mac=mac, device_name=dev.get("name", mac))
                )
                entities.append(
                    ButtonFirmwareSensor(entry=entry, mac=mac, device_name=dev.get("name", mac))
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
    _attr_name = "Error"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, entry: ConfigEntry, room_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self.entity_description = SensorEntityDescription(
            key="error",
            translation_key="error",
            device_class=SensorDeviceClass.ENUM,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_error_v2"
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
    _attr_name = "State"
    _attr_icon = "mdi:information-outline"

    def __init__(self, entry: ConfigEntry, room_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self.entity_description = SensorEntityDescription(
            key="state",
            translation_key="state",
            device_class=SensorDeviceClass.ENUM,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_state_v2"
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
    _attr_name = "Volume"
    _attr_icon = "mdi:volume-high"
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
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_volume_v2"
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
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_media_v2"
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

    _attr_has_entity_name = True
    _attr_name = "Now Playing"
    _attr_icon = "mdi:cast-audio"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PLAYER_TYPE_OPTIONS
    _attr_should_poll = True

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
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_player_type_v5"
        self.entity_id = f"sensor.{room_key}_player_type"

    async def async_added_to_hass(self) -> None:
        """Set static player type once, retry until player is available."""
        await super().async_added_to_hass()

    async def async_update(self) -> None:
        """Poll until player type is available."""
        # native_value is read by async_write_ha_state via the property
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @property
    def native_value(self) -> str | None:
        """Read player type once, then return cached value."""
        if self._attr_native_value is not None:
            return self._attr_native_value
        state = self.hass.states.get(self._source_entity)
        if state is not None:
            self._attr_native_value = _get_player_type(dict(state.attributes))
            return self._attr_native_value
        return None


class ConfigSensor(SensorEntity):
    """Diagnostic sensor displaying a configured value from the entry."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, room_key: str, config_key: str, room_name: str) -> None:
        self._entry = entry
        self._room_key = room_key
        self._config_key = config_key
        self.entity_description = SensorEntityDescription(
            key=f"config_{config_key}",
            translation_key=f"config_{config_key}",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{room_key}_config_{config_key}_v3"
        self._attr_name = (
            "Announce Volume" if config_key == CONF_ANNOUNCE_VOLUME else "Pause Buffer"
        )
        self._attr_icon = "mdi:tune" if config_key == CONF_ANNOUNCE_VOLUME else "mdi:timer-sand"
        self.entity_id = f"sensor.{room_key}_{config_key}"

    async def async_added_to_hass(self) -> None:
        """Register for config update signals."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"{DOMAIN}_config_update", self._on_config_update)
        )

    @callback
    def _on_config_update(self) -> None:
        """Handle config update signal."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        return _device_info(self._room_key)

    @property
    def native_value(self) -> float:
        """Read current value from in-memory config."""
        hass_data = self.hass.data.get(DOMAIN, {})
        rooms = hass_data.get("rooms", {})
        cfg = rooms.get(self._room_key, {})
        return cfg.get(self._config_key, 0)


# ——— Per-device sensor entities (issue #48: native HA device registry) ———


class ButtonLastSeenSensor(SensorEntity):
    """Sensor: last-seen timestamp for an intercom button (issue #48)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True
    _attr_icon = "mdi:clock-outline"

    def __init__(self, entry: ConfigEntry, mac: str, device_name: str) -> None:
        """Initialize."""
        self._entry = entry
        self._mac = mac
        self.entity_description = SensorEntityDescription(
            key="last_seen",
            translation_key="last_seen",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{mac}_last_seen_v1"
        self._attr_name = "Last seen"
        self.entity_id = f"sensor.{_safe_entity_id(mac)}_last_seen"

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._mac)}}

    @property
    def native_value(self) -> str | None:
        """Return the last_seen ISO timestamp."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return None
        dev = store.devices.get(self._mac)
        if dev is None:
            return None
        return dev.get("last_seen") or None


class ButtonFirmwareSensor(SensorEntity):
    """Sensor: firmware version of an intercom button (issue #48)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, mac: str, device_name: str) -> None:
        """Initialize."""
        self._entry = entry
        self._mac = mac
        self.entity_description = SensorEntityDescription(
            key="firmware_version",
            translation_key="firmware_version",
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_unique_id = f"{entry.entry_id}_{mac}_firmware_v1"
        self._attr_name = "Firmware"
        self._attr_icon = "mdi:chip"
        self.entity_id = f"sensor.{_safe_entity_id(mac)}_firmware"

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._mac)}}

    @property
    def native_value(self) -> str | None:
        """Return the firmware version string."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return None
        dev = store.devices.get(self._mac)
        if dev is None:
            return None
        return dev.get("firmware_version") or None


def _safe_entity_id(mac: str) -> str:
    """Convert a MAC address to a safe entity id fragment."""
    return mac.lower().replace(":", "_")
