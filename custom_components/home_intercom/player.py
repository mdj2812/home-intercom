"""Media player playback logic — adapted from ha_client.py for HA integration.

Uses direct hass.services.async_call() instead of REST — no HA_TOKEN needed.

Three-tier auto-stop strategy (unchanged from container version):
  1. Music Assistant → music_assistant.play_announcement (self-stopping)
  2. Modern players (SUPPORT_REPEAT_SET) → play_media(announce=True), no timer
  3. Basic players → play_media(announce=True) + pause timer fallback
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Bit flags from HA core: homeassistant/components/media_player/const.py
SUPPORT_PLAY_MEDIA = 1 << 9  # = 512
SUPPORT_REPEAT_SET = 1 << 18  # = 262144

# Retry / timing constants
STATE_POLL_INTERVAL = 0.5
PLAYING_CONFIRM_RETRIES = 10  # 10 × 0.5s = 5s max
PAUSE_RETRIES = 5
DEFAULT_PAUSE_BUFFER = 0.0


class PlayResult:
    """Result of a play_announcement call."""

    def __init__(self, ok: bool, error: str = ""):
        self.ok = ok
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": self.ok}
        if self.error:
            result["error"] = self.error
        return result


async def _entity_attrs(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Get entity attributes from HA state machine (no REST call)."""
    state = hass.states.get(entity_id)
    if not state:
        return {}
    return dict(state.attributes)


def _has_play_media(attrs: dict[str, Any]) -> bool:
    """Check if entity supports play_media service (bit 9)."""
    return bool(attrs.get("supported_features", 0) & SUPPORT_PLAY_MEDIA)


def _has_repeat_set(attrs: dict[str, Any]) -> bool:
    """Check if entity supports repeat_set (bit 18) — modernity proxy."""
    return bool(attrs.get("supported_features", 0) & SUPPORT_REPEAT_SET)


def _is_ma_player(attrs: dict[str, Any]) -> bool:
    """Detect Music Assistant-exposed media_player entities."""
    return attrs.get("app_id") == "music_assistant"


async def play_announcement(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    duration: float,
    *,
    announce_volume: int | None = None,
    audio_url_with_chime: str | None = None,
    duration_with_chime: float | None = None,
    pause_buffer: float = DEFAULT_PAUSE_BUFFER,
) -> PlayResult:
    """Play an announcement on a media_player entity.

    Args:
        hass: Home Assistant core object.
        entity_id: media_player entity ID.
        audio_url: URL to the audio file (HA-relative or absolute).
        duration: Audio duration in seconds.
        announce_volume: Optional volume (1-100) for MA players.
        audio_url_with_chime: URL with chime prepended (for standard players).
        duration_with_chime: Duration of chime + audio.
        pause_buffer: Extra seconds before auto-pause (HomePod needs ~1.0).

    Returns:
        PlayResult with .ok and optional .error.
    """
    state = hass.states.get(entity_id)
    if not state or state.state == "unavailable":
        _LOGGER.warning("Entity %s is unavailable, skipping", entity_id)
        return PlayResult(ok=False, error="unavailable")

    attrs = dict(state.attributes)

    # Tier 1: Music Assistant
    if _is_ma_player(attrs):
        return await _play_ma_announcement(
            hass, entity_id, audio_url, announce_volume
        )

    # Guard: entity must support play_media
    if not _has_play_media(attrs):
        _LOGGER.warning(
            "Entity %s lacks SUPPORT_PLAY_MEDIA (bit 9), skipping",
            entity_id,
        )
        return PlayResult(ok=False, error="no_play_media")

    # Use chime version for standard players
    play_url = audio_url_with_chime or audio_url
    play_duration = duration_with_chime or duration

    # Tier 2: Modern player (supports repeat_set → assume announce=True works)
    if _has_repeat_set(attrs):
        return await _play_standard(hass, entity_id, play_url, announce_volume=announce_volume)

    # Tier 3: Basic player with pause timer
    return await _play_with_timer(hass, entity_id, play_url, play_duration, pause_buffer, announce_volume=announce_volume)


async def _play_ma_announcement(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    announce_volume: int | None,
) -> PlayResult:
    """Play via Music Assistant's play_announcement service."""
    service_data: dict[str, Any] = {
        "entity_id": entity_id,
        "url": audio_url,
        "use_pre_announce": True,
    }
    if announce_volume is not None:
        service_data["announce_volume"] = announce_volume

    try:
        await hass.services.async_call(
            "music_assistant",
            "play_announcement",
            service_data,
            blocking=True,
        )
        return PlayResult(ok=True)
    except Exception as e:
        _LOGGER.error("MA announcement failed for %s: %s", entity_id, e)
        return PlayResult(ok=False, error="ma_failed")


