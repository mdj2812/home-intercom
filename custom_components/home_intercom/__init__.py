"""Home Intercom — PWA-based family broadcast system for Home Assistant.

Registers HTTP views (HomeAssistantView) for the PWA frontend and API,
serves static assets, and provides audio recording + playback to any
media_player entity.

Configuration: YAML only (home_intercom: { rooms: { key: { name, entity_id } } })

Sidebar: add a Dashboard with Webpage card pointing to /home_intercom.
Alternatively, if panel_iframe is available in your HA version:
  panel_iframe:
    intercom:
      title: "Home Intercom"
      icon: "mdi:bullhorn-outline"
      url: "/home_intercom"
"""

from __future__ import annotations

import logging
import os
import secrets

import voluptuous as vol
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .announce import handle_announce_service
from .api import register_api_views
from .const import AUDIO_SUBDIR, DOMAIN, WWW_DIR

_LOGGER = logging.getLogger(__name__)

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ENTITY_ID): cv.string,
        vol.Optional("announce_volume"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("pause_buffer"): vol.All(vol.Coerce(float), vol.Range(min=0)),
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
    if DOMAIN not in config or "rooms" not in config[DOMAIN]:
        _LOGGER.error("No home_intercom config found in configuration.yaml")
        return False

    room_map = dict(config[DOMAIN]["rooms"])
    await _setup(hass, room_map)
    return True


async def _setup(hass: HomeAssistant, room_map: dict) -> None:
    """Shared setup: register views, services, audio dir."""
    audio_dir = hass.config.path(WWW_DIR, AUDIO_SUBDIR)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(
        {
            "rooms": room_map,
            "audio_dir": audio_dir,
        }
    )

    await hass.async_add_executor_job(lambda: os.makedirs(audio_dir, exist_ok=True))

    # Generate a shared secret token for PWA ↔ backend auth.
    # The Companion App's WebView doesn't carry HA auth for /api/ calls;
    # we use a simple bearer token injected into the HTML instead.
    hass.data[DOMAIN]["pwa_token"] = secrets.token_urlsafe(32)

    register_api_views(hass)
    _register_services(hass)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


def _register_services(hass: HomeAssistant) -> None:
    """Register home_intercom.announce service."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, "announce", _handle_announce)
