"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration:
  - Config flow (preferred): HA UI → Settings → Devices & Services → Add
  - YAML (legacy): home_intercom: { rooms: { key: { name, entity } } }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .announce import handle_announce_service
from .api import register_api_views
from .const import DOMAIN
from .player import play_announcement

_LOGGER = logging.getLogger(__name__)

# Path to src directory (where intercom.html and static/ live)
_SRC_DIR = Path(__file__).parent.parent.parent / "src"

# YAML config schema (legacy fallback)
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


def _load_room_config(config: ConfigType | None = None, entry: ConfigEntry | None = None) -> dict:
    """Load room configuration.

    Priority: config entry (UI) > YAML config > rooms.json fallback.
    """
    # Config entry (from config flow)
    if entry and entry.data.get("rooms"):
        return dict(entry.data["rooms"])

    # YAML config
    if config and DOMAIN in config and "rooms" in config[DOMAIN]:
        return dict(config[DOMAIN]["rooms"])

    # Fallback: rooms.json (legacy container mode)
    rooms_path = _SRC_DIR / "rooms.json"
    try:
        with open(rooms_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _LOGGER.warning("No room config found")
        return {}


async def _async_setup_integration(
    hass: HomeAssistant,
    room_map: dict,
) -> None:
    """Shared setup logic for both YAML and config entry paths."""
    audio_dir = hass.config.path("www", "home_intercom_audio")
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update({
        "rooms": room_map,
        "audio_dir": audio_dir,
    })

    os.makedirs(audio_dir, exist_ok=True)

    # Register HTTP API views
    register_api_views(hass)

    # Register static file paths for PWA assets
    static_dir = str(_SRC_DIR / "static")
    await hass.http.async_register_static_paths(
        "/home_intercom/static",
        static_dir,
        cache_headers=True,
    )

    # Register the PWA frontend as a sidebar panel
    hass.components.frontend.async_register_built_in_panel(
        component_name="custom",
        sidebar_title="Home Intercom",
        sidebar_icon="mdi:intercom",
        frontend_url_path="home_intercom",
        config={
            "_panel_custom": {
                "name": "home-intercom-panel",
                "module_url": "/home_intercom/panel",
            }
        },
        require_admin=False,
    )

    # Register announce service
    _register_services(hass, room_map)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


def _register_services(hass: HomeAssistant, room_map: dict) -> None:
    """Register home_intercom services."""

    async def _handle_announce(call: ServiceCall):
        """Handle home_intercom.announce service call."""
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, "announce", _handle_announce)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """YAML-based setup (legacy fallback)."""
    room_map = _load_room_config(config=config)
    await _async_setup_integration(hass, room_map)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from config entry (config flow)."""
    room_map = _load_room_config(entry=entry)
    await _async_setup_integration(hass, room_map)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, "announce")
    hass.data.pop(DOMAIN, None)
    return True
