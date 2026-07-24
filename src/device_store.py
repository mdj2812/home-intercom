"""Device registry storage for ESP32 intercom buttons — Docker side.

CRUD logic lives in shared.py's DeviceStoreBase; this subclass only adds
persistence to a plain JSON file (atomic write + threading lock). The HA
integration subclasses the same base in custom_components' device_store.py
with homeassistant.helpers.storage.Store.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from const import DEVICE_STORAGE_VERSION
from shared import DeviceStoreBase

_LOGGER = logging.getLogger(__name__)


class DeviceStore(DeviceStoreBase):
    """Thread-safe MAC → device-config store backed by a JSON file."""

    def __init__(self, path: str) -> None:
        """Initialize and load existing data (missing file = empty registry)."""
        super().__init__()
        self._path = path
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            _LOGGER.error("Failed to load device registry %s: %s", self._path, exc)
            return
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            self._devices = data["devices"]
            _LOGGER.info("Device registry loaded: %d devices", len(self._devices))

    def _save_locked(self) -> None:
        """Persist under self._lock. Atomic via tmp file + replace."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"version": DEVICE_STORAGE_VERSION, "devices": self._devices},
                f,
                indent=2,
            )
        os.replace(tmp_path, self._path)

    def get(self, mac: str) -> dict[str, Any] | None:
        """Return a copy of the device info for a MAC, or None if unknown."""
        with self._lock:
            return super().get(mac)

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all registered devices (MAC → info)."""
        with self._lock:
            return super().devices

    def register_or_update(self, mac: str, firmware_version: str = "") -> dict[str, Any]:
        """Register a new device or refresh last_seen/firmware of a known one.

        Raises ValueError on a malformed MAC address.
        """
        with self._lock:
            device, created = self._register_or_update(mac, firmware_version)
            if created:
                _LOGGER.info("Auto-registered new device %s (%s)", mac, device["name"])
            self._save_locked()
            return device

    def update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Update one whitelisted field of a registered device (for UI edits).

        Returns the updated device, or None if the MAC is unknown.
        Raises ValueError on a non-updateable field.
        """
        with self._lock:
            device = self._update_field(mac, key, value)
            if device is None:
                return None
            self._save_locked()
            return device

    def revoke(self, mac: str) -> dict[str, Any] | None:
        """Block a device from future hello/record calls (flags, not deletes).

        Returns the updated device, or None if the MAC is unknown.
        """
        with self._lock:
            device = self._revoke(mac)
            if device is None:
                return None
            self._save_locked()
            _LOGGER.warning("Device revoked: %s (%s)", mac, device["name"])
            return device

    def remove(self, mac: str) -> None:
        """Permanently delete a device from the registry.

        Unlike revoke (which flags), this removes the record entirely.
        """
        with self._lock:
            if mac not in self._devices:
                return
            name = self._devices[mac].get("name", mac)
            self._remove(mac)
            self._save_locked()
            _LOGGER.info("Device removed: %s (%s)", mac, name)
