"""Device registry storage for ESP32 intercom buttons — Docker side.

Plain JSON file persistence with a threading lock. The HA integration
uses its own custom_components device_store.py backed by
homeassistant.helpers.storage.Store.

Note on revoke(): devices are flagged "revoked", not deleted. Deleting
would be pointless — /devices/hello auto-registers unknown MACs
(trust-on-first-use), so a deleted device would simply re-register on
its next boot. The flag is what actually blocks future hellos (#37)
and record calls (#47).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from typing import Any

from const import (
    DEVICE_NAME_PREFIX,
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
    """Thread-safe MAC → device-config store backed by a JSON file."""

    def __init__(self, path: str) -> None:
        """Initialize and load existing data (missing file = empty registry)."""
        self._path = path
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
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
        """Return device info for a MAC, or None if unknown."""
        with self._lock:
            device = self._devices.get(normalize_mac(mac))
            return dict(device) if device is not None else None

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all registered devices (MAC → info)."""
        with self._lock:
            return {mac: dict(d) for mac, d in self._devices.items()}

    def register_or_update(self, mac: str, firmware_version: str = "") -> dict[str, Any]:
        """Register a new device or refresh last_seen/firmware of a known one.

        Raises ValueError on a malformed MAC address.
        """
        mac = normalize_mac(mac)
        if not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC address: {mac!r}")

        with self._lock:
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

            self._save_locked()
            return dict(device)

    def update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Update one field of a registered device (for UI edits).

        Only fields in DEVICE_UPDATEABLE_FIELDS may be changed.
        Returns the updated device, or None if the MAC is unknown.
        Raises ValueError on a non-updateable field.
        """
        if key not in DEVICE_UPDATEABLE_FIELDS:
            raise ValueError(f"field not updateable: {key!r}")
        with self._lock:
            device = self._devices.get(normalize_mac(mac))
            if device is None:
                return None
            device[key] = value
            self._save_locked()
            return dict(device)

    def revoke(self, mac: str) -> dict[str, Any] | None:
        """Block a device from future hello/record calls.

        Flags instead of deletes — see module docstring.
        Returns the updated device, or None if the MAC is unknown.
        """
        with self._lock:
            device = self._devices.get(normalize_mac(mac))
            if device is None:
                return None
            device["revoked"] = True
            self._save_locked()
            _LOGGER.warning("Device revoked: %s (%s)", normalize_mac(mac), device["name"])
            return dict(device)
