"""Shared modules — imported by both Docker (Flask) and HA integration.

Audio: both deployment modes use the same PCM→WAV conversion and WAV
concatenation. Audio constants (PCM_RATE, PCM_BPS, WAV_MAGIC,
WAV_HEADER_SIZE) come from const.py.

Playback: auto_pause.py holds the Tier-3 confirm→sleep→pause algorithm
shared by ha_client (sync REST/WS) and player.py (async HA services).

Devices: DeviceStoreBase holds the MAC registry CRUD logic shared by the
HA integration (persistence via helpers.storage.Store) and the Docker
server (persistence via a JSON file) — see device_store.py on each side.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

try:
    from .const import (  # HA integration (relative)
        CUSTOM_CHIME_FILENAME,
        DEFAULT_CHIME_STATIC_URL,
        DEVICE_NAME_PREFIX,
        DEVICE_UPDATEABLE_FIELDS,
        MAC_PATTERN,
        MAX_CHIME_BYTES,
        MAX_RECORD_SECS,
        PCM_BPS,
        PCM_RATE,
        WAV_MAGIC,
    )
except ImportError:
    from const import (  # Docker standalone (absolute)
        CUSTOM_CHIME_FILENAME,
        DEFAULT_CHIME_STATIC_URL,
        DEVICE_NAME_PREFIX,
        DEVICE_UPDATEABLE_FIELDS,
        MAC_PATTERN,
        MAX_CHIME_BYTES,
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
# Custom chime — shared resolve / upload (issue #66)
# ═══════════════════════════════════════════════════════════════════════


def default_chime_path(integration_dir: Path) -> Path:
    """Bundled pre-announce WAV shipped with the integration."""
    return integration_dir / "static" / "pre_announce.wav"


def custom_chime_path(audio_dir: str) -> Path:
    """User-uploaded chime stored alongside recorded announcements."""
    return Path(audio_dir) / CUSTOM_CHIME_FILENAME


def has_custom_chime(audio_dir: str) -> bool:
    """True when a user-uploaded custom chime file exists."""
    return custom_chime_path(audio_dir).is_file()


def resolve_chime_wav(*, integration_dir: Path, audio_dir: str) -> Path:
    """Return custom chime if uploaded, else bundled default."""
    custom = custom_chime_path(audio_dir)
    if custom.is_file():
        return custom
    return default_chime_path(integration_dir)


def chime_public_url(
    base_url: str, *, audio_dir: str, deployment: Literal["ha", "docker"]
) -> str | None:
    """Public URL for MA pre_announce_url; None when using bundled default only."""
    if not has_custom_chime(audio_dir):
        return None
    base = base_url.rstrip("/")
    if deployment == "ha":
        return f"{base}/local/home_intercom_audio/{CUSTOM_CHIME_FILENAME}"
    return f"{base}/audio/{CUSTOM_CHIME_FILENAME}"


def chime_status_payload(
    *,
    base_url: str,
    audio_dir: str,
    deployment: Literal["ha", "docker"],
) -> dict[str, Any]:
    """Build GET /chime JSON — active URL plus whether it is custom."""
    custom = has_custom_chime(audio_dir)
    if custom:
        url = chime_public_url(base_url, audio_dir=audio_dir, deployment=deployment)
    else:
        url = DEFAULT_CHIME_STATIC_URL
    return {
        "custom": custom,
        "url": url or DEFAULT_CHIME_STATIC_URL,
        "default_url": DEFAULT_CHIME_STATIC_URL,
    }


def write_custom_chime_wav(data: bytes, audio_dir: str) -> None:
    """Persist uploaded WAV as the custom chime."""
    if len(data) > MAX_CHIME_BYTES:
        raise ValueError("chime too large")
    if not is_wav(data):
        raise ValueError("not a wav file")
    path = custom_chime_path(audio_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _LOGGER.info("custom chime saved: %s (%dB)", path.name, len(data))


def delete_custom_chime(audio_dir: str) -> bool:
    """Remove custom chime file. Returns True if a file was deleted."""
    path = custom_chime_path(audio_dir)
    if path.is_file():
        path.unlink()
        _LOGGER.info("custom chime removed: %s", path.name)
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# Device registry — shared CRUD (issue #40, PR review)
# ═══════════════════════════════════════════════════════════════════════

_MAC_RE = re.compile(MAC_PATTERN)


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to the uppercase colon-separated form."""
    return mac.strip().upper()


