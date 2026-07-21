"""Device registry storage for ESP32 intercom buttons — HA integration side.

Persists MAC → device config via homeassistant.helpers.storage.Store
(.storage/home_intercom.devices). The Docker deployment uses its own
src/device_store.py backed by a plain JSON file.

Note on revoke(): devices are flagged "revoked", not deleted. Deleting
would be pointless — /devices/hello auto-registers unknown MACs
(trust-on-first-use), so a deleted device would simply re-register on
its next boot. The flag is what actually blocks future hellos (#37)
and record calls (#47).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEVICE_NAME_PREFIX,
    DEVICE_STORAGE_KEY,
    DEVICE_STORAGE_VERSION,
    DEVICE_UPDATEABLE_FIELDS,
    MAC_PATTERN,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(MAC_PATTERN)


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to the uppercase colon-separated form."""
    return mac.strip().upper()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_device_name(mac: str) -> str:
    """Default name for auto-registered devices: "Device EE:FF"."""
    return f"{DEVICE_NAME_PREFIX} {':'.join(mac.split(':')[-2:])}"


class DeviceStore:
    """Async MAC → device-config store backed by HA .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize; call async_load() before use."""
        self._store: Store = Store(hass, DEVICE_STORAGE_VERSION, DEVICE_STORAGE_KEY)
        self._devices: dict[str, dict[str, Any]] = {}

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

    def get(self, mac: str) -> dict[str, Any] | None:
        """Return a copy of the device info for a MAC, or None if unknown."""
        device = self._devices.get(normalize_mac(mac))
        return dict(device) if device is not None else None

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all registered devices (MAC → info)."""
        return {mac: dict(d) for mac, d in self._devices.items()}

    async def register_or_update(self, mac: str, firmware_version: str = "") -> dict[str, Any]:
        """Register a new device or refresh last_seen/firmware of a known one.

        Raises ValueError on a malformed MAC address.
        """
        mac = normalize_mac(mac)
        if not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC address: {mac!r}")

        now = _now_iso()
        device = self._devices.get(mac)
        if device is None:
            device = {
                "name": default_device_name(mac),
                "room": "",
                "created_at": now,
                "last_seen": now,
                "firmware_version": firmware_version,
                "revoked": False,
            }
            self._devices[mac] = device
            _LOGGER.info("Auto-registered new device %s (%s)", mac, device["name"])
        else:
            device["last_seen"] = now
            if firmware_version:
                device["firmware_version"] = firmware_version

        await self._async_save()
        return dict(device)

    async def update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Update one field of a registered device (for UI edits).

        Only fields in DEVICE_UPDATEABLE_FIELDS may be changed.
        Returns the updated device, or None if the MAC is unknown.
        Raises ValueError on a non-updateable field.
        """
        if key not in DEVICE_UPDATEABLE_FIELDS:
            raise ValueError(f"field not updateable: {key!r}")
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device[key] = value
        await self._async_save()
        return dict(device)

    async def revoke(self, mac: str) -> dict[str, Any] | None:
        """Block a device from future hello/record calls.

        Flags instead of deletes — see module docstring.
        Returns the updated device, or None if the MAC is unknown.
        """
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device["revoked"] = True
        await self._async_save()
        _LOGGER.warning("Device revoked: %s (%s)", normalize_mac(mac), device["name"])
        return dict(device)
