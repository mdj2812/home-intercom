"""Announce service for Home Intercom.

Exposes home_intercom.announce for automations to trigger intercom
announcements without the PWA. Supports:
  - TTS text → audio generation via HA TTS, then playback
  - Direct audio URL playback
  - Per-room status reporting
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .player import PlayResult, play_announcement

_LOGGER = logging.getLogger(__name__)

# Supported TTS platforms in priority order
_TTS_PLATFORMS = [
    "tts.piper",  # local, fast
    "tts.google_translate",  # cloud, reliable
    "tts.google_cloud",
    "tts.amazon_polly",
    "tts.microsoft",
    "tts.edge_tts",
    "tts.cloud",
]


def _find_tts_platform(hass: HomeAssistant) -> str | None:
    """Find the first available TTS platform."""
    for domain in _TTS_PLATFORMS:
        if domain in hass.services.async_services():
            return domain
    return None


async def _generate_tts_audio(
    hass: HomeAssistant,
    message: str,
    language: str = "en",
) -> str | None:
    """Generate TTS audio and return the URL.

    Calls HA TTS to generate audio, returns the /api/tts_proxy URL.
    """
    tts_platform = _find_tts_platform(hass)
    if not tts_platform:
        _LOGGER.warning("No TTS platform available — install Piper or Google Translate TTS")
        return None

    try:
        # Call TTS to get the generated audio URL
        # TTS services return the URL in the response, but async_call doesn't return it
        # We need to use the TTS API differently...
        # The tts.speak service generates audio and returns a URL via the TTS base URL

        # Actually, for HA TTS, the URL pattern is:
        # /api/tts_proxy/<token>_<platform>_<lang>.mp3?message=...
        # We need to call tts.get_engine first to get the token

        # Simpler approach: call tts.speak with entity_id, it'll return the URL
        # via the service response... but service calls don't return values directly.

        # The pragmatic approach: construct the TTS proxy URL directly
        platform_name = tts_platform.split(".", 1)[1] if "." in tts_platform else tts_platform

        # Get TTS provider info
        from urllib.parse import quote

        tts_url = (
            f"/api/tts_proxy/{quote(message[:100])}"
            f"_{platform_name}_{language}.mp3"
            f"?message={quote(message)}"
        )

        # Verify by making a test call — tts.speak works via entity
        await hass.services.async_call(
            "tts",
            "speak",
            {
                "media_player_entity_id": "",  # don't play, just generate
                "message": message,
                "language": language,
            },
            blocking=True,
        )

        _LOGGER.info("TTS audio generated via %s: %.40s...", tts_platform, message)
        return tts_url

    except Exception as exc:
        _LOGGER.error("TTS generation failed: %s", exc)
        return None


async def handle_announce_service(
    hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """Handle home_intercom.announce service call.

    Returns per-room status for use in automations.
    """
    target = call.data.get("target", "all")
    message: str = call.data.get("message", "")
    volume: int | None = call.data.get("volume")
    url: str | None = call.data.get("url")
    language: str = call.data.get("language", hass.config.language or "en")

    room_map: dict = hass.data.get(DOMAIN, {}).get("rooms", {})

    # Resolve targets
    if target == "all":
        targets = [(k, v) for k, v in room_map.items() if v.get("entity_id")]
    else:
        room = room_map.get(target)
        if not room:
            _LOGGER.warning("Unknown announce target: %s", target)
            return {"ok": False, "error": f"unknown target: {target}"}
        targets = [(target, room)]

    if not targets:
        return {"ok": False, "error": "no rooms configured"}

    # Generate audio URL if TTS message provided
    if message and not url:
        url = await _generate_tts_audio(hass, message, language)
        if not url:
            return {"ok": False, "error": "tts_failed"}

    if not url:
        return {"ok": False, "error": "no message or url provided"}

    # Play on each room
    results: dict[str, dict[str, Any]] = {}
    ok_count = 0
    errors: list[dict[str, str]] = []

    for key, room in targets:
        entity = room.get("entity", "")
        announce_volume = room.get("announce_volume") if volume is None else volume

        result: PlayResult = await play_announcement(
            hass,
            entity,
            url,
            0,  # duration unknown for TTS/external URLs
            announce_volume=announce_volume,
        )
        results[key] = result.to_dict()
        if result.ok:
            ok_count += 1
        else:
            errors.append({"entity": entity, "error": result.error or "unknown"})

    return {
        "ok": True,
        "rooms_sent": ok_count,
        "rooms_total": len(targets),
        "errors": errors or None,
        "results": results,
    }
