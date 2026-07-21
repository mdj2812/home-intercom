"""Device registry storage for ESP32 intercom buttons — HA integration side.

CRUD logic lives in shared.py's DeviceStoreBase; this subclass only adds
persistence via homeassistant.helpers.storage.Store
(.storage/home_intercom.devices). The Docker deployment subclasses the
same base in src/device_store.py with a plain JSON file.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEVICE_STORAGE_KEY, DEVICE_STORAGE_VERSION
from .shared import DeviceStoreBase

_LOGGER = logging.getLogger(__name__)


class DeviceStore(DeviceStoreBase):
    """Async MAC → device-config store backed by HA .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize; call async_load() before use."""
        super().__init__()
        self._store: Store = Store(hass, DEVICE_STORAGE_VERSION, DEVICE_STORAGE_KEY)

    async def async_load(self) -> None:
        """Load persisted devices from .storage (call once at setup)."""
        data = await self._store.async_load()
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            self._devices = data["devices"]
            _LOGGER.info("Device registry loaded: %d devices", len(self._devices))
        else:
            self._devices = {}

    async def _async_save(self) -> None:
        await self._store.async_save({"version": DEVICE_STORAGE_VERSION, "devices": self._devices})

    async def register_or_update(self, mac: str, firmware_version: str = "") -> dict[str, Any]:
        """Register a new device or refresh last_seen/firmware of a known one.

        Raises ValueError on a malformed MAC address.
        """
        device, created = self._register_or_update(mac, firmware_version)
        if created:
            _LOGGER.info("Auto-registered new device %s (%s)", mac, device["name"])
        await self._async_save()
        return device

    async def update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Update one whitelisted field of a registered device (for UI edits).

        Returns the updated device, or None if the MAC is unknown.
        Raises ValueError on a non-updateable field.
        """
        device = self._update_field(mac, key, value)
        if device is None:
            return None
        await self._async_save()
        return device

    async def revoke(self, mac: str) -> dict[str, Any] | None:
        """Block a device from future hello/record calls (flags, not deletes).

        Returns the updated device, or None if the MAC is unknown.
        """
        device = self._revoke(mac)
        if device is None:
            return None
        await self._async_save()
        _LOGGER.warning("Device revoked: %s (%s)", mac, device["name"])
        return device
