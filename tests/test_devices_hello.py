"""Tests for POST /devices/hello (issue #37) — both deployment targets.

Docker: Flask route in src/intercom_server.py (incl. /api/home_intercom alias).
HA: DevicesHelloView in custom_components/home_intercom/api.py.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from shared import pending_hello_hub

import intercom_server
from device_store import DeviceStore as DockerDeviceStore

from .ha_fakes import FakeStore, install_fake_homeassistant

install_fake_homeassistant()

from custom_components.home_intercom.device_store import (  # noqa: E402
    DeviceStore as HADeviceStore,
)

MAC = "AA:BB:CC:DD:EE:FF"
HELLO_URLS = ["/devices/hello", "/api/home_intercom/devices/hello"]


# ——— Docker side (Flask) ———


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with an isolated device registry."""
    store = DockerDeviceStore(str(tmp_path / "device_registry.json"))
    monkeypatch.setattr(intercom_server, "device_store", store)
    intercom_server.app.config["TESTING"] = True
    with intercom_server.app.test_client() as c:
        c.store = store  # convenience handle for seeding/assertions
        yield c


class TestDockerDevicesHello:
    @pytest.mark.parametrize("url", HELLO_URLS)
    def test_new_device_auto_registers(self, client, url):
        resp = client.post(url, headers={"X-Device-ID": MAC}, json={"firmware_version": "1.0.0"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "pending"
        assert "device_name" not in body
        assert client.store.get(MAC)["firmware_version"] == "1.0.0"
        assert client.store.get(MAC)["pending"] is True

    def test_known_device_returns_binding(self, client):
        client.store.register_or_update(MAC, "1.0.0")
        client.store.approve(MAC)
        client.store.update_field(MAC, "name", "Study Button")
        client.store.update_field(MAC, "room", "study")
        resp = client.post(
            "/devices/hello", headers={"X-Device-ID": MAC}, json={"firmware_version": "2.0.0"}
        )
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["device_name"] == "Study Button"
        assert body["room"] == "study"
        # firmware refreshed
        assert client.store.get(MAC)["firmware_version"] == "2.0.0"

    def test_pending_hello_stays_pending(self, client):
        client.store.register_or_update(MAC)
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC}, json={})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "pending"

    def test_revoked_device_rejected(self, client):
        client.store.register_or_update(MAC)
        client.store.revoke(MAC)
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC}, json={})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "device revoked"

    def test_unrevoked_device_hello_succeeds(self, client):
        """Revoke → un-revoke → hello should succeed."""
        client.store.register_or_update(MAC)
        client.store.approve(MAC)
        client.store.revoke(MAC)
        # un-revoke
        client.store.update_field(MAC, "revoked", False)
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC}, json={})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_missing_header_rejected(self, client):
        resp = client.post("/devices/hello", json={})
        assert resp.status_code == 400
        assert "missing" in resp.get_json()["error"]

    def test_invalid_mac_rejected(self, client):
        resp = client.post("/devices/hello", headers={"X-Device-ID": "not-a-mac"}, json={})
        assert resp.status_code == 400
        assert "invalid" in resp.get_json()["error"]

    def test_lowercase_mac_normalized(self, client):
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC.lower()}, json={})
        assert resp.status_code == 200
        assert client.store.get(MAC) is not None

    def test_empty_body_ok(self, client):
        resp = client.post(
            "/devices/hello", headers={"X-Device-ID": MAC}, content_type="application/json"
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "pending"

    def test_approve_during_held_hello_returns_ok(self, client, monkeypatch):
        """Approve should wake the in-flight hello so the button goes online now."""
        monkeypatch.setenv("HOME_INTERCOM_PENDING_HELLO_WAIT", "2")
        client.store.register_or_update(MAC)
        ready = threading.Event()
        original_wait = pending_hello_hub.wait

        def wrapped_wait(mac, timeout, still_waiting=None):
            ready.set()
            return original_wait(mac, timeout, still_waiting)

        monkeypatch.setattr(pending_hello_hub, "wait", wrapped_wait)

        def approve():
            assert ready.wait(timeout=2)
            client.store.approve(MAC)

        worker = threading.Thread(target=approve)
        worker.start()
        started = time.monotonic()
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC}, json={})
        worker.join(timeout=3)
        elapsed = time.monotonic() - started
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert elapsed < 1.5

    def test_pending_hello_wait_times_out(self, client, monkeypatch):
        monkeypatch.setenv("HOME_INTERCOM_PENDING_HELLO_WAIT", "0.15")
        client.store.register_or_update(MAC)
        started = time.monotonic()
        resp = client.post("/devices/hello", headers={"X-Device-ID": MAC}, json={})
        elapsed = time.monotonic() - started
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "pending"
        assert elapsed >= 0.1


# ——— HA side (HomeAssistantView) ———


def _make_hello_request(mac: str | None, body: dict | None, hass: MagicMock) -> MagicMock:
    req = MagicMock()
    req.headers = {}
    if mac is not None:
        req.headers["X-Device-ID"] = mac
    req.json = AsyncMock(return_value=body if body is not None else {})
    if body is None:
        req.json = AsyncMock(side_effect=json.JSONDecodeError("x", "", 0))
    req.app = {"hass": hass}
    return req