async def _call_play_media(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    *,
    announce_volume: int | None = None,
) -> PlayResult:
    """Call entity.async_play_media directly with optional volume boost.

    Saves current volume, sets announce volume if higher, restores after.
    """
    try:
        entity = hass.data["entity_components"]["media_player"].get_entity(entity_id)
        if entity is None:
            return PlayResult(ok=False, error="entity_not_found")

        # Volume boost: save current → set announce volume
        saved_volume = None
        if announce_volume is not None and announce_volume > 0:
            state = hass.states.get(entity_id)
            cv = state.attributes.get("volume_level") if state else None
            if cv is not None and cv * 100 < announce_volume:
                saved_volume = cv
                target = announce_volume / 100.0
                await hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": entity_id, "volume_level": target},
                    blocking=True,
                )

        await entity.async_play_media("music", audio_url, announce=True)

        # Schedule volume restore (non-blocking)
        if saved_volume is not None:
            async def _restore():
                await asyncio.sleep(5)  # Let playback start before restoring
                await hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": entity_id, "volume_level": saved_volume},
                )
            hass.async_create_background_task(
                _restore(), f"home_intercom_restore_vol_{entity_id}"
            )

        return PlayResult(ok=True)
    except Exception as e:
        _LOGGER.error("play_media failed for %s: %s", entity_id, e)
        return PlayResult(ok=False, error="play_failed")


async def _play_standard(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    *,
    announce_volume: int | None = None,
) -> PlayResult:
    """Play via entity.async_play_media directly.

    For modern players (HomePod, Chromecast) that handle announce correctly.
    """
    try:
        entity = hass.data["entity_components"]["media_player"].get_entity(entity_id)
        if entity is None:
            return PlayResult(ok=False, error="entity_not_found")

        # Volume boost: save current → set announce volume
        saved_volume = None
        if announce_volume is not None and announce_volume > 0:
            state = hass.states.get(entity_id)
            cv = state.attributes.get("volume_level") if state else None
            if cv is not None and cv * 100 < announce_volume:
                saved_volume = cv
                target = announce_volume / 100.0
                await hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": entity_id, "volume_level": target},
                    blocking=True,
                )

        await entity.async_play_media("music", audio_url, announce=True)

        # Schedule volume restore (non-blocking)
        if saved_volume is not None:
            async def _restore():
                await asyncio.sleep(5)
                await hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": entity_id, "volume_level": saved_volume},
                )
            hass.async_create_background_task(
                _restore(), f"home_intercom_restore_vol_{entity_id}"
            )

        return PlayResult(ok=True)
    except Exception as e:
        _LOGGER.error("play_media failed for %s: %s", entity_id, e)
        return PlayResult(ok=False, error="play_failed")


async def _play_with_timer(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    duration: float,
    pause_buffer: float,
    *,
    announce_volume: int | None = None,
) -> PlayResult:
    """Play via media_player.play_media + background pause timer.

    For basic players (Xiaomi via miot) that need manual pause after playback.
    """
    result = await _call_play_media(hass, entity_id, audio_url, announce_volume=announce_volume)
    if not result.ok:
        return result

    # Background pause timer
    hass.async_create_background_task(
        _auto_pause(hass, entity_id, duration, pause_buffer),
        f"home_intercom_pause_{entity_id}",
    )
    return PlayResult(ok=True)


async def _auto_pause(
    hass: HomeAssistant,
    entity_id: str,
    duration: float,
    pause_buffer: float,
) -> None:
    """Background task: wait for playback to finish, then pause."""
    # Wait for playing state (up to 5s)
    for _ in range(PLAYING_CONFIRM_RETRIES):
        state = hass.states.get(entity_id)
        if state and state.state == "playing":
            break
        await asyncio.sleep(STATE_POLL_INTERVAL)

    # Sleep for duration + buffer
    await asyncio.sleep(duration + pause_buffer)

    # Pause with retry
    for attempt in range(PAUSE_RETRIES):
        try:
            await hass.services.async_call(
                "media_player",
                "media_pause",
                {"entity_id": entity_id},
                blocking=True,
            )
            break
        except Exception:
            if attempt < PAUSE_RETRIES - 1:
                await asyncio.sleep(0.3)
