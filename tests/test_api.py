"""Unit tests for Home Assistant API views (RecordView, DeviceRecordView).

Uses mocked web.Request, patched homeassistant module, and pytest-asyncio.
"""

from __future__ import annotations

import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .ha_fakes import install_fake_homeassistant

# ——— Fake homeassistant package before any custom_components imports ———
install_fake_homeassistant()

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

    hass.data = {
        "home_intercom": {
            "rooms": rooms
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
            },
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


# ——— register_api_views tests ———


class TestRegisterApiViews:
    def test_registers_both_record_views(self):
        from custom_components.home_intercom.api import (
            DeviceRecordView,
            RecordView,
            register_api_views,
        )

        hass = MagicMock()
        hass.http.register_view = MagicMock()
        register_api_views(hass)
        calls = [c.args[0] for c in hass.http.register_view.call_args_list]
        assert RecordView in calls
        assert DeviceRecordView in calls


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
