"""Switch platform — per-device revoke toggle (issue #48)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BUTTONS_UNIQUE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches from config entry.

    Only the buttons entry gets revoke switches. Room entries have no switches.
    """
    if entry.unique_id != BUTTONS_UNIQUE_ID:
        return

    device_store = hass.data.get(DOMAIN, {}).get("device_store")
    if device_store is None:
        return

    entities: list[ButtonRevokeSwitch] = []
    for mac, dev in device_store.devices.items():
        entities.append(
            ButtonRevokeSwitch(
                entry=entry,
                mac=mac,
                device_name=dev.get("name", mac),
            )
        )

    async_add_entities(entities)


class ButtonRevokeSwitch(SwitchEntity):
    """Switch: toggle to revoke / un-revoke an intercom button (issue #48).

    ON  = revoked (blocked, won't accept hello)
    OFF = active (normal operation)
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cancel"

    def __init__(self, entry: ConfigEntry, mac: str, device_name: str) -> None:
        """Initialize."""
        self._entry = entry
        self._mac = mac
        self._device_name = device_name
        self.entity_description = SwitchEntityDescription(
            key="revoked",
            translation_key="revoked",
            entity_category=EntityCategory.CONFIG,
        )
        self._attr_unique_id = f"{entry.entry_id}_{mac}_revoked_v1"
        self._attr_name = "Revoked"
        self.entity_id = f"switch.{_safe_entity_id(mac)}_revoked"

    @property
    def device_info(self):
        """Associate with the button's HA device."""
        return {"identifiers": {(DOMAIN, self._mac)}}

    @property
    def is_on(self) -> bool:
        """Return True if the device is revoked."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return False
        dev = store.devices.get(self._mac)
        if dev is None:
            return False
        return bool(dev.get("revoked", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Revoke the device (block hello)."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return
        await store.revoke(self._mac)
        _LOGGER.info("Button %s revoked via switch", self._mac)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Un-revoke the device (allow hello again)."""
        store = self.hass.data.get(DOMAIN, {}).get("device_store")
        if store is None:
            return
        await store.update_field(self._mac, "revoked", False)
        _LOGGER.info("Button %s un-revoked via switch", self._mac)
        self.async_write_ha_state()


def _safe_entity_id(mac: str) -> str:
    """Convert a MAC address to a safe entity id fragment."""
    return mac.lower().replace(":", "_")
