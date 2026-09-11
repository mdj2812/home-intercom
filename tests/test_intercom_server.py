"""Flask route tests — using app.test_client() to simulate requests."""

import io
import os
import tempfile
from unittest.mock import patch

import pytest

# src in pythonpath via pyproject.toml
from const import WAV_HEADER_SIZE

from device_store import DeviceStore as DockerDeviceStore
from intercom_server import app


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _default_docker_rooms(monkeypatch):
    """Flask tests used to inherit rooms from the bundled seed file."""
    import intercom_server

    monkeypatch.setattr(
        intercom_server,
        "ROOM_MAP",
        {
            "living": {
                "name": "Living Room",
                "entity": "media_player.living_room_speaker",
                "announce_volume": 50,
            },
            "bedroom": {"name": "Bedroom", "entity": "media_player.bedroom_speaker"},
        },
    )


class TestStaticRoutes:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<html" in resp.data

    def test_rooms(self, client):
        resp = client.get("/rooms")
        assert resp.status_code == 200
        data = resp.json
        assert data is not None
        assert "living" in data
        assert data["living"]["name"] == "Living Room"

    def test_rooms_json_removed(self, client):
        resp = client.get("/rooms.json")
        assert resp.status_code == 404

    def test_rooms_ha_alias(self, client):
        """HA-style alias used by ESP32 after /devices/hello (issue #38)."""
        resp = client.get("/api/home_intercom/rooms")
        assert resp.status_code == 200
        data = resp.json
        assert "living" in data
        assert data["living"]["name"] == "Living Room"

    def test_static_icon_192(self, client):
        """icon-192.png served from static/ (symlinked from custom_components/)."""
        resp = client.get("/static/icon-192.png")
        assert resp.status_code == 200
        assert resp.content_length > 0

    def test_audio_file_not_found(self, client):
        resp = client.get("/audio/nonexistent.wav")
        assert resp.status_code == 404


