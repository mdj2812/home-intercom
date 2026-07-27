"""Tests for _reconcile_yaml_devices — YAML orphan cleanup (issue #63)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from .ha_fakes import install_fake_homeassistant

install_fake_homeassistant()

import homeassistant.helpers.device_registry as dr  # noqa: E402
from custom_components.home_intercom.__init__ import (  # noqa: E402
    DOMAIN,
    _reconcile_yaml_devices,
)


class FakeDevice:
    """Minimal stand-in for a device registry entry."""

    def __init__(self, device_id: str, name: str, identifiers: set[tuple[str, str]]):
        self.id = device_id
        self.name = name
        self.identifiers = identifiers
        self.config_entries = set()


class FakeDeviceRegistry:
    """Fake device registry that tracks get_devices_for_config_entry_id + remove_device."""

    def __init__(self, devices: list[FakeDevice]):
        self.devices = MagicMock()
        self.devices.get_devices_for_config_entry_id = MagicMock(
            return_value=list(devices)
        )
        self._removed: list[str] = []
        self.async_remove_device = MagicMock(
            side_effect=lambda device_id: self._removed.append(device_id)
        )


@pytest.fixture
def registry():
    """Fixture that replaces dr.async_get with a fresh FakeDeviceRegistry each test."""
    return FakeDeviceRegistry


def test_removes_orphaned_room(registry):
    """Device for a room not in current_rooms is removed."""
    devices = [
        FakeDevice("d1", "TV", {(DOMAIN, "tv")}),
        FakeDevice("d2", "客厅", {(DOMAIN, "living")}),
    ]
    reg = FakeDeviceRegistry(devices)

    dr.async_get = MagicMock(return_value=reg)
    hass = MagicMock()

    _reconcile_yaml_devices(hass, "entry_1", {"living", "bedroom"})

    # TV should be removed (not in current_rooms)
    assert "d1" in reg._removed
    # 客厅 should stay
    assert "d2" not in reg._removed


def test_noop_when_all_rooms_match(registry):
    """No devices removed when current_rooms covers everything."""
    devices = [
        FakeDevice("d1", "客厅", {(DOMAIN, "living")}),
        FakeDevice("d2", "主卧", {(DOMAIN, "bedroom")}),
    ]
    reg = FakeDeviceRegistry(devices)

    dr.async_get = MagicMock(return_value=reg)
    hass = MagicMock()

    _reconcile_yaml_devices(hass, "entry_1", {"living", "bedroom"})

    assert len(reg._removed) == 0


def test_skips_non_intercom_devices(registry):
    """Devices from other domains are ignored (only DOMAIN identifiers matter)."""
    devices = [
        FakeDevice("d1", "TV", {(DOMAIN, "tv")}),
        FakeDevice("d2", "其他", {("other_domain", "foo")}),
    ]
    reg = FakeDeviceRegistry(devices)

    dr.async_get = MagicMock(return_value=reg)
    hass = MagicMock()

    _reconcile_yaml_devices(hass, "entry_1", {"living"})

    # TV (home_intercom domain) should be removed
    assert "d1" in reg._removed
    # 其他 (different domain) should be left alone
    assert "d2" not in reg._removed


def test_no_devices_at_all(registry):
    """Empty device list — nothing crashes."""
    reg = FakeDeviceRegistry([])
    dr.async_get = MagicMock(return_value=reg)
    hass = MagicMock()

    _reconcile_yaml_devices(hass, "entry_1", {"living"})

    assert len(reg._removed) == 0
