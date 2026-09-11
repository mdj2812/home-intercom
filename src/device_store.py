"""Device registry storage for ESP32 intercom buttons — Docker side.

CRUD logic lives in shared.py's DeviceStoreBase; this subclass only adds
persistence to a plain JSON file (atomic write + threading lock). The HA
integration subclasses the same base in custom_components' device_store.py
with homeassistant.helpers.storage.Store.
"""

from __future__ import annotations

import contextlib
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
        self._atomic_replace = True
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
        """Persist under self._lock.

        Prefer tmp + ``os.replace``. Docker file bind-mounts (QNAP) reject
        that with EBUSY/EXDEV, so fall back to writing the mounted file
        in place.
        """
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        payload = json.dumps(
            {"version": DEVICE_STORAGE_VERSION, "devices": self._devices},
            indent=2,
        )
        if self._atomic_replace:
            tmp_path = f"{self._path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._path)
                return
            except OSError as exc:
                self._atomic_replace = False
                _LOGGER.warning(
                    "atomic replace of %s failed (%s); writing in place from now on",
                    self._path,
                    exc,
                )
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    def get(self, mac: str) -> dict[str, Any] | None:
        """Return a copy of the device info for a MAC, or None if unknown."""
        with self._lock:
            return super().get(mac)

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all registered devices (MAC → info)."""
        with self._lock:
            return super().devices

    def register_or_update(
        self, mac: str, firmware_version: str = "", pins: list[int] | None = None
    ) -> dict[str, Any]:
        """Register a new device or refresh last_seen/firmware of a known one.

        Raises ValueError on a malformed MAC address.
        """
        with self._lock:
            device, created = self._register_or_update(mac, firmware_version, pins)
            if created:
                _LOGGER.info(
                    "Auto-registered new device %s (%s) — pending approval",
                    mac,
                    device["name"],
                )
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

    def approve(self, mac: str) -> dict[str, Any] | None:
        """Mark a pending device as active (issue #51).

        Returns the updated device, or None if the MAC is unknown.
        """
        with self._lock:
            device = self._approve(mac)
            if device is None:
                return None
            self._save_locked()
            _LOGGER.info("Device approved: %s (%s)", mac, device["name"])
            return device

    def request_ota(self, mac: str, target_version: str) -> dict[str, Any] | None:
        """Flag a device to flash on its next hello (manage action ``ota``)."""
        with self._lock:
            device = self._request_ota(mac, target_version)
            if device is None:
                return None
            self._save_locked()
            _LOGGER.info("OTA requested: %s → %s", mac, target_version)
            return device

    def cancel_ota(self, mac: str) -> dict[str, Any] | None:
        """Clear leftover hello ``ota`` without deapproving (manage action ``ota_cancel``)."""
        with self._lock:
            device = self._cancel_ota(mac)
            if device is None:
                return None
            self._save_locked()
            _LOGGER.info("OTA cancelled: %s", mac)
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
