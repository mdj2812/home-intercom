"""Button entities for Home Intercom.

Provides quick-announce buttons for automations and dashboards.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .announce import handle_announce_service
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BUTTON_DESCRIPTIONS = (
    ButtonEntityDescription(
        key="announce_all",
        name="Announce All",
        icon="mdi:bullhorn",
        has_entity_name=True,
    ),
)


class IntercomAnnounceButton(ButtonEntity):
    """Button to trigger a pre-configured announcement."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry_id: str,
        message: str,
        target: str = "all",
        volume: int | None = None,
    ) -> None:
        """Initialize button."""
        self.entity_description = BUTTON_DESCRIPTIONS[0]
        self._attr_unique_id = f"{DOMAIN}_announce_{target}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Home Intercom",
            "manufacturer": "Home Lab",
            "model": "PWA Intercom System",
        }
        self._entry_id = entry_id
        self._message = message
        self._target = target
        self._volume = volume

    async def async_press(self) -> None:
        """Press the button — trigger announcement."""
        from homeassistant.core import ServiceCall

        # Create a synthetic service call
        call = ServiceCall(
            domain=DOMAIN,
            service="announce",
            data={
                "target": self._target,
                "message": self._message,
                "volume": self._volume,
            },
        )
        await handle_announce_service(self.hass, call)
        _LOGGER.info(
            "Announce button pressed: target=%s message=%.40s...",
            self._target,
            self._message,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Home Intercom buttons."""
    entities = [
        IntercomAnnounceButton(
            entry.entry_id,
            message="Dinner is ready!",
            target="all",
        ),
    ]
    async_add_entities(entities)
