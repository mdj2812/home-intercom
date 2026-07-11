"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Architecture:
  PWA (push-to-talk) → HomeAssistantView (/api/home_intercom/record)
  → player.py (direct hass service calls, no HA_TOKEN needed)
  → any media_player entity (Music Assistant / Xiaomi / HomePod / Chromecast)

Configuration (YAML, until #17 config flow):
  home_intercom:
    rooms:
      living:
        name: Living Room
        entity: media_player.living_room_speaker
"""

import json
import logging
import os
from pathlib import Path

import voluptuous as vol

from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import register_api_views
from .const import DOMAIN
from .player import play_announcement

_LOGGER = logging.getLogger(__name__)

# Path to src directory (where intercom.html and static/ live)
_SRC_DIR = Path(__file__).parent.parent.parent / "src"

# YAML config schema (temporary — replaced by config flow in #17)
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
                vol.Required("rooms"): vol.Schema(
                    {cv.string: ROOM_SCHEMA}
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _load_room_config(config: ConfigType) -> dict:
    """Load room configuration from YAML config or fallback to rooms.json."""
    if DOMAIN in config and "rooms" in config[DOMAIN]:
        return dict(config[DOMAIN]["rooms"])

    # Fallback: try rooms.json (legacy container mode)
    rooms_path = _SRC_DIR / "rooms.json"
    try:
        with open(rooms_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _LOGGER.warning("No room config found in YAML or rooms.json")
        return {}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Home Intercom from configuration.yaml.

    Will be replaced by config flow in #17.
    """
    room_map = _load_room_config(config)

    audio_dir = hass.config.path("www", "home_intercom_audio")
    hass.data[DOMAIN] = {
        "rooms": room_map,
        "audio_dir": audio_dir,
    }

    # Ensure audio directory exists
    os.makedirs(audio_dir, exist_ok=True)

    # Register HTTP API views (HomeAssistantView)
    register_api_views(hass)

    # Register static file paths for PWA assets (CSS, JS, icons)
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
        config={"_panel_custom": {"name": "home-intercom-panel", "module_url": "/home_intercom/panel"}},
        require_admin=False,
    )

    # Register the announce service (basic — full impl in #18)
    async def _handle_announce(call: ServiceCall):
        """Handle home_intercom.announce service call."""
        target = call.data.get("target", "all")
        message = call.data.get("message", "")
        volume = call.data.get("volume")
        url = call.data.get("url")

        if target == "all":
            targets = [(k, v) for k, v in room_map.items() if v.get("entity")]
        else:
            room = room_map.get(target)
            if not room:
                _LOGGER.warning("Unknown target: %s", target)
                return
            targets = [(target, room)]

        if not url and not message:
            _LOGGER.warning("Announce called without message or url")
            return

        # For TTS messages: generate audio via HA TTS
        if message and not url:
            # TODO: TTS integration in #18
            _LOGGER.info("TTS announce not yet implemented: %s", message)
            return

        if url:
            for _key, room in targets:
                await play_announcement(
                    hass,
                    room["entity"],
                    url,
                    0,  # duration unknown for external URLs
                    announce_volume=volume,
                )

    hass.services.async_register(DOMAIN, "announce", _handle_announce)

    _LOGGER.info(
        "Home Intercom set up — %d rooms, audio: %s",
        len(room_map),
        audio_dir,
    )
    return True