def normalize_firmware_version(value: str) -> str:
    """Strip a leading ``v`` so GitHub tags match ESP32 FIRMWARE_VERSION."""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] in "vV" and text[1].isdigit():
        return text[1:]
    return text


def firmware_update_available(current: str, latest: str) -> bool:
    """True when both sides are known and the device is not on ``latest``."""
    lat = normalize_firmware_version(latest)
    if not lat:
        return False
    cur = normalize_firmware_version(current)
    if not cur:
        return True
    return cur != lat


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
    would be pointless — /devices/hello auto-registers unknown MACs, so a
    deleted device would simply re-register on its next boot. The flag is
    what actually blocks future hellos (#37) and record calls (#47).

    Note on pending (issue #51): unknown MACs still auto-register, but new
    devices start pending until an admin approves them. Existing records
    without a ``pending`` key are treated as approved (grandfathered).
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
                "pending": True,
            }
            self._devices[mac] = device
        else:
            device["last_seen"] = now
            if firmware_version:
                device["firmware_version"] = firmware_version
                self._clear_ota_if_matched(device, firmware_version)
        return dict(device), created

    def _update_field(self, mac: str, key: str, value: Any) -> dict[str, Any] | None:
        """Shared update logic. Raises ValueError on a non-updateable field."""
        if key not in DEVICE_UPDATEABLE_FIELDS:
            raise ValueError(f"field not updateable: {key!r}")
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device[key] = value
        if key == "pending" and not value:
            pending_hello_hub.notify(normalize_mac(mac))
        return dict(device)

    def _revoke(self, mac: str) -> dict[str, Any] | None:
        """Shared revoke logic: flags "revoked", never deletes."""
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device["revoked"] = True
        pending_hello_hub.notify(mac)
        return dict(device)

    def _approve(self, mac: str) -> dict[str, Any] | None:
        """Clear the pending flag so hello delivers config and record is allowed."""
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device["pending"] = False
        pending_hello_hub.notify(mac)
        return dict(device)

    def _remove(self, mac: str) -> None:
        """Shared remove logic: permanently deletes from registry."""
        key = normalize_mac(mac)
        self._devices.pop(key, None)
        pending_hello_hub.notify(key)

    @staticmethod
    def _clear_ota_if_matched(device: dict[str, Any], firmware_version: str) -> None:
        """Drop OTA flags once hello reports the requested firmware version."""
        if not device.get("ota_requested"):
            return
        target = normalize_firmware_version(str(device.get("ota_target_version") or ""))
        current = normalize_firmware_version(firmware_version)
        if target and current == target:
            device["ota_requested"] = False
            device["ota_target_version"] = ""

    def _request_ota(self, mac: str, target_version: str) -> dict[str, Any] | None:
        """Mark an approved device to flash on its next hello."""
        device = self._devices.get(normalize_mac(mac))
        if device is None:
            return None
        device["ota_requested"] = True
        device["ota_target_version"] = normalize_firmware_version(target_version)
        return dict(device)


class PendingHelloHub:
    """Wake a held /devices/hello when the device is approved (issue #51).

    ESP32 is the HTTP client, so the server cannot push. Pending hello
    waits here until notify() or timeout, then returns the ok payload.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def wait(
        self,
        mac: str,
        timeout: float,
        still_waiting: Callable[[], bool] | None = None,
    ) -> bool:
        """Block until notify() or timeout. True if notify won or already done.

        ``still_waiting`` is re-checked after this hello is registered so an
        approve that landed between the pending read and wait() is not missed.
        """
        if timeout <= 0:
            return False
        key = normalize_mac(mac)
        event = threading.Event()
        with self._lock:
            prev = self._events.get(key)
            self._events[key] = event
        if prev is not None:
            prev.set()
        if still_waiting is not None and not still_waiting():
            with self._lock:
                if self._events.get(key) is event:
                    del self._events[key]
            return True
        notified = event.wait(timeout=timeout)
        with self._lock:
            if self._events.get(key) is event:
                del self._events[key]
        return notified

    def notify(self, mac: str) -> None:
        key = normalize_mac(mac)
        with self._lock:
            event = self._events.get(key)
        if event is not None:
            event.set()


pending_hello_hub = PendingHelloHub()

# Default stays below current ESP32 hello HTTP timeout (10s). After firmware
# HELLO_HTTP_TIMEOUT_MS=30000 this can be raised (e.g. 22s) for a longer hold.
_DEFAULT_PENDING_HELLO_WAIT_SECS = 8.0


def pending_hello_wait_secs() -> float:
    raw = os.environ.get("HOME_INTERCOM_PENDING_HELLO_WAIT", str(_DEFAULT_PENDING_HELLO_WAIT_SECS))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_PENDING_HELLO_WAIT_SECS


def wait_for_pending_hello(get_device: Any, mac: str) -> dict[str, Any] | None:
    """If the device is pending, block until approve/revoke or timeout."""

    def still_pending() -> bool:
        current = get_device(mac)
        return bool(current and current.get("pending") and not current.get("revoked"))

    device = get_device(mac)
    if device is None or not still_pending():
        return device
    pending_hello_hub.wait(mac, pending_hello_wait_secs(), still_pending)
    return get_device(mac)


def device_hello_payload(device: dict[str, Any]) -> dict[str, Any]:
    """Build the POST /devices/hello response payload (issue #37, #51).

    Pending devices get ``{"status": "pending"}`` with no room/config so
    the ESP32 can retry. Approved devices get name/room plus audio params.
    """
    if device.get("pending"):
        return {"status": "pending"}
    payload: dict[str, Any] = {
        "status": "ok",
        "device_name": device["name"],
        "room": device.get("room", ""),
        "sample_rate": PCM_RATE,
        "max_record_secs": MAX_RECORD_SECS,
    }
    if device.get("ota_requested"):
        payload["ota"] = True
    return payload


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
    DEVICE_PENDING = "device pending"


def device_ota_reject_reason(device: dict[str, Any] | None) -> str | None:
    """Why manage action ``ota`` is refused, or None if it may proceed."""
    if device is None:
        return "unknown device"
    if device.get("pending"):
        return "device pending"
    if device.get("revoked"):
        return "device revoked"
    return None


def device_record_auth_error(device: dict[str, Any] | None) -> DeviceRecordFault | None:
    """Return why a device may not record, or None if it may (issue #47, #51)."""
    if device is None:
        return DeviceRecordFault.UNKNOWN_DEVICE
    if device.get("revoked"):
        return DeviceRecordFault.DEVICE_REVOKED
    if device.get("pending"):
        return DeviceRecordFault.DEVICE_PENDING
    return None


def devices_payload(
    store: DeviceStoreBase, latest_firmware: str = ""
) -> dict[str, dict[str, Any]]:
    """GET /devices response — read-only registry listing for the PWA (issue #52).

    The store's snapshot is already a defensive copy keyed by MAC. When
    ``latest_firmware`` is known (cached GitHub image), each device gets
    ``firmware_latest`` and ``firmware_update_available`` for the PWA.
    """
    latest = normalize_firmware_version(latest_firmware)
    if not latest:
        return store.devices
    out: dict[str, dict[str, Any]] = {}
    for mac, device in store.devices.items():
        item = dict(device)
        item["firmware_latest"] = latest
        item["firmware_update_available"] = firmware_update_available(
            str(device.get("firmware_version") or ""), latest
        )
        out[mac] = item
    return out


DEVICE_MANAGE_ACTIONS = frozenset({"approve", "deapprove", "revoke", "unrevoke", "delete", "ota"})


def parse_device_manage_body(body: Any) -> tuple[str, str] | str:
    """Parse POST /devices/manage JSON. Returns ``(mac, action)`` or an error string."""
    if not isinstance(body, dict):
        return "invalid body"
    mac = str(body.get("mac") or "").strip()
    action = str(body.get("action") or "").strip()
    if not mac:
        return "missing mac"
    if action not in DEVICE_MANAGE_ACTIONS:
        return "invalid action"
    return mac, action
