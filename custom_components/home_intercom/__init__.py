"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration: YAML only (home_intercom: { rooms: { key: { name, entity_id } } })
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import voluptuous as vol

from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .announce import handle_announce_service
from .api import register_api_views
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_DIR = Path(__file__).parent

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ENTITY_ID): cv.string,
        vol.Optional("announce_volume"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("rooms"): vol.Schema({cv.string: ROOM_SCHEMA}),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-based setup for Home Intercom."""
    room_map = await _load_room_config(hass, config)
    await _setup(hass, room_map)
    return True


async def _load_room_config(
    hass: HomeAssistant,
    config: ConfigType | None = None,
) -> dict:
    """Load room configuration.

    Priority: YAML config > rooms.json fallback.
    """
    if config and DOMAIN in config and "rooms" in config[DOMAIN]:
        return dict(config[DOMAIN]["rooms"])

    # Fallback: rooms.json (legacy container mode)
    rooms_path = _INTEGRATION_DIR / "rooms.json"

    def _read_json() -> dict:
        try:
            with open(rooms_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    result = await hass.async_add_executor_job(_read_json)
    if not result:
        _LOGGER.warning("No room config found in YAML or rooms.json")
    return result


async def _setup(hass: HomeAssistant, room_map: dict) -> None:
    """Shared setup: register views, services, audio dir."""
    audio_dir = hass.config.path("www", "home_intercom_audio")
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update({
        "rooms": room_map,
        "audio_dir": audio_dir,
    })

    os.makedirs(audio_dir, exist_ok=True)

    # Register HTTP API views (PWA frontend + REST API)
    register_api_views(hass)

    # Register announce service
    _register_services(hass)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


def _register_services(hass: HomeAssistant) -> None:
    """Register home_intercom.announce service."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, "announce", _handle_announce)
