"""Unit tests for Home Assistant API views (RecordView, DeviceRecordView).

Uses mocked web.Request, patched homeassistant module, and pytest-asyncio.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .ha_fakes import install_fake_homeassistant

# ——— Fake homeassistant package before any custom_components imports ———
install_fake_homeassistant()

from custom_components.home_intercom.const import CONF_ROOMS, UI_UNIQUE_ID  # noqa: E402

# ——— Test data ———

WAV_DATA = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00" + b"\x00" * 1024
)
PCM_DATA = b"\x00" * 1024
PWA_TOKEN = "test-shared-secret-abc123"


# ——— Helpers ———


def _make_request(target: str = "living_room", data: bytes | None = None) -> MagicMock:
    """Build a mock web.Request with default query params and body."""
    req = MagicMock()
    req.read = AsyncMock(return_value=data or WAV_DATA)
    req.query = MagicMock()
    req.query.__getitem__.side_effect = lambda k: {"target": target, "rate": "16000"}[k]
    req.query.get = lambda k, default=None: {"target": target, "rate": "16000"}.get(k, default)
    req.remote = "192.168.1.100"
    req.host = "homeassistant.local:8123"
    req.scheme = "http"
    req.headers = {"Host": "homeassistant.local:8123"}
    # For aiohttp web.json_response to work in tests, we need a real loop
    return req


def _make_hass(rooms: dict | None = None) -> MagicMock:
    """Build a mock HA instance with integration data."""
    hass = MagicMock()
    hass.config.external_url = None
    hass.config.internal_url = "http://192.168.1.10:8123"

    audio_dir = tempfile.mkdtemp(prefix="hi_test_audio_")

    room_map = (
        rooms
        if rooms is not None
        else {
            "living_room": {
                "name": "Living Room",
                "entity_id": "media_player.living_speaker",
                "announce_volume": 50,
            },
            "bedroom": {
                "name": "Bedroom",
                "entity_id": "media_player.bedroom_speaker",
            },
        }
    )
    rooms_copy = {key: dict(value) for key, value in room_map.items()}
    ui_entry = MagicMock()
    ui_entry.unique_id = UI_UNIQUE_ID
    ui_entry.entry_id = "ui-entry"
    ui_entry.data = {CONF_ROOMS: {key: dict(value) for key, value in rooms_copy.items()}}
    ui_entry.options = {}

    def _update_entry(entry, **kwargs):
        if "options" in kwargs:
            entry.options = kwargs["options"]

    hass.config_entries.async_entries.return_value = [ui_entry]
    hass.config_entries.async_update_entry.side_effect = _update_entry
    hass.data = {
        "home_intercom": {
            "rooms": rooms_copy,
            "entry_rooms": {"ui-entry": {key: dict(value) for key, value in rooms_copy.items()}},
            "audio_dir": audio_dir,
            "pwa_token": PWA_TOKEN,
        },
    }
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args, **kw: fn(*args, **kw))
    return hass


# ——— _handle_record tests ———


class TestHandleRecord:
    """Tests for _handle_record() — the core audio processing function."""

    @pytest.mark.asyncio
    async def test_missing_target(self):
        from custom_components.home_intercom.api import _handle_record

        req = _make_request(target="")
        req.app = {"hass": _make_hass()}
        resp = await _handle_record(req)
        body = json.loads(resp.text)
        assert resp.status == 400, f"got {body}"
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_unknown_target(self):
        from custom_components.home_intercom.api import _handle_record

        req = _make_request(target="nonexistent")
        req.app = {"hass": _make_hass()}
        resp = await _handle_record(req)
        body = json.loads(resp.text)
        assert resp.status == 400
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_no_audio_data(self):
        from custom_components.home_intercom.api import _handle_record

        req = _make_request(target="living_room", data=b"short")
        req.app = {"hass": _make_hass()}
        resp = await _handle_record(req)
        body = json.loads(resp.text)
        assert resp.status == 400
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_target_all_no_rooms(self):
        from custom_components.home_intercom.api import _handle_record

        req = _make_request(target="all")
        req.app = {"hass": _make_hass(rooms={})}
        resp = await _handle_record(req)
        body = json.loads(resp.text)
        assert resp.status == 500, body
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_single_room_wav(self):
        from custom_components.home_intercom.api import _handle_record

        with patch(
            "custom_components.home_intercom.api.play_announcement",
            new=AsyncMock(return_value=MagicMock(ok=True, error=None)),
        ):
            req = _make_request(target="living_room", data=WAV_DATA)
            req.app = {"hass": _make_hass()}
            resp = await _handle_record(req)
            body = json.loads(resp.text)
            assert resp.status == 200, body
            assert body["ok"] is True
            assert body["name"] == "Living Room"
            assert body["rooms_sent"] == 1

    @pytest.mark.asyncio
    async def test_broadcast_all(self):
        from custom_components.home_intercom.api import _handle_record

        with patch(
            "custom_components.home_intercom.api.play_announcement",
            new=AsyncMock(return_value=MagicMock(ok=True, error=None)),
        ):
            req = _make_request(target="all", data=WAV_DATA)
            req.app = {"hass": _make_hass()}
            resp = await _handle_record(req)
            body = json.loads(resp.text)
            assert resp.status == 200, body
            assert body["name"] == "All Rooms"

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        from custom_components.home_intercom.api import _handle_record

        successes = iter([True, False])

        async def mock_play(*args, **kwargs):
            ok = next(successes)
            return MagicMock(ok=ok, error=None if ok else "timeout")

        with patch("custom_components.home_intercom.api.play_announcement", new=mock_play):
            req = _make_request(target="all", data=WAV_DATA)
            req.app = {"hass": _make_hass()}
            resp = await _handle_record(req)
            body = json.loads(resp.text)
            assert resp.status == 200
            assert body["rooms_sent"] == 1
            assert body["rooms_total"] == 2


# ——— RecordView tests ———


class TestRecordView:
    @pytest.mark.asyncio
    async def test_unauthorized_missing_token(self):
        from custom_components.home_intercom.api import RecordView

        view = RecordView()
        req = _make_request()
        req.app = {"hass": _make_hass()}
        req.headers = {}
        resp = await view.post(req)
        body = json.loads(resp.text)
        assert resp.status == 401, body
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_authorized_success(self):
        from custom_components.home_intercom.api import RecordView

        view = RecordView()
        req = _make_request()
        req.app = {"hass": _make_hass()}
        req.headers = {"X-PWA-Token": PWA_TOKEN}

        with patch(
            "custom_components.home_intercom.api._handle_record",
            new=AsyncMock(
                return_value=MagicMock(
                    status=200,
                    text='{"ok": true, "name": "Living Room"}',
                )
            ),
        ):
            resp = await view.post(req)
            body = json.loads(resp.text)
            assert body["ok"] is True


# ——— DeviceRecordView tests ———


class TestDeviceRecordView:
    def test_class_attributes(self):
        from custom_components.home_intercom.api import DeviceRecordView

        assert DeviceRecordView.url == "/api/home_intercom/device/record"
        assert DeviceRecordView.name == "api:home_intercom:device-record"
        assert DeviceRecordView.requires_auth is False  # dual auth: MAC or HA user

    @pytest.mark.asyncio
    async def test_post_delegates_to_handle_record(self):
        from custom_components.home_intercom.api import DeviceRecordView

        view = DeviceRecordView()
        req = _make_request()
        req.app = {"hass": _make_hass()}
        # HA-authenticated request: auth middleware attached a user
        req.get = lambda k, d=None: MagicMock() if k == "hass_user" else d

        with patch(
            "custom_components.home_intercom.api._handle_record",
            new=AsyncMock(
                return_value=MagicMock(
                    status=200,
                    text='{"ok": true, "name": "Living Room"}',
                )
            ),
        ):
            resp = await view.post(req)
            body = json.loads(resp.text)
        assert body["ok"] is True


class TestDeviceRecordViewMacAuth:
    """MAC-based auth on DeviceRecordView (issue #47)."""

    def _req_with_mac(self, mac: str, hass: MagicMock) -> MagicMock:
        req = _make_request()
        req.app = {"hass": hass}
        req.headers = {"X-Device-ID": mac}
        req.get = lambda k, d=None: d  # no hass_user attached
        return req

    def _hass_with_device(self, device: dict | None) -> MagicMock:
        hass = _make_hass()
        store = MagicMock()
        store.get = lambda mac: device
        hass.data["home_intercom"]["device_store"] = store
        return hass

    def _ok_response(self):
        return AsyncMock(return_value=MagicMock(status=200, text='{"ok": true}'))

    @pytest.mark.asyncio
    async def test_registered_mac_delegates(self):
        from custom_components.home_intercom.api import DeviceRecordView

        device = {"name": "Study Button", "room": "study", "revoked": False}
        req = self._req_with_mac("AA:BB:CC:DD:EE:FF", self._hass_with_device(device))
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 200
        mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_mac_403(self):
        from custom_components.home_intercom.api import DeviceRecordView

        req = self._req_with_mac("AA:BB:CC:DD:EE:FF", self._hass_with_device(None))
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 403
        assert json.loads(resp.text)["error"] == "unknown device"
        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoked_mac_403(self):
        from custom_components.home_intercom.api import DeviceRecordView

        device = {"name": "Study Button", "room": "study", "revoked": True}
        req = self._req_with_mac("AA:BB:CC:DD:EE:FF", self._hass_with_device(device))
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 403
        assert json.loads(resp.text)["error"] == "device revoked"
        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_mac_403(self):
        from custom_components.home_intercom.api import DeviceRecordView

        device = {"name": "Study Button", "room": "study", "revoked": False, "pending": True}
        req = self._req_with_mac("AA:BB:CC:DD:EE:FF", self._hass_with_device(device))
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 403
        assert json.loads(resp.text)["error"] == "device pending"
        mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrevoked_mac_record_succeeds(self):
        """Revoke → un-revoke → record should succeed."""
        from custom_components.home_intercom.api import DeviceRecordView

        device = {"name": "Study Button", "room": "study", "revoked": False}
        req = self._req_with_mac("AA:BB:CC:DD:EE:FF", self._hass_with_device(device))
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 200
        mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_mac_no_user_401(self):
        from custom_components.home_intercom.api import DeviceRecordView

        req = _make_request()
        req.app = {"hass": _make_hass()}
        req.get = lambda k, d=None: d  # unauthenticated
        with patch(
            "custom_components.home_intercom.api._handle_record", new=self._ok_response()
        ) as mock_handle:
            resp = await DeviceRecordView().post(req)
        assert resp.status == 401
        mock_handle.assert_not_called()


# ——— ConfigView tests (issue #39) ———


class TestConfigView:
    """GET /api/home_intercom/config — public global audio settings."""

    def test_class_attributes(self):
        from custom_components.home_intercom.api import ConfigView

        assert ConfigView.url == "/api/home_intercom/config"
        assert ConfigView.requires_auth is False  # ESP32 holds zero secrets

    @pytest.mark.asyncio
    async def test_returns_audio_settings(self):
        from custom_components.home_intercom.api import ConfigView

        req = _make_request()
        req.app = {"hass": _make_hass()}
        resp = await ConfigView().get(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["sample_rate"] == 16000
        assert body["max_record_secs"] == 60


# ——— DevicesView tests (issue #52) ———


class TestDevicesView:
    """GET /api/home_intercom/devices — PWA-token-gated read-only listing."""

    def _req(self, token: str | None, store: MagicMock | None) -> MagicMock:
        req = _make_request()
        hass = _make_hass()
        if store is not None:
            hass.data["home_intercom"]["device_store"] = store
        req.app = {"hass": hass}
        req.headers = {"X-PWA-Token": token} if token else {}
        return req

    def _store_with_device(self) -> MagicMock:
        store = MagicMock()
        store.devices = {
            "AA:BB:CC:DD:EE:FF": {
                "name": "Device EE:FF",
                "room": "living_room",
                "last_seen": "2026-07-24T08:00:00",
                "firmware_version": "1.0.0",
                "revoked": False,
            }
        }
        return store

    @pytest.mark.asyncio
    async def test_returns_devices_with_valid_token(self):
        from custom_components.home_intercom.api import DevicesView

        req = self._req(PWA_TOKEN, self._store_with_device())
        resp = await DevicesView().get(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["AA:BB:CC:DD:EE:FF"]["name"] == "Device EE:FF"
        assert body["AA:BB:CC:DD:EE:FF"]["room"] == "living_room"
        assert "firmware_update_available" not in body["AA:BB:CC:DD:EE:FF"]

    @pytest.mark.asyncio
    async def test_marks_update_available_from_firmware_cache(self):
        from custom_components.home_intercom.api import DevicesView

        req = self._req(PWA_TOKEN, self._store_with_device())
        hass = req.app["hass"]
        _seed_firmware_cache(
            Path(hass.data["home_intercom"]["audio_dir"]) / "firmware", b"esp32-bin"
        )
        resp = await DevicesView().get(req)
        body = json.loads(resp.text)
        dev = body["AA:BB:CC:DD:EE:FF"]
        assert dev["firmware_latest"] == "0.2.0"
        assert dev["firmware_update_available"] is True
        assert dev["firmware_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_rejects_missing_or_wrong_token(self):
        from custom_components.home_intercom.api import DevicesView

        for bad in (None, "wrong-token"):
            req = self._req(bad, self._store_with_device())
            resp = await DevicesView().get(req)
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_empty_without_store(self):
        from custom_components.home_intercom.api import DevicesView

        req = self._req(PWA_TOKEN, None)
        resp = await DevicesView().get(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {}


class TestDevicesApproveView:
    """POST /api/home_intercom/devices/approve (issue #51)."""

    def _req(
        self, token: str | None, store: MagicMock | None, body: dict | None = None
    ) -> MagicMock:
        req = _make_request()
        hass = _make_hass()
        if store is not None:
            hass.data["home_intercom"]["device_store"] = store
        req.app = {"hass": hass}
        req.headers = {"X-PWA-Token": token} if token else {}
        req.json = AsyncMock(
            return_value=body if body is not None else {"mac": "AA:BB:CC:DD:EE:FF"}
        )
        return req

    @pytest.mark.asyncio
    async def test_approve_with_valid_token(self):
        from custom_components.home_intercom.api import DevicesApproveView

        store = MagicMock()
        store.approve = AsyncMock(return_value={"pending": False, "name": "Device EE:FF"})
        req = self._req(PWA_TOKEN, store)
        resp = await DevicesApproveView().post(req)
        assert resp.status == 200
        assert json.loads(resp.text)["ok"] is True
        store.approve.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self):
        from custom_components.home_intercom.api import DevicesApproveView

        store = MagicMock()
        store.approve = AsyncMock()
        req = self._req(None, store)
        resp = await DevicesApproveView().post(req)
        assert resp.status == 401
        store.approve.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_mac_404(self):
        from custom_components.home_intercom.api import DevicesApproveView

        store = MagicMock()
        store.approve = AsyncMock(return_value=None)
        req = self._req(PWA_TOKEN, store)
        resp = await DevicesApproveView().post(req)
        assert resp.status == 404


class TestDevicesManageView:
    """POST /api/home_intercom/devices/manage."""

    def _req(self, token: str | None, store: MagicMock | None, body: dict | None) -> MagicMock:
        req = _make_request()
        hass = _make_hass()
        if store is not None:
            hass.data["home_intercom"]["device_store"] = store
        req.app = {"hass": hass}
        req.headers = {"X-PWA-Token": token} if token else {}
        req.json = AsyncMock(return_value=body if body is not None else {})
        return req

    @pytest.mark.asyncio
    async def test_revoke_with_valid_token(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.revoke = AsyncMock(return_value={"pending": False, "revoked": True})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "revoke"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 200
        assert json.loads(resp.text)["revoked"] is True
        store.revoke.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")

    @pytest.mark.asyncio
    async def test_delete_notifies_store_changed(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.get = MagicMock(return_value={"name": "Device EE:FF"})
        store.remove = AsyncMock()
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "delete"})
        with patch("custom_components.home_intercom.api._remove_button_ha_device") as remove_ha:
            resp = await DevicesManageView().post(req)
        assert resp.status == 200
        assert json.loads(resp.text)["deleted"] is True
        store.remove.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")
        remove_ha.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        req = self._req(None, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "revoke"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_action_400(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "nope"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_buttons_updates_map(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.update_field = AsyncMock(return_value={"buttons": {"4": "living_room"}})
        req = self._req(
            PWA_TOKEN,
            store,
            {
                "mac": "AA:BB:CC:DD:EE:FF",
                "action": "buttons",
                "buttons": {"4": "living_room", "5": "mars"},
            },
        )
        resp = await DevicesManageView().post(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["buttons"] == {"4": "living_room"}
        store.update_field.assert_awaited_once()
        args = store.update_field.await_args.args
        assert args[0] == "AA:BB:CC:DD:EE:FF"
        assert args[1] == "buttons"
        assert args[2] == {"4": "living_room"}

    @pytest.mark.asyncio
    async def test_ota_sets_flags(self):
        from custom_components.home_intercom.api import DevicesManageView
        from custom_components.home_intercom.firmware import CachedFirmware

        store = MagicMock()
        store.get = MagicMock(return_value={"pending": False, "revoked": False})
        store.request_ota = AsyncMock(return_value={"ota_requested": True})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"})
        cached = CachedFirmware(version="0.2.0", sha256="ab", bin_path="/tmp/fw.bin")
        with patch(
            "custom_components.home_intercom.api.ensure_latest_firmware", return_value=cached
        ):
            resp = await DevicesManageView().post(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["target_version"] == "0.2.0"
        store.request_ota.assert_awaited_once_with("AA:BB:CC:DD:EE:FF", "0.2.0")

    @pytest.mark.asyncio
    async def test_ota_cancel_clears_flags(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.cancel_ota = AsyncMock(return_value={"ota_requested": False})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota_cancel"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["ota_requested"] is False
        store.cancel_ota.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")

    @pytest.mark.asyncio
    async def test_ota_unknown_mac_404(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.get = MagicMock(return_value=None)
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_ota_pending_400(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.get = MagicMock(return_value={"pending": True, "revoked": False})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 400
        assert json.loads(resp.text)["error"] == "device pending"

    @pytest.mark.asyncio
    async def test_ota_revoked_400(self):
        from custom_components.home_intercom.api import DevicesManageView

        store = MagicMock()
        store.get = MagicMock(return_value={"pending": False, "revoked": True})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"})
        resp = await DevicesManageView().post(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_ota_github_failure_502(self):
        from custom_components.home_intercom.api import DevicesManageView
        from custom_components.home_intercom.firmware import FirmwareError

        store = MagicMock()
        store.get = MagicMock(return_value={"pending": False, "revoked": False})
        req = self._req(PWA_TOKEN, store, {"mac": "AA:BB:CC:DD:EE:FF", "action": "ota"})
        with patch(
            "custom_components.home_intercom.api.ensure_latest_firmware",
            side_effect=FirmwareError("github down"),
        ):
            resp = await DevicesManageView().post(req)
        assert resp.status == 502


# ——— register_api_views tests ———


class TestRegisterApiViews:
    def test_registers_both_record_views(self):
        from custom_components.home_intercom.api import (
            ChimeView,
            DeviceRecordView,
            DevicesApproveView,
            DevicesManageView,
            FirmwareSigView,
            FirmwareStatusView,
            FirmwareSyncView,
            FirmwareView,
            MediaPlayersView,
            PanelAliasView,
            PanelView,
            RecordView,
            RoomsItemView,
            RoomsOrderView,
            StaticAliasView,
            StaticView,
            register_api_views,
        )

        hass = MagicMock()
        hass.http.register_view = MagicMock()
        register_api_views(hass)
        calls = [c.args[0] for c in hass.http.register_view.call_args_list]
        assert RecordView in calls
        assert MediaPlayersView in calls
        assert RoomsItemView in calls
        assert RoomsOrderView in calls
        assert ChimeView in calls
        assert DeviceRecordView in calls
        assert DevicesApproveView in calls
        assert DevicesManageView in calls
        assert FirmwareView in calls
        assert FirmwareStatusView in calls
        assert FirmwareSyncView in calls
        assert FirmwareSigView in calls
        assert PanelView in calls
        assert PanelAliasView in calls
        assert StaticView in calls
        assert StaticAliasView in calls


# ——— PanelView tests ———


class TestPanelViews:
    def test_class_attributes(self):
        from custom_components.home_intercom.api import PanelAliasView, PanelView
        from custom_components.home_intercom.const import PANEL_PATH, PANEL_PATH_LEGACY

        assert PanelView.url == PANEL_PATH_LEGACY
        assert PanelAliasView.url == PANEL_PATH

    @pytest.mark.asyncio
    async def test_legacy_path_rewrites_static_urls(self):
        from custom_components.home_intercom.api import PanelView

        req = MagicMock()
        req.path = "/home_intercom"
        req.app = {"hass": _make_hass()}
        resp = await PanelView().get(req)
        assert resp.status == 200
        assert 'src="/home_intercom/static/' in resp.text
        assert 'href="/home_intercom/static/' in resp.text
        assert f'window._PWA_TOKEN="{PWA_TOKEN}"' in resp.text

    @pytest.mark.asyncio
    async def test_hyphen_path_rewrites_static_urls(self):
        from custom_components.home_intercom.api import PanelAliasView

        req = MagicMock()
        req.path = "/home-intercom"
        req.app = {"hass": _make_hass()}
        resp = await PanelAliasView().get(req)
        assert resp.status == 200
        assert 'src="/home-intercom/static/' in resp.text
        assert 'href="/home-intercom/static/' in resp.text
        assert "/home_intercom/static/" not in resp.text

    @pytest.mark.asyncio
    async def test_static_alias_serves_asset(self):
        from custom_components.home_intercom.api import StaticAliasView

        req = MagicMock()
        req.app = {"hass": _make_hass()}
        resp = await StaticAliasView().get(req, "intercom.css")
        assert resp.status == 200
        assert resp.content_type == "text/css"


# ——— RoomsView tests (issue #38) ———


class TestRoomsView:
    """GET /api/home_intercom/rooms — ESP32 calls this right after /devices/hello."""

    def test_class_attributes(self):
        from custom_components.home_intercom.api import RoomsView

        assert RoomsView.url == "/api/home_intercom/rooms"
        # Public: ESP32 holds zero secrets, so this endpoint must not require auth.
        assert RoomsView.requires_auth is False

    @pytest.mark.asyncio
    async def test_get_returns_room_map(self):
        from custom_components.home_intercom.api import RoomsView

        req = _make_request()
        req.app = {"hass": _make_hass()}
        resp = await RoomsView().get(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        # ESP32 needs room keys; display names optional but present.
        assert set(body) == {"living_room", "bedroom"}
        assert body["living_room"]["name"] == "Living Room"

    @pytest.mark.asyncio
    async def test_get_empty_when_no_rooms(self):
        from custom_components.home_intercom.api import RoomsView

        req = _make_request()
        req.app = {"hass": _make_hass(rooms={})}
        resp = await RoomsView().get(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {}


class TestMediaPlayersView:
    """GET /api/home_intercom/media_players (issue #73)."""

    def test_class_attributes(self):
        from custom_components.home_intercom.api import MediaPlayersView

        assert MediaPlayersView.url == "/api/home_intercom/media_players"
        assert MediaPlayersView.requires_auth is False

    @pytest.mark.asyncio
    async def test_get_returns_catalog(self):
        from custom_components.home_intercom.api import MediaPlayersView

        catalog = [{"entity_id": "media_player.study", "name": "Study", "area": "Study"}]
        req = _make_request()
        req.app = {"hass": _make_hass()}
        with patch(
            "custom_components.home_intercom.api.media_player_catalog", return_value=catalog
        ):
            resp = await MediaPlayersView().get(req)
        assert resp.status == 200
        assert json.loads(resp.text) == catalog

    def test_catalog_filters_and_area(self):
        from custom_components.home_intercom.media_players import media_player_catalog
        from custom_components.home_intercom.rooms import PLAY_MEDIA

        play = MagicMock()
        play.entity_id = "media_player.kitchen"
        play.attributes = {"friendly_name": "Kitchen Speaker", "supported_features": PLAY_MEDIA}
        skip = MagicMock()
        skip.entity_id = "media_player.dead"
        skip.attributes = {"friendly_name": "Dead", "supported_features": 0}
        hass = MagicMock()
        hass.states.async_all.return_value = [play, skip]
        ent = MagicMock()
        ent.area_id = "kitchen"
        er_reg = MagicMock()
        er_reg.async_get.return_value = ent
        area = MagicMock()
        area.name = "Kitchen"
        ar_reg = MagicMock()
        ar_reg.async_get_area.return_value = area
        with (
            patch("homeassistant.helpers.entity_registry.async_get", return_value=er_reg),
            patch("homeassistant.helpers.area_registry.async_get", return_value=ar_reg),
        ):
            catalog = media_player_catalog(hass)
        assert catalog == [
            {"entity_id": "media_player.kitchen", "name": "Kitchen Speaker", "area": "Kitchen"}
        ]

    def test_catalog_empty_area_and_entity_id_fallback(self):
        from custom_components.home_intercom.media_players import media_player_catalog
        from custom_components.home_intercom.rooms import PLAY_MEDIA

        player = MagicMock()
        player.entity_id = "media_player.study"
        player.attributes = {"supported_features": PLAY_MEDIA}
        hass = MagicMock()
        hass.states.async_all.return_value = [player]
        er_reg = MagicMock()
        er_reg.async_get.return_value = None
        ar_reg = MagicMock()
        with (
            patch("homeassistant.helpers.entity_registry.async_get", return_value=er_reg),
            patch("homeassistant.helpers.area_registry.async_get", return_value=ar_reg),
        ):
            catalog = media_player_catalog(hass)
        assert catalog == [
            {"entity_id": "media_player.study", "name": "media_player.study", "area": ""}
        ]


class TestRoomsItemView:
    """PUT/PATCH/DELETE /api/home_intercom/rooms/{id} (issue #72)."""

    def _req(
        self,
        token: str | None,
        body: dict | None = None,
        *,
        hass: MagicMock | None = None,
    ) -> MagicMock:
        req = _make_request()
        req.app = {"hass": hass or _make_hass()}
        req.headers = {"X-PWA-Token": token} if token else {}
        req.json = AsyncMock(return_value=body if body is not None else {})
        return req

    @pytest.mark.asyncio
    async def test_put_creates_room(self):
        from custom_components.home_intercom.api import RoomsItemView, RoomsView

        hass = _make_hass()
        req = self._req(
            PWA_TOKEN,
            {"name": "Study", "entity_id": "media_player.study"},
            hass=hass,
        )
        resp = await RoomsItemView().put(req, "study")
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["rooms"]["study"]["entity_id"] == "media_player.study"
        get_req = _make_request()
        get_req.app = {"hass": hass}
        got = json.loads((await RoomsView().get(get_req)).text)
        assert "study" in got

    @pytest.mark.asyncio
    async def test_patch_updates_name(self):
        from custom_components.home_intercom.api import RoomsItemView

        hass = _make_hass()
        req = self._req(PWA_TOKEN, {"name": "Lounge"}, hass=hass)
        resp = await RoomsItemView().patch(req, "living_room")
        assert resp.status == 200
        room = json.loads(resp.text)["rooms"]["living_room"]
        assert room["name"] == "Lounge"
        assert room["entity_id"] == "media_player.living_speaker"

    @pytest.mark.asyncio
    async def test_delete_removes_room(self):
        from custom_components.home_intercom.api import RoomsItemView

        hass = _make_hass()
        req = self._req(PWA_TOKEN, hass=hass)
        with patch("custom_components.home_intercom.api._remove_room_ha_device") as remove_ha:
            resp = await RoomsItemView().delete(req, "bedroom")
        assert resp.status == 200
        assert "bedroom" not in json.loads(resp.text)["rooms"]
        remove_ha.assert_called_once_with(hass, "bedroom", "ui-entry")

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self):
        from custom_components.home_intercom.api import RoomsItemView

        req = self._req(None, {"name": "Study", "entity": "media_player.study"})
        resp = await RoomsItemView().put(req, "study")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_room_id(self):
        from custom_components.home_intercom.api import RoomsItemView

        req = self._req(PWA_TOKEN, {"name": "X", "entity_id": "media_player.x"})
        resp = await RoomsItemView().put(req, "all")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_no_ui_entry_conflict(self):
        from custom_components.home_intercom.api import RoomsItemView
        from custom_components.home_intercom.const import YAML_UNIQUE_ID

        hass = _make_hass()
        yaml_entry = MagicMock()
        yaml_entry.unique_id = YAML_UNIQUE_ID
        yaml_entry.entry_id = "yaml-entry"
        hass.config_entries.async_entries.return_value = [yaml_entry]
        req = self._req(PWA_TOKEN, {"name": "Study", "entity_id": "media_player.study"}, hass=hass)
        resp = await RoomsItemView().put(req, "study")
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "no writable config entry"

    @pytest.mark.asyncio
    async def test_delete_unknown_room_404(self):
        from custom_components.home_intercom.api import RoomsItemView

        hass = _make_hass(rooms={})
        ui_entry = hass.config_entries.async_entries.return_value[0]
        ui_entry.data = {CONF_ROOMS: {}}
        hass.data["home_intercom"]["entry_rooms"] = {"ui-entry": {}}
        hass.data["home_intercom"]["rooms"] = {}
        req = self._req(PWA_TOKEN, hass=hass)
        resp = await RoomsItemView().delete(req, "study")
        assert resp.status == 404
        assert json.loads(resp.text)["error"] == "unknown room"


class TestRoomsOrderView:
    """PUT /api/home_intercom/rooms/order (issue #76)."""

    def _req(self, token: str, body: dict, *, hass: MagicMock | None = None) -> MagicMock:
        req = _make_request()
        req.app = {"hass": hass or _make_hass()}
        req.headers = {"X-PWA-Token": token}
        req.json = AsyncMock(return_value=body)
        return req

    @pytest.mark.asyncio
    async def test_put_reorders_keys(self):
        from custom_components.home_intercom.api import RoomsOrderView

        hass = _make_hass()
        req = self._req(PWA_TOKEN, {"order": ["bedroom", "living_room"]}, hass=hass)
        resp = await RoomsOrderView().put(req)
        assert resp.status == 200
        rooms = json.loads(resp.text)["rooms"]
        assert list(rooms) == ["bedroom", "living_room"]

    @pytest.mark.asyncio
    async def test_put_rejects_partial_list(self):
        from custom_components.home_intercom.api import RoomsOrderView

        hass = _make_hass()
        req = self._req(PWA_TOKEN, {"order": ["bedroom"]}, hass=hass)
        resp = await RoomsOrderView().put(req)
        assert resp.status == 400
        assert json.loads(resp.text)["error"] == "invalid order"


# ——— ChimeView tests (issue #66) ———


class TestChimeView:
    @pytest.mark.asyncio
    async def test_get_default(self):
        from custom_components.home_intercom.api import ChimeView
        from custom_components.home_intercom.const import DEFAULT_CHIME_STATIC_URL

        req = _make_request()
        req.app = {"hass": _make_hass()}
        resp = await ChimeView().get(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["custom"] is False
        assert body["url"] == DEFAULT_CHIME_STATIC_URL

    @pytest.mark.asyncio
    async def test_post_upload(self):
        from custom_components.home_intercom.api import ChimeView

        req = _make_request(data=WAV_DATA)
        req.headers = {"X-PWA-Token": PWA_TOKEN}
        req.app = {"hass": _make_hass()}
        resp = await ChimeView().post(req)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert "custom_chime.wav" in body["url"]

    @pytest.mark.asyncio
    async def test_post_unauthorized(self):
        from custom_components.home_intercom.api import ChimeView

        req = _make_request(data=WAV_DATA)
        req.headers = {}
        req.app = {"hass": _make_hass()}
        resp = await ChimeView().post(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_delete_reset(self):
        from custom_components.home_intercom.api import ChimeView

        hass = _make_hass()
        post_req = _make_request(data=WAV_DATA)
        post_req.headers = {"X-PWA-Token": PWA_TOKEN}
        post_req.app = {"hass": hass}
        await ChimeView().post(post_req)

        del_req = _make_request()
        del_req.headers = {"X-PWA-Token": PWA_TOKEN}
        del_req.app = {"hass": hass}
        resp = await ChimeView().delete(del_req)
        assert resp.status == 200
        assert json.loads(resp.text)["custom"] is False


# ——— FirmwareView tests ———


def _seed_firmware_cache(cache_dir: Path, blob: bytes, *, sig: bytes | None = None) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(blob).hexdigest()
    (cache_dir / "firmware.bin").write_bytes(blob)
    (cache_dir / "firmware.json").write_text(
        json.dumps({"version": "0.2.0", "sha256": sha}), encoding="utf-8"
    )
    if sig is not None:
        (cache_dir / "firmware.sig").write_bytes(sig)
    return sha


class TestFirmwareView:
    @pytest.mark.asyncio
    async def test_get_404_when_empty(self):
        from custom_components.home_intercom.api import FirmwareView

        req = _make_request()
        req.app = {"hass": _make_hass()}
        resp = await FirmwareView().get(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_200_with_checksum(self):
        from custom_components.home_intercom.api import FirmwareView

        hass = _make_hass()
        blob = b"esp32-bin"
        sha = _seed_firmware_cache(Path(hass.data["home_intercom"]["audio_dir"]) / "firmware", blob)
        req = _make_request()
        req.app = {"hass": hass}
        resp = await FirmwareView().get(req)
        assert resp.status == 200
        assert resp.body == blob
        assert resp.headers["X-Checksum-SHA256"] == sha

    @pytest.mark.asyncio
    async def test_sig_404_until_published(self):
        from custom_components.home_intercom.api import FirmwareSigView

        hass = _make_hass()
        _seed_firmware_cache(Path(hass.data["home_intercom"]["audio_dir"]) / "firmware", b"bin")
        req = _make_request()
        req.app = {"hass": hass}
        resp = await FirmwareSigView().get(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_sig_200_when_cached(self):
        from custom_components.home_intercom.api import FirmwareSigView

        hass = _make_hass()
        sig = b"s" * 64
        _seed_firmware_cache(
            Path(hass.data["home_intercom"]["audio_dir"]) / "firmware", b"bin", sig=sig
        )
        req = _make_request()
        req.app = {"hass": hass}
        resp = await FirmwareSigView().get(req)
        assert resp.status == 200
        assert resp.body == sig


class TestFirmwareCacheViews:
    @pytest.mark.asyncio
    async def test_status_empty(self):
        from custom_components.home_intercom.api import FirmwareStatusView

        req = _make_request()
        req.app = {"hass": _make_hass()}
        resp = await FirmwareStatusView().get(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {"version": ""}

    @pytest.mark.asyncio
    async def test_status_cached(self):
        from custom_components.home_intercom.api import FirmwareStatusView

        hass = _make_hass()
        _seed_firmware_cache(Path(hass.data["home_intercom"]["audio_dir"]) / "firmware", b"bin")
        req = _make_request()
        req.app = {"hass": hass}
        resp = await FirmwareStatusView().get(req)
        assert json.loads(resp.text) == {"version": "0.2.0"}

    @pytest.mark.asyncio
    async def test_sync_ok(self):
        from custom_components.home_intercom.api import FirmwareSyncView
        from custom_components.home_intercom.firmware import CachedFirmware

        hass = _make_hass()
        req = _make_request()
        req.headers = {"X-PWA-Token": PWA_TOKEN}
        req.app = {"hass": hass}
        cached = CachedFirmware(version="0.2.1", sha256="ab", bin_path="x")
        with patch(
            "custom_components.home_intercom.api.sync_firmware_cache",
            return_value=(cached, True),
        ):
            resp = await FirmwareSyncView().post(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {"ok": True, "version": "0.2.1", "updated": True}

    @pytest.mark.asyncio
    async def test_sync_unauthorized(self):
        from custom_components.home_intercom.api import FirmwareSyncView

        req = _make_request()
        req.headers = {}
        req.app = {"hass": _make_hass()}
        resp = await FirmwareSyncView().post(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_sync_unavailable(self):
        from custom_components.home_intercom.api import FirmwareSyncView
        from custom_components.home_intercom.firmware import FirmwareError

        hass = _make_hass()
        req = _make_request()
        req.headers = {"X-PWA-Token": PWA_TOKEN}
        req.app = {"hass": hass}
        with patch(
            "custom_components.home_intercom.api.sync_firmware_cache",
            side_effect=FirmwareError("github down"),
        ):
            resp = await FirmwareSyncView().post(req)
        assert resp.status == 502
        assert json.loads(resp.text)["error"] == "firmware unavailable"
