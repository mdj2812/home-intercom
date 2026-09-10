"""Unit tests for room catalog helpers (issue #72)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rooms import (
    RoomValidationError,
    load_rooms,
    patch_room,
    put_room,
    room_entity,
    save_rooms,
    validate_room_key,
)


class TestValidateRoomKey:
    def test_accepts_slug(self) -> None:
        assert validate_room_key("living_room") == "living_room"

    def test_rejects_reserved(self) -> None:
        with pytest.raises(RoomValidationError, match="invalid room id"):
            validate_room_key("all")
        with pytest.raises(RoomValidationError, match="invalid room id"):
            validate_room_key("status")

    def test_rejects_empty(self) -> None:
        with pytest.raises(RoomValidationError, match="invalid room id"):
            validate_room_key(" ")


class TestPutPatch:
    def test_put_docker_entity(self) -> None:
        room = put_room(
            {"name": "Study", "entity": "media_player.study", "announce_volume": 40},
            entity_key="entity",
        )
        assert room == {
            "name": "Study",
            "entity": "media_player.study",
            "announce_volume": 40,
        }

    def test_put_accepts_entity_id_alias(self) -> None:
        room = put_room(
            {"name": "Study", "entity_id": "media_player.study"},
            entity_key="entity",
        )
        assert room["entity"] == "media_player.study"
        assert "entity_id" not in room

    def test_put_omits_zero_optionals(self) -> None:
        room = put_room(
            {
                "name": "Study",
                "entity_id": "media_player.study",
                "announce_volume": 0,
                "pause_buffer": 0,
            },
            entity_key="entity_id",
        )
        assert room == {"name": "Study", "entity_id": "media_player.study"}

    def test_put_requires_name_and_entity(self) -> None:
        with pytest.raises(RoomValidationError, match="missing entity"):
            put_room({"name": "Study"}, entity_key="entity_id")

    def test_patch_clears_volume(self) -> None:
        existing = {
            "name": "Study",
            "entity_id": "media_player.study",
            "announce_volume": 50,
        }
        room = patch_room(existing, {"announce_volume": None}, entity_key="entity_id")
        assert "announce_volume" not in room
        assert room["name"] == "Study"

    def test_patch_empty_rejected(self) -> None:
        with pytest.raises(RoomValidationError, match="empty patch"):
            patch_room({"name": "A", "entity": "media_player.a"}, {}, entity_key="entity")

    def test_room_entity_reads_both_keys(self) -> None:
        assert room_entity({"entity": "media_player.a"}) == "media_player.a"
        assert room_entity({"entity_id": "media_player.b"}) == "media_player.b"


class TestLoadSave:
    def test_seeds_when_store_missing(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.json"
        seed.write_text(json.dumps({"living": {"name": "Living", "entity": "media_player.x"}}))
        store = tmp_path / "data" / "rooms.json"
        rooms = load_rooms(str(store), str(seed))
        assert "living" in rooms
        saved = json.loads(store.read_text())
        assert saved["living"]["name"] == "Living"

    def test_store_wins_over_seed(self, tmp_path: Path) -> None:
        seed = tmp_path / "seed.json"
        seed.write_text(json.dumps({"old": {"name": "Old", "entity": "media_player.a"}}))
        store = tmp_path / "rooms.json"
        save_rooms(str(store), {"new": {"name": "New", "entity": "media_player.b"}})
        rooms = load_rooms(str(store), str(seed))
        assert list(rooms) == ["new"]
