"""Shared modules — imported by both Docker (Flask) and HA integration.

Audio: both deployment modes use the same PCM→WAV conversion and WAV
concatenation. Audio constants (PCM_RATE, PCM_BPS, WAV_MAGIC,
WAV_HEADER_SIZE) come from const.py.

Devices: DeviceStoreBase holds the MAC registry CRUD logic shared by the
HA integration (persistence via helpers.storage.Store) and the Docker
server (persistence via a JSON file) — see device_store.py on each side.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import wave
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

try:
    from .const import (  # HA integration (relative)
        DEVICE_NAME_PREFIX,
        DEVICE_UPDATEABLE_FIELDS,
        MAC_PATTERN,
        MAX_RECORD_SECS,
        PCM_BPS,
        PCM_RATE,
        WAV_MAGIC,
    )
except ImportError:
    from const import (  # Docker standalone (absolute)
        DEVICE_NAME_PREFIX,
        DEVICE_UPDATEABLE_FIELDS,
        MAC_PATTERN,
        MAX_RECORD_SECS,
        PCM_BPS,
        PCM_RATE,
        WAV_MAGIC,
    )

_LOGGER = logging.getLogger(__name__)


def is_wav(data: bytes) -> bool:
    """Check if raw data starts with WAV RIFF magic."""
    return data[: len(WAV_MAGIC)] == WAV_MAGIC


def handle_wav_passthrough(data: bytes, filepath: str) -> tuple[int, float]:
    """ESP32 / complete WAV file → write as-is.

    Returns (sample_rate, duration_seconds).
    """
    with open(filepath, "wb") as f:
        f.write(data)
    with wave.open(filepath, "rb") as wf:
        rate = wf.getframerate()
        duration = wf.getnframes() / rate
    _LOGGER.info(
        "WAV passthrough %dB, %dHz, %dch, %dbit, %.1fs",
        len(data),
        rate,
        wf.getnchannels(),
        wf.getsampwidth() * 8,
        duration,
    )
    return rate, duration


def handle_pcm_to_wav(data: bytes, rate: int, filepath: str) -> float:
    """Raw 16-bit mono PCM → write WAV file with correct header.

    Returns duration_seconds.
    """
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(PCM_BPS)
        wf.setframerate(rate)
        wf.writeframes(data)
    duration = len(data) / (rate * PCM_BPS)
    file_size = os.path.getsize(filepath)
    _LOGGER.info(
        "WAV written: %s (%dB, %.1fs, %dHz)",
        os.path.basename(filepath),
        file_size,
        duration,
        rate,
    )
    return duration


def concat_wavs(chime_path: str, audio_path: str, output_path: str) -> float:
    """Prepend chime WAV to audio WAV. Returns total duration (seconds).

    Both files must have the same sample rate, channels, and sample width.
    On format mismatch, copies audio as-is and logs a warning.
    """
    with wave.open(chime_path, "rb") as wf_chime:
        chime_rate = wf_chime.getframerate()
        chime_frames = wf_chime.readframes(wf_chime.getnframes())
        chime_width = wf_chime.getsampwidth()
        chime_channels = wf_chime.getnchannels()

    with wave.open(audio_path, "rb") as wf_audio:
        audio_rate = wf_audio.getframerate()
        audio_frames = wf_audio.readframes(wf_audio.getnframes())
        audio_width = wf_audio.getsampwidth()
        audio_channels = wf_audio.getnchannels()

    if (chime_rate, chime_width, chime_channels) != (audio_rate, audio_width, audio_channels):
        _LOGGER.warning(
            "chime/audio format mismatch (chime=%dHz/%dB/%dch, audio=%dHz/%dB/%dch) — skipping chime",
            chime_rate,
            chime_width,
            chime_channels,
            audio_rate,
            audio_width,
            audio_channels,
        )
        shutil.copyfile(audio_path, output_path)
        with wave.open(output_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        return duration

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(audio_channels)
        wf.setsampwidth(audio_width)
        wf.setframerate(audio_rate)
        wf.writeframes(chime_frames + audio_frames)

    total_frames = (len(chime_frames) + len(audio_frames)) // (audio_width * audio_channels)
    duration = total_frames / audio_rate
    _LOGGER.info("chime + audio combined: %s (%.1fs)", os.path.basename(output_path), duration)
    return duration


# ═══════════════════════════════════════════════════════════════════════
# Device registry — shared CRUD (issue #40, PR review)
# ═══════════════════════════════════════════════════════════════════════

_MAC_RE = re.compile(MAC_PATTERN)


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to the uppercase colon-separated form."""
    return mac.strip().upper()


