"""Media player playback logic — adapted from ha_client.py for HA integration.

Uses direct hass.services.async_call() instead of REST — no HA_TOKEN needed.

Three-tier auto-stop strategy (unchanged from container version):
  1. Music Assistant → music_assistant.play_announcement (self-stopping)
  2. Modern players (SUPPORT_REPEAT_SET) → play_media(announce=True), no timer
  3. Basic players → play_media(announce=True) + pause timer fallback
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Bit flags from HA core: homeassistant/components/media_player/const.py
SUPPORT_PLAY_MEDIA = 1 << 9  # = 512
SUPPORT_REPEAT_SET = 1 << 18  # = 262144

# Retry / timing constants
STATE_POLL_INTERVAL = 0.5
PLAYING_CONFIRM_RETRIES = 10  # 10 × 0.5s = 5s max
PAUSE_RETRIES = 5
DEFAULT_PAUSE_BUFFER = 0.0

# Xiaomi screen-speaker display-clear via TTS
_XIAOMI_CLEAR_TEXT = "关机"
_XIAOMI_TTS_DOMAIN = "xiaomi_miot"
_XIAOMI_TTS_SERVICE = "intelligent_speaker"


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
        pause_buffer: Extra seconds before auto-pause (default 0.0, tuned per-room).

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
        return await _play_ma_announcement(hass, entity_id, audio_url, announce_volume)

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
    return await _play_with_timer(
        hass, entity_id, play_url, play_duration, pause_buffer, announce_volume=announce_volume
    )


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


async def _clear_xiaomi_display(hass: HomeAssistant, entity_id: str) -> None:
    """Clear screen metadata on Xiaomi devices via silent TTS.

    player_play_music hardcodes audio_id → cloud metadata persists
    until the display is refreshed with new content.
    """
    state = hass.states.get(entity_id)
    if not state or not state.attributes.get("xiaoai_id"):
        return
    with contextlib.suppress(Exception):
        await hass.services.async_call(
            _XIAOMI_TTS_DOMAIN,
            _XIAOMI_TTS_SERVICE,
            {
                "entity_id": entity_id,
                "text": _XIAOMI_CLEAR_TEXT,
                "silent": True,
                "execute": True,
            },
            blocking=True,
        )


async def _call_play_media(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    *,
    announce_volume: int | None = None,
) -> tuple[PlayResult, float | None]:
    """Call entity.async_play_media directly with optional volume boost.

    Saves current volume, sets announce volume if higher, restores after.
    """
    try:
        state = hass.states.get(entity_id)
        if state is None:
            return PlayResult(ok=False, error="entity_not_found"), None

        # Volume boost: save current → set announce volume
        saved_volume = None
        if announce_volume is not None and announce_volume > 0:
            cv = state.attributes.get("volume_level")
            if cv is not None and cv * 100 < announce_volume:
                saved_volume = cv
                target = announce_volume / 100.0
                await hass.services.async_call(
                    "media_player",
                    "volume_set",
                    {"entity_id": entity_id, "volume_level": target},
                    blocking=True,
                )

        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": entity_id,
                "media_content_id": audio_url,
                "media_content_type": "music",
                "extra": {
                    "announce": True,
                },
            },
            blocking=True,
        )
        return PlayResult(ok=True), saved_volume
    except Exception as e:
        _LOGGER.error("play_media failed for %s: %s", entity_id, e)
        return PlayResult(ok=False, error="play_failed"), None


async def _play_standard(
    hass: HomeAssistant,
    entity_id: str,
    audio_url: str,
    *,
    announce_volume: int | None = None,
) -> PlayResult:
    """Play via entity.async_play_media directly.

    For modern players (HomePod, Chromecast) that handle announce correctly.
    Volume restore via state change listener.
    """
    result, saved_volume = await _call_play_media(
        hass, entity_id, audio_url, announce_volume=announce_volume
    )
    if not result.ok:
        return result

    # Schedule cleanup when playback finishes (stop + optional volume restore)
    async def _cleanup_after_playback():
        import asyncio as _asyncio

        from homeassistant.helpers.event import async_track_state_change_event

        done_ev = _asyncio.Event()

        def _cb(event):
            if event.data.get("entity_id") == entity_id:
                ns = event.data.get("new_state")
                if ns and ns.state != "playing":
                    done_ev.set()

        unsub = async_track_state_change_event(hass, [entity_id], _cb)
        try:
            await _asyncio.wait_for(done_ev.wait(), timeout=120)
        except TimeoutError:
            pass
        finally:
            unsub()
        # Restore volume
        if saved_volume is not None:
            with contextlib.suppress(Exception):
                await hass.services.async_call(
                    "media_player",
                    "volume_set",
                    {"entity_id": entity_id, "volume_level": saved_volume},
                    blocking=True,
                )

        # Clear Xiaomi screen speakers (same logic as _auto_pause)
        await _clear_xiaomi_display(hass, entity_id)

    hass.async_create_background_task(
        _cleanup_after_playback(), f"home_intercom_cleanup_{entity_id}"
    )

    return PlayResult(ok=True)


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
    result, saved_volume = await _call_play_media(
        hass, entity_id, audio_url, announce_volume=announce_volume
    )
    if not result.ok:
        return result

    # Background pause timer (handles volume restore too)
    hass.async_create_background_task(
        _auto_pause(hass, entity_id, duration, pause_buffer, saved_volume),
        f"home_intercom_pause_{entity_id}",
    )
    return PlayResult(ok=True)


async def _auto_pause(
    hass: HomeAssistant,
    entity_id: str,
    duration: float,
    pause_buffer: float,
    saved_volume: float | None = None,
) -> None:
    """Background task: wait for playback to finish, then pause + restore volume.

    Listens for state_changed events instead of fixed-duration sleep.
    Falls back to duration-based timeout if state event never arrives.
    Restores original volume after pausing (if volume was boosted).
    """
    import asyncio

    from homeassistant.helpers.event import async_track_state_change_event

    done = asyncio.Event()

    def _on_state_change(event):
        if event.data.get("entity_id") != entity_id:
            return
        new_state = event.data.get("new_state")
        if new_state and new_state.state != "playing":
            done.set()

    # Wait up to 5s for playing, then listen for state change
    for _ in range(PLAYING_CONFIRM_RETRIES):
        state = hass.states.get(entity_id)
        if state and state.state == "playing":
            break
        await asyncio.sleep(STATE_POLL_INTERVAL)

    unsub = async_track_state_change_event(hass, [entity_id], _on_state_change)

    try:
        # Wait for state change with duration-based fallback timeout
        await asyncio.wait_for(done.wait(), timeout=duration + pause_buffer + 5)
    except TimeoutError:
        pass  # fallback: just pause now
    finally:
        unsub()

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

    # Restore volume if it was boosted
    if saved_volume is not None:
        with contextlib.suppress(Exception):
            await hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": saved_volume},
                blocking=True,
            )

    # Clear screen metadata on Xiaomi devices via silent TTS
    await _clear_xiaomi_display(hass, entity_id)
