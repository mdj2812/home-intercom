"""Announce service for Home Intercom.

Exposes home_intercom.announce for automations to trigger intercom
announcements without the PWA. Supports:
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


async def handle_announce_service(
    hass: HomeAssistant,
    call: ServiceCall,
) -> dict[str, Any]:
    """Handle home_intercom.announce service call.

    Returns per-room status for use in automations.
    """
    target = call.data.get("target", "all")
    url: str | None = call.data.get("url")
    volume: int | None = call.data.get("volume")

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

    if not url:
        return {"ok": False, "error": "no url provided"}

    # Play on each room
    results: dict[str, dict[str, Any]] = {}
    ok_count = 0
    errors: list[dict[str, str]] = []

    for key, room in targets:
        entity = room.get("entity_id", "")
        announce_volume = room.get("announce_volume") if volume is None else volume
        pause_buffer = room.get("pause_buffer", 0.0)

        result: PlayResult = await play_announcement(
            hass,
            entity,
            url,
            0,  # duration unknown for external URLs
            announce_volume=announce_volume,
            pause_buffer=pause_buffer,
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
