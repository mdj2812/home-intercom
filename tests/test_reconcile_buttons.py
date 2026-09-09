"""Tests for _reconcile_button_devices — HA leftover after PWA delete."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from .ha_fakes import install_fake_homeassistant

install_fake_homeassistant()

import homeassistant.helpers.device_registry as dr  # noqa: E402

from custom_components.home_intercom.__init__ import (  # noqa: E402
    DOMAIN,
    _reconcile_button_devices,
)
from custom_components.home_intercom.api import _remove_button_ha_device  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
MAC2 = "11:22:33:44:55:66"


class FakeDevice:
    def __init__(self, device_id: str, name: str, identifiers: set[tuple[str, str]]):
        self.id = device_id
        self.name = name
        self.identifiers = identifiers


class FakeDeviceRegistry:
    def __init__(self, devices: list[FakeDevice]):
        self.devices = MagicMock()
        self.devices.get_devices_for_config_entry_id = MagicMock(return_value=list(devices))
        self._by_ident = {next(iter(d.identifiers)): d for d in devices if d.identifiers}
        self._removed: list[str] = []
        self.async_remove_device = MagicMock(
            side_effect=lambda device_id: self._removed.append(device_id)
        )

    def async_get_device(self, identifiers=None, connections=None):
        if not identifiers:
            return None
        key = next(iter(identifiers))
        return self._by_ident.get(key)


class FakeStore:
    def __init__(self, macs: set[str]):
        self.devices = {mac: {"name": mac} for mac in macs}


@pytest.fixture
def hass():
    return MagicMock()


def test_removes_deleted_button(hass):
    devices = [
        FakeDevice("d1", "Gone", {(DOMAIN, MAC)}),
        FakeDevice("d2", "Kept", {(DOMAIN, MAC2)}),
    ]
    reg = FakeDeviceRegistry(devices)
    dr.async_get = MagicMock(return_value=reg)

    _reconcile_button_devices(hass, "btn-entry", FakeStore({MAC2}))

    assert "d1" in reg._removed
    assert "d2" not in reg._removed


def test_keeps_all_when_store_matches(hass):
    devices = [
        FakeDevice("d1", "A", {(DOMAIN, MAC)}),
        FakeDevice("d2", "B", {(DOMAIN, MAC2)}),
    ]
    reg = FakeDeviceRegistry(devices)
    dr.async_get = MagicMock(return_value=reg)

    _reconcile_button_devices(hass, "btn-entry", FakeStore({MAC, MAC2}))

    assert reg._removed == []


def test_skips_room_devices(hass):
    devices = [
        FakeDevice("d1", "Living", {(DOMAIN, "living")}),
        FakeDevice("d2", "Button", {(DOMAIN, MAC)}),
    ]
    reg = FakeDeviceRegistry(devices)
    dr.async_get = MagicMock(return_value=reg)

    _reconcile_button_devices(hass, "btn-entry", FakeStore(set()))

    assert "d1" not in reg._removed
    assert "d2" in reg._removed


def test_remove_button_ha_device_by_mac(hass):
    gone = FakeDevice("d1", "Gone", {(DOMAIN, MAC)})
    reg = FakeDeviceRegistry([gone])
    dr.async_get = MagicMock(return_value=reg)

    _remove_button_ha_device(hass, MAC.lower())

    assert "d1" in reg._removed


def test_remove_button_ha_device_noop_when_missing(hass):
    reg = FakeDeviceRegistry([])
    dr.async_get = MagicMock(return_value=reg)

    _remove_button_ha_device(hass, MAC)

    assert reg._removed == []
    reg.async_remove_device.assert_not_called()