class TestRoomsWrite:
    """PUT/PATCH/DELETE /rooms/<id> (issue #72)."""

    @pytest.fixture
    def rooms_client(self, client, monkeypatch, tmp_path):
        import copy

        import intercom_server

        monkeypatch.setattr(intercom_server, "ROOMS_STORE", str(tmp_path / "rooms.json"))
        monkeypatch.setattr(intercom_server, "ROOM_MAP", copy.deepcopy(intercom_server.ROOM_MAP))
        return client

    def test_put_creates_and_get_reflects(self, rooms_client):
        resp = rooms_client.put(
            "/rooms/office",
            json={"name": "Office", "entity": "media_player.office", "announce_volume": 40},
        )
        assert resp.status_code == 200
        assert resp.json["ok"] is True
        assert resp.json["rooms"]["office"]["entity"] == "media_player.office"
        got = rooms_client.get("/rooms").json
        assert got["office"]["name"] == "Office"

    def test_put_stores_icon(self, rooms_client):
        resp = rooms_client.put(
            "/rooms/office",
            json={"name": "Office", "entity": "media_player.office", "icon": "💻"},
        )
        assert resp.status_code == 200
        assert resp.json["rooms"]["office"]["icon"] == "💻"
        assert rooms_client.get("/rooms").json["office"]["icon"] == "💻"
        bad = rooms_client.put(
            "/rooms/office",
            json={"name": "Office", "entity": "media_player.office", "icon": "🚀"},
        )
        assert bad.status_code == 400

    def test_put_ha_alias(self, rooms_client):
        resp = rooms_client.put(
            "/api/home_intercom/rooms/office",
            json={"name": "Office", "entity_id": "media_player.office"},
        )
        assert resp.status_code == 200
        assert rooms_client.get("/rooms").json["office"]["entity"] == "media_player.office"

    def test_patch_and_delete(self, rooms_client):
        rooms_client.put("/rooms/office", json={"name": "Office", "entity": "media_player.office"})
        patched = rooms_client.patch("/rooms/office", json={"name": "Study", "pause_buffer": 1.5})
        assert patched.status_code == 200
        room = patched.json["rooms"]["office"]
        assert room["name"] == "Study"
        assert room["pause_buffer"] == 1.5
        deleted = rooms_client.delete("/rooms/office")
        assert deleted.status_code == 200
        assert "office" not in deleted.json["rooms"]
        assert rooms_client.delete("/rooms/office").status_code == 404

    def test_delete_does_not_touch_ha_device_registry(self, rooms_client):
        """Docker room delete is rooms.json only — HA registry cleanup lives in api.py."""
        import inspect

        import intercom_server

        src = inspect.getsource(intercom_server.rooms_item)
        assert "device_registry" not in src
        assert "_remove_room_ha_device" not in src
        assert "async_remove_device" not in src

    def test_rejects_reserved_id(self, rooms_client):
        resp = rooms_client.put("/rooms/all", json={"name": "All", "entity": "media_player.x"})
        assert resp.status_code == 400

    def test_patch_unknown(self, rooms_client):
        resp = rooms_client.patch("/rooms/mars", json={"name": "Mars"})
        assert resp.status_code == 404

    def test_persist_error_is_500(self, rooms_client, monkeypatch):
        import intercom_server

        def _boom():
            raise OSError("ebusy")

        monkeypatch.setattr(intercom_server, "_persist_room_map", _boom)
        resp = rooms_client.put(
            "/rooms/office", json={"name": "Office", "entity": "media_player.office"}
        )
        assert resp.status_code == 500
        assert resp.json["error"] == "cannot persist rooms"

    def test_put_order(self, rooms_client, monkeypatch):
        import intercom_server

        monkeypatch.setattr(intercom_server, "ROOM_MAP", {})
        rooms_client.put("/rooms/office", json={"name": "Office", "entity": "media_player.office"})
        rooms_client.put("/rooms/den", json={"name": "Den", "entity": "media_player.den"})
        resp = rooms_client.put("/rooms/order", json={"order": ["den", "office"]})
        assert resp.status_code == 200
        assert list(resp.json["rooms"]) == ["den", "office"]
        assert list(rooms_client.get("/rooms").json) == ["den", "office"]
        alias = rooms_client.put(
            "/api/home_intercom/rooms/order",
            json={"order": ["office", "den"]},
        )
        assert alias.status_code == 200
        assert list(alias.json["rooms"]) == ["office", "den"]

    def test_put_order_rejects_unknown(self, rooms_client):
        resp = rooms_client.put("/rooms/order", json={"order": ["mars"]})
        assert resp.status_code == 400


