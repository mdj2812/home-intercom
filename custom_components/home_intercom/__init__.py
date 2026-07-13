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

    # Create a system-level API token for the PWA (Companion App compat).
    # Token is stored in hass.data and injected into the HTML by PanelView.
    api_token = await _create_pwa_token(hass)
    if api_token:
        hass.data[DOMAIN]["api_token"] = api_token

    register_api_views(hass)
    _register_services(hass)

    _LOGGER.info("Home Intercom set up — %d rooms, audio: %s", len(room_map), audio_dir)


async def _create_pwa_token(hass: HomeAssistant) -> str | None:
    """Create a long-lived access token for the PWA frontend."""
    try:
        # Get the owner user (or first available user)
        users = await hass.auth.async_get_users()
        owner = None
        for u in users:
            if u.is_owner:
                owner = u
                break
        if not owner and users:
            owner = users[0]
        if not owner:
            _LOGGER.warning("No HA users found — PWA token injection skipped")
            return None

        # Create a refresh token with 10-year expiration
        from datetime import timedelta

        refresh_token = await hass.auth.async_create_refresh_token(
            owner,
            client_name="Home Intercom PWA",
            access_token_expiration=timedelta(days=3650),
        )
        access_token = hass.auth.async_create_access_token(refresh_token)
        _LOGGER.info("Created PWA API token for user %s", owner.name)
        return access_token
    except Exception as exc:
        _LOGGER.warning("Failed to create PWA token (web browser will still work via localStorage): %s", exc)
        return None


def _register_services(hass: HomeAssistant) -> None:
    """Register home_intercom.announce service."""

    async def _handle_announce(call: ServiceCall):
        await handle_announce_service(hass, call)

    hass.services.async_register(DOMAIN, "announce", _handle_announce)