def default_device_name(mac: str) -> str:
    """Default name for auto-registered devices: "Device EE:FF"."""
    return f"{DEVICE_NAME_PREFIX} {':'.join(mac.split(':')[-2:])}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DeviceStoreBase:
    """MAC → device-config CRUD, shared by the HA and Docker device stores.

    Subclasses provide persistence: they override the public
    register_or_update / update_field / revoke methods to call the
    protected _-prefixed implementation here, then save.

    Note on revoke(): devices are flagged "revoked", not deleted. Deleting
    would be pointless — /devices/hello auto-registers unknown MACs
    (trust-on-first-use), so a deleted device would simply re-register on
    its next boot. The flag is what actually blocks future hellos (#37)
    and record calls (#47).
    """

    def __init__(self) -> None:
        self._devices: dict[str, dict[str, Any]] = {}

    def get(self, mac: str) -> dict[str, Any] | None:
        """Return a copy of the device info for a MAC, or None if unknown."""
        device = self._devices.get(normalize_mac(mac))
        return dict(device) if device is not None else None

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Snapshot of all registered devices (MAC → info)."""
        return {mac: dict(d) for mac, d in self._devices.items()}

    def _register_or_update(
        self, mac: str, firmware_version: str = ""
    ) -> tuple[dict[str, Any], bool]:
        """Shared register/update logic. Returns (device_copy, created)."""
        mac = normalize_mac(mac)
        if not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC address: {mac!r}")

        now = _now_iso()
        device = self._devices.get(mac)
        created = device is None
        if created:
            device = {
                "name": default_device_name(mac),
                "room": "",
                "created_at": now,
                "last_seen": now,
                "firmware_version": firmware_version,
                "revoked": False,
            }
            self._devices[mac] = device
        else:
            device["last_seen"] = now
            if firmware_version:
                device["firmware_version"] = firmware_version
        return dict(device), created

    def _update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Shared update logic. Raises ValueError on a non-updateable field."""
        if key not in DEVICE_UPDATEABLE_FIELDS:
            raise ValueError(f"field not updateable: {key!r}")
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device[key] = value
        return dict(device)

    def _revoke(self, mac: str) -> dict[str, Any] | None:
        """Shared revoke logic: flags "revoked", never deletes."""
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device["revoked"] = True
        return dict(device)

    def _remove(self, mac: str) -> None:
        """Shared remove logic: permanently deletes from registry."""
        key = normalize_mac(mac)
        self._devices.pop(key, None)


def device_hello_payload(device: dict[str, Any]) -> dict[str, Any]:
    """Build the POST /devices/hello response payload (issue #37).

    Delivers everything an ESP32 needs at boot: its name/room binding
    plus the global audio parameters.
    """
    return {
        "status": "ok",
        "device_name": device["name"],
        "room": device.get("room", ""),
        "sample_rate": PCM_RATE,
        "max_record_secs": MAX_RECORD_SECS,
    }


def config_payload() -> dict[str, Any]:
    """Build the GET /config response — global audio settings (issue #39).

    Field names match the hello payload: this is the same audio config,
    discoverable pre-registration and by non-ESP32 clients.
    """
    return {
        "sample_rate": PCM_RATE,
        "max_record_secs": MAX_RECORD_SECS,
    }


class DeviceRecordFault(StrEnum):
    """Why a device may not record (issue #47). Serialized as its string value."""

    UNKNOWN_DEVICE = "unknown device"
    DEVICE_REVOKED = "device revoked"


def device_record_auth_error(device: dict[str, Any] | None) -> DeviceRecordFault | None:
    """Return why a device may not record, or None if it may (issue #47)."""
    if device is None:
        return DeviceRecordFault.UNKNOWN_DEVICE
    if device.get("revoked"):
        return DeviceRecordFault.DEVICE_REVOKED
    return None
