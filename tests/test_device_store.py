"""Tests for the device registry stores (issue #40).

Covers both implementations:
- src/device_store.py        — Docker side (sync, JSON file)
- custom_components device_store.py — HA side (async, .storage via FakeStore)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import device_store as docker_module
from device_store import DeviceStore as DockerDeviceStore

from .ha_fakes import FakeStore, install_fake_homeassistant

install_fake_homeassistant()

from custom_components.home_intercom.device_store import (  # noqa: E402
    DeviceStore as HADeviceStore,
)

MAC = "AA:BB:CC:DD:EE:FF"
MAC2 = "11:22:33:44:55:66"


def _fresh_docker_store(tmp_path) -> DockerDeviceStore:
    return DockerDeviceStore(str(tmp_path / "device_registry.json"))


# ——— Docker side (sync, JSON file) ———


class TestDockerDeviceStore:
    def test_register_new_device_defaults(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        device = store.register_or_update(MAC, "1.0.0")
        assert device["name"] == "Device EE:FF"
        assert device["room"] == ""
        assert device["firmware_version"] == "1.0.0"
        assert device["revoked"] is False
        assert device["created_at"]
        assert device["last_seen"]

    def test_register_normalizes_mac(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update("aa:bb:cc:dd:ee:ff")
        assert store.get(MAC) is not None
        assert store.get(MAC.lower()) is not None

    def test_register_invalid_mac_raises(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        with pytest.raises(ValueError):
            store.register_or_update("not-a-mac")
        with pytest.raises(ValueError):
            store.register_or_update("")

    def test_register_existing_updates_last_seen_and_firmware(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        first = store.register_or_update(MAC, "1.0.0")
        created = first["created_at"]
        second = store.register_or_update(MAC, "2.0.0")
        assert second["created_at"] == created
        assert second["firmware_version"] == "2.0.0"
        assert len(store.devices) == 1

    def test_register_existing_keeps_firmware_when_empty(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC, "1.0.0")
        device = store.register_or_update(MAC)
        assert device["firmware_version"] == "1.0.0"

    def test_get_unknown_returns_none(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        assert store.get(MAC) is None

    def test_update_field(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC)
        device = store.update_field(MAC, "name", "Study Button")
        assert device["name"] == "Study Button"
        device = store.update_field(MAC, "room", "study")
        assert device["room"] == "study"

    def test_update_field_unknown_mac_returns_none(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        assert store.update_field(MAC, "name", "X") is None

    def test_update_field_rejects_non_updateable(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC)
        with pytest.raises(ValueError):
            store.update_field(MAC, "created_at", "yesterday")

    def test_revoke_flags_not_deletes(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC)
        device = store.revoke(MAC)
        assert device["revoked"] is True
        assert store.get(MAC) is not None  # still in registry

    def test_revoke_unknown_returns_none(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        assert store.revoke(MAC) is None

    def test_persistence_round_trip(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC, "1.0.0")
        store.update_field(MAC, "room", "study")
        store.revoke(MAC2 if store.get(MAC2) else MAC)

        reloaded = _fresh_docker_store(tmp_path)
        device = reloaded.get(MAC)
        assert device is not None
        assert device["room"] == "study"
        assert device["revoked"] is True

    def test_corrupt_json_starts_empty(self, tmp_path):
        path = tmp_path / "device_registry.json"
        path.write_text("{not json", encoding="utf-8")
        store = DockerDeviceStore(str(path))
        assert store.get(MAC) is None
        assert store.devices == {}

    def test_saved_file_format(self, tmp_path):
        path = tmp_path / "device_registry.json"
        store = DockerDeviceStore(str(path))
        store.register_or_update(MAC, "1.0.0")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["version"] == 1
        assert MAC in on_disk["devices"]

    def test_two_devices_independent(self, tmp_path):
        store = _fresh_docker_store(tmp_path)
        store.register_or_update(MAC)
        store.register_or_update(MAC2)
        assert docker_module.default_device_name(MAC2) == "Device 55:66"
        assert len(store.devices) == 2


# ——— HA side (async, .storage via FakeStore) ———


async def _fresh_ha_store() -> HADeviceStore:
    store = HADeviceStore(MagicMock())
    await store.async_load()
    return store


class TestHADeviceStore:
    @pytest.fixture(autouse=True)
    def _reset_fake_store(self):
        FakeStore.reset()

    @pytest.mark.asyncio
    async def test_register_new_device_defaults(self):
        store = await _fresh_ha_store()
        device = await store.register_or_update(MAC, "1.0.0")
        assert device["name"] == "Device EE:FF"
        assert device["room"] == ""
        assert device["firmware_version"] == "1.0.0"
        assert device["revoked"] is False
        assert device["created_at"]
        assert device["last_seen"]

    @pytest.mark.asyncio
    async def test_register_normalizes_mac(self):
        store = await _fresh_ha_store()
        await store.register_or_update("aa:bb:cc:dd:ee:ff")
        assert store.get(MAC) is not None

    @pytest.mark.asyncio
    async def test_register_invalid_mac_raises(self):
        store = await _fresh_ha_store()
        with pytest.raises(ValueError):
            await store.register_or_update("zz:zz:zz:zz:zz:zz")

    @pytest.mark.asyncio
    async def test_register_existing_updates(self):
        store = await _fresh_ha_store()
        first = await store.register_or_update(MAC, "1.0.0")
        second = await store.register_or_update(MAC, "2.0.0")
        assert second["created_at"] == first["created_at"]
        assert second["firmware_version"] == "2.0.0"
        third = await store.register_or_update(MAC)
        assert third["firmware_version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_get_unknown_returns_none(self):
        store = await _fresh_ha_store()
        assert store.get(MAC) is None

    @pytest.mark.asyncio
    async def test_update_field(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        device = await store.update_field(MAC, "name", "Study Button")
        assert device["name"] == "Study Button"
        device = await store.update_field(MAC, "room", "study")
        assert device["room"] == "study"

    @pytest.mark.asyncio
    async def test_update_field_unknown_mac_returns_none(self):
        store = await _fresh_ha_store()
        assert await store.update_field(MAC, "name", "X") is None

    @pytest.mark.asyncio
    async def test_update_field_rejects_non_updateable(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        with pytest.raises(ValueError):
            await store.update_field(MAC, "last_seen", "tomorrow")

    @pytest.mark.asyncio
    async def test_revoke_flags_not_deletes(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        device = await store.revoke(MAC)
        assert device["revoked"] is True
        assert store.get(MAC) is not None

    @pytest.mark.asyncio
    async def test_revoke_unknown_returns_none(self):
        store = await _fresh_ha_store()
        assert await store.revoke(MAC) is None

    @pytest.mark.asyncio
    async def test_persistence_round_trip(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC, "1.0.0")
        await store.update_field(MAC, "room", "study")
        await store.revoke(MAC)

        reloaded = await _fresh_ha_store()  # new instance, same .storage key
        device = reloaded.get(MAC)
        assert device is not None
        assert device["room"] == "study"
        assert device["revoked"] is True

    @pytest.mark.asyncio
    async def test_devices_property_returns_copy(self):
        store = await _fresh_ha_store()
        await store.register_or_update(MAC)
        snapshot = store.devices
        snapshot[MAC]["name"] = "mutated"
        assert store.get(MAC)["name"] == "Device EE:FF"
