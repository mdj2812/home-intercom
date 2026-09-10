"""Room catalog helpers shared by HA and Docker (issue #72).

GET /rooms stays a public map. Writes validate the same payload on both
deployments, then persist in the native shape:

- HA config entries: ``name`` + ``entity_id``
- Docker ``/data/rooms.json``: ``name`` + ``entity``
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import Any, Literal

try:
    from .const import CONF_ANNOUNCE_VOLUME, CONF_PAUSE_BUFFER
except ImportError:
    from const import CONF_ANNOUNCE_VOLUME, CONF_PAUSE_BUFFER

_LOGGER = logging.getLogger(__name__)

ROOM_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
ENTITY_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
RESERVED_ROOM_KEYS = frozenset({"all", "status"})
MAX_ROOM_NAME_LEN = 64
MAX_PAUSE_BUFFER = 10.0

EntityKey = Literal["entity", "entity_id"]


class RoomValidationError(ValueError):
    """Invalid room key or body. ``str(exc)`` is safe to return to the client."""


def validate_room_key(room_id: str) -> str:
    """Return a trimmed room key or raise RoomValidationError."""
    key = (room_id or "").strip()
    if not key or not ROOM_KEY_RE.match(key) or key.lower() in RESERVED_ROOM_KEYS:
        raise RoomValidationError("invalid room id")
    return key


def room_entity(room: dict[str, Any]) -> str:
    """Media player id from either Docker (``entity``) or HA (``entity_id``)."""
    return str(room.get("entity_id") or room.get("entity") or "").strip()


def _parse_name(value: Any) -> str:
    if not isinstance(value, str):
        raise RoomValidationError("invalid name")
    name = value.strip()
    if not name or len(name) > MAX_ROOM_NAME_LEN:
        raise RoomValidationError("invalid name")
    return name


def _parse_entity(body: dict[str, Any]) -> str | None:
    if "entity_id" not in body and "entity" not in body:
        return None
    raw = body.get("entity_id", body.get("entity"))
    if not isinstance(raw, str):
        raise RoomValidationError("invalid entity")
    entity = raw.strip().lower()
    if not ENTITY_RE.match(entity):
        raise RoomValidationError("invalid entity")
    return entity


def _parse_announce_volume(value: Any) -> int | None:
    """Return 1–100, or None to omit/clear. 0 and null clear."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RoomValidationError("invalid announce_volume")
    volume = int(value)
    if volume == 0:
        return None
    if volume < 1 or volume > 100:
        raise RoomValidationError("invalid announce_volume")
    return volume


def _parse_pause_buffer(value: Any) -> float | None:
    """Return >0 seconds, or None to omit/clear. 0 and null clear."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RoomValidationError("invalid pause_buffer")
    buf = float(value)
    if buf == 0:
        return None
    if buf < 0 or buf > MAX_PAUSE_BUFFER:
        raise RoomValidationError("invalid pause_buffer")
    return buf


def _apply_optional(room: dict[str, Any], body: dict[str, Any], *, replace: bool) -> None:
    if replace or CONF_ANNOUNCE_VOLUME in body:
        volume = _parse_announce_volume(body.get(CONF_ANNOUNCE_VOLUME))
        if volume is None:
            room.pop(CONF_ANNOUNCE_VOLUME, None)
        else:
            room[CONF_ANNOUNCE_VOLUME] = volume
    if replace or CONF_PAUSE_BUFFER in body:
        buf = _parse_pause_buffer(body.get(CONF_PAUSE_BUFFER))
        if buf is None:
            room.pop(CONF_PAUSE_BUFFER, None)
        else:
            room[CONF_PAUSE_BUFFER] = buf


def put_room(body: Any, *, entity_key: EntityKey) -> dict[str, Any]:
    """Build a full room record for PUT (create or replace)."""
    if not isinstance(body, dict):
        raise RoomValidationError("invalid body")
    name = _parse_name(body.get("name"))
    entity = _parse_entity(body)
    if entity is None:
        raise RoomValidationError("missing entity")
    room: dict[str, Any] = {"name": name, entity_key: entity}
    _apply_optional(room, body, replace=True)
    return room


def patch_room(existing: dict[str, Any], body: Any, *, entity_key: EntityKey) -> dict[str, Any]:
    """Merge PATCH fields into an existing room."""
    if not isinstance(body, dict):
        raise RoomValidationError("invalid body")
    known = {"name", "entity", "entity_id", CONF_ANNOUNCE_VOLUME, CONF_PAUSE_BUFFER}
    if not any(key in body for key in known):
        raise RoomValidationError("empty patch")
    room = dict(existing)
    if "name" in body:
        room["name"] = _parse_name(body.get("name"))
    entity = _parse_entity(body)
    if entity is not None:
        other = "entity_id" if entity_key == "entity" else "entity"
        room.pop(other, None)
        room[entity_key] = entity
    _apply_optional(room, body, replace=False)
    return room


def load_rooms(store_path: str, seed_path: str) -> dict[str, Any]:
    """Load the writable store, seeding from the bundled file when missing."""
    if os.path.isfile(store_path):
        rooms = _read_rooms_file(store_path)
        _LOGGER.info("rooms loaded from %s (%d)", store_path, len(rooms))
        return rooms
    rooms = _read_rooms_file(seed_path) if os.path.isfile(seed_path) else {}
    try:
        save_rooms(store_path, rooms)
        _LOGGER.info("rooms seeded %s → %s (%d)", seed_path, store_path, len(rooms))
    except OSError as exc:
        _LOGGER.warning("could not seed rooms store %s: %s", store_path, exc)
    return rooms


def save_rooms(path: str, rooms: dict[str, Any]) -> None:
    """Persist rooms JSON. Falls back to in-place write if os.replace fails."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(rooms, indent=2, ensure_ascii=False) + "\n"
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def _read_rooms_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _LOGGER.error("Failed to load rooms %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}