def _make_hass_with_store(store) -> MagicMock:
    hass = MagicMock()
    hass.data = {"home_intercom": {"device_store": store}}
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args, **kw: fn(*args, **kw))
    return hass


async def _fresh_ha_store() -> HADeviceStore:
    store = HADeviceStore(MagicMock())
    await store.async_load()
    return store


class TestHADevicesHelloView:
    @pytest.fixture(autouse=True)
    def _reset_fake_store(self):
        FakeStore.reset()

    async def _post(self, mac=MAC, body=None, hass=None):
        from custom_components.home_intercom.api import DevicesHelloView

        store = await _fresh_ha_store()
        if hass is None:
            hass = _make_hass_with_store(store)
        req = _make_hello_request(mac, body, hass)
        resp = await DevicesHelloView().post(req)
        return resp, json.loads(resp.text), store

    @pytest.mark.asyncio
    async def test_new_device_auto_registers(self):
        resp, body, store = await self._post(body={"firmware_version": "1.0.0"})
        assert resp.status == 200
        assert body["status"] == "pending"
        assert "device_name" not in body
        assert store.get(MAC)["firmware_version"] == "1.0.0"
        assert store.get(MAC)["pending"] is True

    @pytest.mark.asyncio
    async def test_known_device_returns_binding(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        await store.approve(MAC)
        await store.update_field(MAC, "name", "Study Button")
        await store.update_field(MAC, "room", "study")
        hass = _make_hass_with_store(store)
        resp, body, _ = await self._post(body={"firmware_version": "2.0.0"}, hass=hass)
        assert body["device_name"] == "Study Button"
        assert body["room"] == "study"
        assert store.get(MAC)["firmware_version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_revoked_device_rejected(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        await store.revoke(MAC)
        hass = _make_hass_with_store(store)
        resp, body, _ = await self._post(body={}, hass=hass)
        assert resp.status == 403
        assert body["error"] == "device revoked"

    @pytest.mark.asyncio
    async def test_unrevoked_device_hello_succeeds(self):
        """Revoke → un-revoke → hello should succeed."""
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        await store.approve(MAC)
        await store.revoke(MAC)
        # un-revoke
        await store.update_field(MAC, "revoked", False)
        hass = _make_hass_with_store(store)
        resp, body, _ = await self._post(body={}, hass=hass)
        assert resp.status == 200
        assert body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_missing_header_rejected(self):
        resp, body, _ = await self._post(mac=None, body={})
        assert resp.status == 400
        assert "missing" in body["error"]

    @pytest.mark.asyncio
    async def test_invalid_mac_rejected(self):
        resp, body, _ = await self._post(mac="zz:zz:zz:zz:zz:zz", body={})
        assert resp.status == 400
        assert "invalid" in body["error"]

    @pytest.mark.asyncio
    async def test_malformed_json_body_ok(self):
        resp, body, _ = await self._post(body=None)  # req.json raises
        assert resp.status == 200
        assert body["status"] == "pending"

    @pytest.mark.asyncio
    async def test_store_unavailable_500(self):
        hass = MagicMock()
        hass.data = {"home_intercom": {}}
        resp, body, _ = await self._post(body={}, hass=hass)
        assert resp.status == 500

    # ── delete + re-hello round-trip ──

    @pytest.mark.asyncio
    async def test_delete_rehello_registers_entities(self):
        """Delete from device_store → hello → device + entities recreated."""
        store = await _fresh_ha_store()
        await store.register_or_update(MAC, "1.0.0")
        assert store.get(MAC) is not None

        # Simulate HA delete: remove from store entirely
        await store.remove(MAC)
        assert store.get(MAC) is None

        # Re-hello should auto-register as a brand-new device
        hass = _make_hass_with_store(store)
        resp, body, _ = await self._post(body={"firmware_version": "3.0.0"}, hass=hass)
        assert resp.status == 200
        assert body["status"] == "pending"
        assert "device_name" not in body

        # Verify device is back in store
        dev = store.get(MAC)
        assert dev is not None
        assert dev["name"] == "Device EE:FF"
        assert dev["firmware_version"] == "3.0.0"
        assert not dev.get("revoked")
        assert dev.get("pending") is True

    @pytest.mark.asyncio
    async def test_delete_rehello_dispaches_signal(self):
        """After re-hello, dispatcher signal fires for entity creation."""
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        await store.remove(MAC)

        hass = _make_hass_with_store(store)
        resp, body, _ = await self._post(body={}, hass=hass)
        assert resp.status == 200

        # Verify dispatcher was called
        import homeassistant.helpers.dispatcher as disp

        disp.async_dispatcher_send.assert_called_with(hass, "home_intercom_device_store_changed")

    @pytest.mark.asyncio
    async def test_class_attributes(self):
        from custom_components.home_intercom.api import DevicesHelloView

        assert DevicesHelloView.url == "/api/home_intercom/devices/hello"
        assert DevicesHelloView.requires_auth is False