class TestDevicesRoute:
    """GET /devices + HA alias — read-only registry listing (issue #52)."""

    @pytest.fixture
    def dev_client(self, client, monkeypatch, tmp_path):
        import intercom_server

        store = DockerDeviceStore(str(tmp_path / "device_registry.json"))
        monkeypatch.setattr(intercom_server, "device_store", store)
        return client, store

    def test_devices_empty(self, dev_client):
        client, _ = dev_client
        resp = client.get("/devices")
        assert resp.status_code == 200
        assert resp.json == {}

    def test_devices_lists_registered(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF", "1.0.0")
        resp = client.get("/devices")
        assert resp.status_code == 200
        dev = resp.json["AA:BB:CC:DD:EE:FF"]
        assert dev["name"] == "Device EE:FF"
        assert dev["firmware_version"] == "1.0.0"
        assert dev["buttons"] == {}
        assert dev["pins"] == []

    def test_devices_ha_alias(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF", "1.0.0")
        resp = client.get("/api/home_intercom/devices")
        assert resp.status_code == 200
        assert "AA:BB:CC:DD:EE:FF" in resp.json

    def test_devices_marks_update_from_cache(self, dev_client, monkeypatch, tmp_path):
        import hashlib
        import json

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF", "1.0.0")
        cache = tmp_path / "firmware"
        cache.mkdir()
        blob = b"esp32-bin"
        sha = hashlib.sha256(blob).hexdigest()
        (cache / "firmware.bin").write_bytes(blob)
        (cache / "firmware.json").write_text(json.dumps({"version": "0.2.1", "sha256": sha}))
        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(cache))
        resp = client.get("/devices")
        dev = resp.json["AA:BB:CC:DD:EE:FF"]
        assert dev["firmware_latest"] == "0.2.1"
        assert dev["firmware_update_available"] is True


class TestDevicesApprove:
    """POST /devices/approve — pending → active (issue #51)."""

    @pytest.fixture
    def dev_client(self, client, monkeypatch, tmp_path):
        import intercom_server

        store = DockerDeviceStore(str(tmp_path / "device_registry.json"))
        monkeypatch.setattr(intercom_server, "device_store", store)
        return client, store

    def test_approve_then_listed_not_pending(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        assert store.get("AA:BB:CC:DD:EE:FF")["pending"] is True
        resp = client.post(
            "/devices/approve",
            json={"mac": "AA:BB:CC:DD:EE:FF"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["ok"] is True
        assert store.get("AA:BB:CC:DD:EE:FF")["pending"] is False

    def test_approve_ha_alias(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/api/home_intercom/devices/approve",
            json={"mac": "aa:bb:cc:dd:ee:ff"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert store.get("AA:BB:CC:DD:EE:FF")["pending"] is False

    def test_approve_unknown_404(self, dev_client):
        client, _store = dev_client
        resp = client.post(
            "/devices/approve",
            json={"mac": "AA:BB:CC:DD:EE:FF"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_approve_missing_mac_400(self, dev_client):
        client, _store = dev_client
        resp = client.post("/devices/approve", json={}, content_type="application/json")
        assert resp.status_code == 400


class TestDevicesManage:
    """POST /devices/manage — approve / deapprove / revoke / unrevoke / delete."""

    @pytest.fixture
    def dev_client(self, client, monkeypatch, tmp_path):
        import intercom_server

        store = DockerDeviceStore(str(tmp_path / "device_registry.json"))
        monkeypatch.setattr(intercom_server, "device_store", store)
        return client, store

    def test_deapprove_sets_pending(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "deapprove"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["pending"] is True
        assert store.get("AA:BB:CC:DD:EE:FF")["pending"] is True

    def test_revoke_and_unrevoke(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "revoke"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["revoked"] is True
        resp = client.post(
            "/api/home_intercom/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "unrevoke"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert store.get("AA:BB:CC:DD:EE:FF")["revoked"] is False

    def test_delete_removes_device(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "delete"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["deleted"] is True
        assert store.get("AA:BB:CC:DD:EE:FF") is None

    def test_delete_does_not_touch_ha_device_registry(self, dev_client):
        """Docker delete is store-only — HA registry cleanup lives in api.py."""
        import inspect

        import intercom_server

        src = inspect.getsource(intercom_server.devices_manage)
        assert "device_registry" not in src
        assert "_remove_button_ha_device" not in src
        assert "async_remove_device" not in src

    def test_invalid_action_400(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "explode"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_buttons_updates_map(self, dev_client):
        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        resp = client.post(
            "/devices/manage",
            json={
                "mac": "AA:BB:CC:DD:EE:FF",
                "action": "buttons",
                "buttons": {"4": "living", "5": "mars"},
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["buttons"] == {"4": "living"}
        assert store.get("AA:BB:CC:DD:EE:FF")["buttons"] == {"4": "living"}

    def test_unknown_mac_404(self, dev_client):
        client, _store = dev_client
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "revoke"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_ota_sets_flags(self, dev_client, monkeypatch, tmp_path):
        from firmware import CachedFirmware

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        cached = CachedFirmware(version="0.2.0", sha256="ab", bin_path=str(tmp_path / "fw.bin"))
        monkeypatch.setattr(intercom_server, "ensure_latest_firmware", lambda _dir: cached)
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["target_version"] == "0.2.0"
        device = store.get("AA:BB:CC:DD:EE:FF")
        assert device["ota_requested"] is True
        assert device["ota_target_version"] == "0.2.0"

    def test_ota_cancel_clears_flags(self, dev_client, monkeypatch, tmp_path):
        from firmware import CachedFirmware

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        cached = CachedFirmware(version="0.2.0", sha256="ab", bin_path=str(tmp_path / "fw.bin"))
        monkeypatch.setattr(intercom_server, "ensure_latest_firmware", lambda _dir: cached)
        client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota_cancel"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json["ota_requested"] is False
        device = store.get("AA:BB:CC:DD:EE:FF")
        assert device["ota_requested"] is False
        assert device["ota_target_version"] == ""

    def test_ota_pending_rejected(self, dev_client, monkeypatch):
        from firmware import CachedFirmware

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        monkeypatch.setattr(
            intercom_server,
            "ensure_latest_firmware",
            lambda _dir: CachedFirmware(version="0.2.0", sha256="ab", bin_path="x"),
        )
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.json["error"] == "device pending"

    def test_ota_unknown_404(self, dev_client):
        client, _store = dev_client
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_ota_502_when_fetch_fails(self, dev_client, monkeypatch):
        from firmware import FirmwareError

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")

        def _fail(_dir):
            raise FirmwareError("github down")

        monkeypatch.setattr(intercom_server, "ensure_latest_firmware", _fail)
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        assert resp.status_code == 502

    def test_ota_revoked_rejected(self, dev_client, monkeypatch):
        from firmware import CachedFirmware

        import intercom_server

        client, store = dev_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        store.revoke("AA:BB:CC:DD:EE:FF")
        monkeypatch.setattr(
            intercom_server,
            "ensure_latest_firmware",
            lambda _dir: CachedFirmware(version="0.2.0", sha256="ab", bin_path="x"),
        )
        resp = client.post(
            "/devices/manage",
            json={"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.json["error"] == "device revoked"


class TestFirmwareRoute:
    def test_get_404_when_empty(self, client, monkeypatch, tmp_path):
        import intercom_server

        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(tmp_path / "firmware"))
        resp = client.get("/api/home_intercom/firmware")
        assert resp.status_code == 404

    def test_get_200_with_checksum(self, client, monkeypatch, tmp_path):
        import hashlib
        import json

        import intercom_server

        cache = tmp_path / "firmware"
        cache.mkdir()
        blob = b"esp32-bin"
        sha = hashlib.sha256(blob).hexdigest()
        (cache / "firmware.bin").write_bytes(blob)
        (cache / "firmware.json").write_text(json.dumps({"version": "0.2.0", "sha256": sha}))
        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(cache))
        resp = client.get("/api/home_intercom/firmware")
        assert resp.status_code == 200
        assert resp.data == blob
        assert resp.headers["X-Checksum-SHA256"] == sha

    def test_sig_404_until_published(self, client, monkeypatch, tmp_path):
        import hashlib
        import json

        import intercom_server

        cache = tmp_path / "firmware"
        cache.mkdir()
        blob = b"esp32-bin"
        sha = hashlib.sha256(blob).hexdigest()
        (cache / "firmware.bin").write_bytes(blob)
        (cache / "firmware.json").write_text(json.dumps({"version": "0.2.0", "sha256": sha}))
        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(cache))
        resp = client.get("/api/home_intercom/firmware.sig")
        assert resp.status_code == 404

    def test_sig_200_when_cached(self, client, monkeypatch, tmp_path):
        import hashlib
        import json

        import intercom_server

        cache = tmp_path / "firmware"
        cache.mkdir()
        blob = b"esp32-bin"
        sig = b"s" * 64
        sha = hashlib.sha256(blob).hexdigest()
        (cache / "firmware.bin").write_bytes(blob)
        (cache / "firmware.sig").write_bytes(sig)
        (cache / "firmware.json").write_text(json.dumps({"version": "0.2.0", "sha256": sha}))
        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(cache))
        resp = client.get("/api/home_intercom/firmware.sig")
        assert resp.status_code == 200
        assert resp.data == sig


class TestFirmwareCacheRoutes:
    def test_status_empty(self, client, monkeypatch, tmp_path):
        import intercom_server

        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(tmp_path / "firmware"))
        resp = client.get("/firmware/status")
        assert resp.status_code == 200
        assert resp.json == {"version": ""}
        alias = client.get("/api/home_intercom/firmware/status")
        assert alias.status_code == 200
        assert alias.json == {"version": ""}

    def test_status_cached(self, client, monkeypatch, tmp_path):
        import hashlib
        import json

        import intercom_server

        cache = tmp_path / "firmware"
        cache.mkdir()
        blob = b"esp32-bin"
        sha = hashlib.sha256(blob).hexdigest()
        (cache / "firmware.bin").write_bytes(blob)
        (cache / "firmware.json").write_text(json.dumps({"version": "0.2.0", "sha256": sha}))
        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(cache))
        resp = client.get("/firmware/status")
        assert resp.json == {"version": "0.2.0"}

    def test_sync_ok(self, client, monkeypatch, tmp_path):
        from firmware import CachedFirmware

        import intercom_server

        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(tmp_path / "firmware"))
        monkeypatch.setattr(
            intercom_server,
            "sync_firmware_cache",
            lambda _dir: (
                CachedFirmware(version="0.2.1", sha256="ab", bin_path="x"),
                True,
            ),
        )
        resp = client.post("/firmware/sync")
        assert resp.status_code == 200
        assert resp.json == {"ok": True, "version": "0.2.1", "updated": True}
        alias = client.post("/api/home_intercom/firmware/sync")
        assert alias.status_code == 200
        assert alias.json["updated"] is True

    def test_sync_unavailable(self, client, monkeypatch, tmp_path):
        from firmware import FirmwareError

        import intercom_server

        monkeypatch.setattr(intercom_server, "FIRMWARE_DIR", str(tmp_path / "firmware"))

        def _fail(_dir):
            raise FirmwareError("github down")

        monkeypatch.setattr(intercom_server, "sync_firmware_cache", _fail)
        resp = client.post("/firmware/sync")
        assert resp.status_code == 502
        assert resp.json["error"] == "firmware unavailable"


class TestVersionRoute:
    def test_returns_version_json(self, client):
        resp = client.get("/version")
        assert resp.status_code == 200
        data = resp.json
        assert "version" in data


class TestConfigRoute:
    """GET /config + HA alias — global audio settings (issue #39)."""

    def test_config_fields(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        assert resp.json == {"sample_rate": 16000, "max_record_secs": 60}

    def test_config_ha_alias(self, client):
        resp = client.get("/api/home_intercom/config")
        assert resp.status_code == 200
        assert resp.json == {"sample_rate": 16000, "max_record_secs": 60}


class TestMediaPlayersRoute:
    """GET /media_players + HA alias (issue #73)."""

    def test_returns_catalog(self, client, monkeypatch):
        import intercom_server

        catalog = [{"entity_id": "media_player.study", "name": "Study", "area": ""}]
        monkeypatch.setattr(intercom_server.haclient, "media_player_catalog", lambda: catalog)
        resp = client.get("/media_players")
        assert resp.status_code == 200
        assert resp.json == catalog

    def test_ha_alias(self, client, monkeypatch):
        import intercom_server

        monkeypatch.setattr(intercom_server.haclient, "media_player_catalog", lambda: [])
        resp = client.get("/api/home_intercom/media_players")
        assert resp.status_code == 200
        assert resp.json == []


class TestRoomsStatus:
    def test_returns_500_without_token(self, client, monkeypatch):
        monkeypatch.setattr("intercom_server.HA_TOKEN", "")
        resp = client.get("/rooms/status")
        assert resp.status_code == 500
        data = resp.json
        assert "error" in data


class TestRecordValidation:
    def test_no_target(self, client):
        resp = client.post("/record")
        assert resp.status_code == 400
        data = resp.json
        assert "missing target" in data["error"]

    def test_no_audio_data(self, client):
        resp = client.post("/record?target=living", data=b"")
        assert resp.status_code == 400
        data = resp.json
        assert "no audio" in data["error"].lower()

    def test_unknown_target(self, client):
        resp = client.post("/record?target=mars", data=b"x" * WAV_HEADER_SIZE)
        assert resp.status_code == 400
        data = resp.json
        assert "unknown" in data["error"].lower()

    def test_target_all_with_no_rooms(self, client, monkeypatch):
        import intercom_server

        monkeypatch.setattr(intercom_server, "ROOM_MAP", {})
        resp = client.post("/record?target=all", data=b"x" * WAV_HEADER_SIZE)
        assert resp.status_code == 500
        data = resp.json
        assert "no rooms" in data["error"].lower()


class TestHandleWavPassthrough:
    def test_valid_pcm_wav(self):
        import wave

        from shared import handle_wav_passthrough

        # Create a valid minimal WAV in memory
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)  # 0.5s of silence

        wav_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            rate, duration = handle_wav_passthrough(wav_bytes, tmp_path)
            assert rate == 16000
            assert duration == pytest.approx(0.5, rel=0.01)
            assert os.path.exists(tmp_path)
            assert os.path.getsize(tmp_path) == len(wav_bytes)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestHandlePcmToWav:
    def test_creates_valid_wav(self):
        import wave

        from shared import handle_pcm_to_wav

        # 0.1s of 16-bit mono 16000 Hz PCM
        pcm = b"\x00\x00" * 1600

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            duration = handle_pcm_to_wav(pcm, 16000, tmp_path)
            assert duration == pytest.approx(0.1, rel=0.01)
            assert os.path.exists(tmp_path)

            with wave.open(tmp_path, "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2
                assert wf.getframerate() == 16000
                assert wf.getnframes() == 1600
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestConcatWavs:
    """Test _concat_wavs — prepend chime to audio."""

    def test_duration_is_sum(self, tmp_path):
        """Concat of two waves → duration equals sum of individual durations."""
        import wave

        from shared import concat_wavs

        chime_path = tmp_path / "chime.wav"
        audio_path = tmp_path / "audio.wav"
        output_path = tmp_path / "combined.wav"

        # 0.5s chime: 8000 frames @ 16000 Hz
        with wave.open(str(chime_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)

        # 1.0s audio: 16000 frames @ 16000 Hz
        with wave.open(str(audio_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)

        duration = concat_wavs(str(chime_path), str(audio_path), str(output_path))
        assert duration == pytest.approx(1.5, rel=0.01)
        assert os.path.exists(output_path)

        # Verify combined WAV structure
        with wave.open(str(output_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 24000  # 8000 + 16000

    def test_format_mismatch_skips_chime(self, tmp_path):
        """Mismatched rates → skip chime, duration = audio only."""
        import wave

        from shared import concat_wavs

        chime_path = tmp_path / "chime.wav"
        audio_path = tmp_path / "audio.wav"
        output_path = tmp_path / "combined.wav"

        with wave.open(str(chime_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)

        with wave.open(str(audio_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)  # different rate!
            wf.writeframes(b"\x00\x00" * 44100)

        duration = concat_wavs(str(chime_path), str(audio_path), str(output_path))
        assert duration == pytest.approx(1.0, rel=0.01)


class TestRecordPcmBranch:
    """Test /record with raw PCM (PWA path)."""

    def test_pcm_triggers_wav_writing(self, client, monkeypatch, tmp_path):
        import intercom_server

        # 0.1s of 16-bit mono 16000 Hz PCM
        pcm = b"\x00\x00" * 1600

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))

        with patch.object(intercom_server.haclient, "play_announcement", return_value={"ok": True}):
            resp = client.post("/record?target=living&rate=16000", data=pcm)

        assert resp.status_code == 200
        data = resp.json
        assert data["ok"] is True
        assert data["rooms_sent"] == 1

        # Check WAV file was written
        wav_path = os.path.join(str(tmp_path), "intercom_living.wav")
        assert os.path.exists(wav_path)

        with __import__("wave").open(wav_path, "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getnchannels() == 1


class TestRecordWavPassthroughBranch:
    """Test /record with complete WAV (ESP32 path)."""

    def test_wav_passthrough(self, client, monkeypatch, tmp_path):
        import wave

        import intercom_server

        # Create a valid WAV
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)
        wav_data = buf.getvalue()

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))

        with patch.object(intercom_server.haclient, "play_announcement", return_value={"ok": True}):
            resp = client.post("/record?target=living", data=wav_data)

        assert resp.status_code == 200
        data = resp.json
        assert data["ok"] is True

        wav_path = os.path.join(str(tmp_path), "intercom_living.wav")
        assert os.path.exists(wav_path)
        assert os.path.getsize(wav_path) == len(wav_data)


class TestRecordAnnounceVolume:
    """Verify announce_volume from room config is passed through to play_announcement."""

    def test_ma_announce_volume_passed(self, client, monkeypatch, tmp_path):
        """Room with announce_volume → passed to play_announcement."""
        import intercom_server

        # 0.1s of 16-bit mono 16000 Hz PCM
        pcm = b"\x00\x00" * 1600

        room_with_volume = {
            "living": {"name": "Living", "entity": "media_player.living", "announce_volume": 50},
        }

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))
        monkeypatch.setattr(intercom_server, "ROOM_MAP", room_with_volume)

        with patch.object(
            intercom_server.haclient, "play_announcement", return_value={"ok": True}
        ) as mock_play:
            resp = client.post("/record?target=living&rate=16000", data=pcm)

        assert resp.status_code == 200
        _, kwargs = mock_play.call_args
        assert kwargs.get("announce_volume") == 50
        assert (
            kwargs.get("audio_url_with_chime") == "http://localhost/audio/intercom_living_chime.wav"
        )
        assert kwargs.get("duration_with_chime") is not None

    def test_ma_no_announce_volume_passed(self, client, monkeypatch, tmp_path):
        """Room without announce_volume → volume not passed."""
        import intercom_server

        pcm = b"\x00\x00" * 1600

        room_no_volume = {
            "living": {"name": "Living", "entity": "media_player.living"},
        }

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))
        monkeypatch.setattr(intercom_server, "ROOM_MAP", room_no_volume)

        with patch.object(
            intercom_server.haclient, "play_announcement", return_value={"ok": True}
        ) as mock_play:
            resp = client.post("/record?target=living&rate=16000", data=pcm)

        assert resp.status_code == 200
        _, kwargs = mock_play.call_args
        assert kwargs.get("announce_volume") is None
        assert (
            kwargs.get("audio_url_with_chime") == "http://localhost/audio/intercom_living_chime.wav"
        )


class TestDeviceRecordAuth:
    """MAC-based auth on /record (issue #47) — Docker side."""

    WAV = (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00" + b"\x00" * 64
    )

    @pytest.fixture
    def mac_client(self, client, monkeypatch, tmp_path):
        import intercom_server

        store = DockerDeviceStore(str(tmp_path / "device_registry.json"))
        monkeypatch.setattr(intercom_server, "device_store", store)
        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))
        return client, store

    def _post(self, client, mac=None, path="/record"):
        import intercom_server

        headers = {"X-Device-ID": mac} if mac else {}
        with patch.object(intercom_server.haclient, "play_announcement", return_value={"ok": True}):
            return client.post(f"{path}?target=living", data=self.WAV, headers=headers)

    def test_registered_mac_allowed(self, mac_client):
        client, store = mac_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        resp = self._post(client, "AA:BB:CC:DD:EE:FF")
        assert resp.status_code == 200
        assert resp.json["ok"] is True

    def test_pending_mac_403(self, mac_client):
        client, store = mac_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        resp = self._post(client, "AA:BB:CC:DD:EE:FF")
        assert resp.status_code == 403
        assert resp.json["error"] == "device pending"

    def test_unknown_mac_403(self, mac_client):
        client, _store = mac_client
        resp = self._post(client, "AA:BB:CC:DD:EE:FF")
        assert resp.status_code == 403
        assert resp.json["error"] == "unknown device"

    def test_revoked_mac_403(self, mac_client):
        client, store = mac_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.revoke("AA:BB:CC:DD:EE:FF")
        resp = self._post(client, "AA:BB:CC:DD:EE:FF")
        assert resp.status_code == 403
        assert resp.json["error"] == "device revoked"

    def test_no_header_stays_open_for_pwa(self, mac_client):
        client, _store = mac_client
        resp = self._post(client)
        assert resp.status_code == 200
        assert resp.json["ok"] is True

    @pytest.mark.parametrize(
        "path",
        ["/device/record", "/api/home_intercom/device/record"],
    )
    def test_device_record_alias_registered_mac(self, mac_client, path):
        """Firmware path aliases share /record auth (issue #70)."""
        client, store = mac_client
        store.register_or_update("AA:BB:CC:DD:EE:FF")
        store.approve("AA:BB:CC:DD:EE:FF")
        resp = self._post(client, "AA:BB:CC:DD:EE:FF", path)
        assert resp.status_code == 200
        assert resp.json["ok"] is True

    def test_ha_device_record_alias_unknown_mac_403(self, mac_client):
        client, _store = mac_client
        resp = self._post(client, "AA:BB:CC:DD:EE:FF", "/api/home_intercom/device/record")
        assert resp.status_code == 403
        assert resp.json["error"] == "unknown device"


class TestChimeRoutes:
    """GET/POST/DELETE /chime — Docker custom chime API."""

    WAV = (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00" + b"\x00" * 64
    )

    def test_get_default(self, client, monkeypatch, tmp_path):
        import intercom_server

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))
        resp = client.get("/chime")
        assert resp.status_code == 200
        assert resp.json["custom"] is False

    def test_post_and_delete(self, client, monkeypatch, tmp_path):
        import intercom_server

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))
        resp = client.post("/chime", data=self.WAV)
        assert resp.status_code == 200
        assert resp.json["ok"] is True
        assert "custom_chime.wav" in resp.json["url"]

        resp = client.get("/chime")
        assert resp.json["custom"] is True

        resp = client.delete("/chime")
        assert resp.status_code == 200
        assert resp.json["custom"] is False


class TestParsePauseBuffer:
    def test_default_zero(self):
        from intercom_server import _parse_pause_buffer

        with patch.dict(os.environ, {}, clear=True):
            assert _parse_pause_buffer() == 0.0

    def test_valid_float(self):
        from intercom_server import _parse_pause_buffer

        with patch.dict(os.environ, {"PAUSE_BUFFER": "1.5"}):
            assert _parse_pause_buffer() == 1.5

    def test_valid_int_string(self):
        from intercom_server import _parse_pause_buffer

        with patch.dict(os.environ, {"PAUSE_BUFFER": "2"}):
            assert _parse_pause_buffer() == 2.0

    def test_invalid_returns_zero(self):
        from intercom_server import _parse_pause_buffer

        with patch.dict(os.environ, {"PAUSE_BUFFER": "abc"}):
            assert _parse_pause_buffer() == 0.0

    def test_empty_string_returns_zero(self):
        from intercom_server import _parse_pause_buffer

        with patch.dict(os.environ, {"PAUSE_BUFFER": ""}):
            assert _parse_pause_buffer() == 0.0
