"""Options flow tests — room menu + device management UI (issue #48)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from .ha_fakes import FakeStore, install_fake_homeassistant

install_fake_homeassistant()

from custom_components.home_intercom.config_flow import (  # noqa: E402
    HomeIntercomOptionsFlow,
)
from custom_components.home_intercom.const import (  # noqa: E402
    CONF_ROOMS,
    DOMAIN,
    YAML_UNIQUE_ID,
)
from custom_components.home_intercom.device_store import (  # noqa: E402
    DeviceStore as HADeviceStore,
)

MAC = "AA:BB:CC:DD:EE:FF"
ROOMS = {
    "living": {"name": "Living Room", "entity_id": "media_player.living_speaker"},
    "bedroom": {"name": "Bedroom", "entity_id": "media_player.bedroom_speaker"},
}


def _entry(unique_id: str = "ui-entry-1", rooms: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.data = {CONF_ROOMS: rooms if rooms is not None else dict(ROOMS)}
    entry.options = {}
    return entry


async def _make_store(hass: MagicMock) -> HADeviceStore:
    store = HADeviceStore(hass)
    await store.async_load()
    return store


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    return hass


def _flow(entry: MagicMock, hass: MagicMock) -> HomeIntercomOptionsFlow:
    flow = HomeIntercomOptionsFlow(entry)
    flow.hass = hass
    return flow


@pytest.fixture(autouse=True)
def _reset_fake_store():
    FakeStore.reset()
    yield
    FakeStore.reset()


class TestInitMenu:
    @pytest.mark.asyncio
    async def test_ui_entry_gets_menu(self):
        flow = _flow(_entry(), _hass())
        result = await flow.async_step_init()
        assert result["type"] == "menu"
        assert result["step_id"] == "init"
        assert result["menu_options"] == ["rooms", "devices"]

    @pytest.mark.asyncio
    async def test_yaml_entry_skips_to_devices(self):
        """YAML entries: rooms read-only, but device registry is runtime state."""
        hass = _hass()
        store = await _make_store(hass)
        hass.data[DOMAIN]["device_store"] = store
        await store.register_or_update(MAC, "1.0.0")

        flow = _flow(_entry(unique_id=YAML_UNIQUE_ID), hass)
        result = await flow.async_step_init()
        # lands on the device picker, not the yaml_read_only abort
        assert result["type"] == "form"
        assert result["step_id"] == "devices"

    @pytest.mark.asyncio
    async def test_yaml_entry_no_devices_aborts(self):
        hass = _hass()
        hass.data[DOMAIN]["device_store"] = await _make_store(hass)
        flow = _flow(_entry(unique_id=YAML_UNIQUE_ID), hass)
        result = await flow.async_step_init()
        assert result == {"type": "abort", "reason": "no_devices"}


class TestDevicesStep:
    async def _setup(self, registered: list[tuple[str, str]] | None = None):
        hass = _hass()
        store = await _make_store(hass)
        hass.data[DOMAIN]["device_store"] = store
        for mac, fw in registered or []:
            await store.register_or_update(mac, fw)
        return hass, store

    @pytest.mark.asyncio
    async def test_empty_registry_aborts(self):
        hass, _ = await self._setup()
        flow = _flow(_entry(), hass)
        result = await flow.async_step_devices()
        assert result == {"type": "abort", "reason": "no_devices"}

    @pytest.mark.asyncio
    async def test_lists_devices_sorted_by_name(self):
        hass, _ = await self._setup([(MAC, "1.0.0"), ("11:22:33:44:55:66", "0.9")])
        store = hass.data[DOMAIN]["device_store"]
        await store.update_field("11:22:33:44:55:66", "name", "AAA First")

        flow = _flow(_entry(), hass)
        result = await flow.async_step_devices()
        assert result["type"] == "form"
        assert result["step_id"] == "devices"
        assert result["description_placeholders"]["device_count"] == "2"
        schema = result["data_schema"].schema
        choice_validator = schema[next(iter(schema))]
        keys = list(choice_validator.container)
        # "AAA First" sorts before "Device EE:FF"
        assert keys == ["11:22:33:44:55:66", MAC]
        assert "AAA First" in choice_validator.container["11:22:33:44:55:66"]

    @pytest.mark.asyncio
    async def test_revoked_device_marked(self):
        hass, store = await self._setup([(MAC, "1.0.0")])
        await store.revoke(MAC)

        flow = _flow(_entry(), hass)
        result = await flow.async_step_devices()
        schema = result["data_schema"].schema
        label = schema[next(iter(schema))].container[MAC]
        assert label.startswith("🚫")
        assert MAC in label

    @pytest.mark.asyncio
    async def test_select_goes_to_edit(self):
        hass, _ = await self._setup([(MAC, "1.0.0")])
        flow = _flow(_entry(), hass)
        result = await flow.async_step_devices({"device_choice": MAC})
        assert result["type"] == "form"
        assert result["step_id"] == "device_edit"
        assert flow._device_mac == MAC


class TestDeviceEditStep:
    async def _setup(self):
        hass = _hass()
        store = await _make_store(hass)
        hass.data[DOMAIN]["device_store"] = store
        await store.register_or_update(MAC, "1.2.3")
        flow = _flow(_entry(), hass)
        flow._device_mac = MAC
        return hass, store, flow

    @staticmethod
    def _fields(form_result) -> dict:
        return {str(k): k for k in form_result["data_schema"].schema}

    @pytest.mark.asyncio
    async def test_form_prefills_current_values(self):
        _, store, flow = await self._setup()
        await store.update_field(MAC, "room", "living")

        result = await flow.async_step_device_edit()
        assert result["type"] == "form"
        ph = result["description_placeholders"]
        assert ph["mac"] == MAC
        assert ph["firmware"] == "1.2.3"
        assert "last_seen" in ph

        fields = self._fields(result)
        name_key = fields["name"]
        room_key = fields["room"]
        revoked_key = fields["revoked"]
        assert name_key.default() == "Device EE:FF"
        assert room_key.default() == "living"
        assert revoked_key.default() is False
        # room choices: unassigned marker + all rooms
        room_choices = result["data_schema"].schema[room_key].container
        assert set(room_choices) == {"", "living", "bedroom"}

    @pytest.mark.asyncio
    async def test_unknown_bound_room_kept_as_choice(self):
        """Device bound to a since-deleted room must still render the form."""
        _, store, flow = await self._setup()
        await store.update_field(MAC, "room", "garage")  # not in ROOMS

        result = await flow.async_step_device_edit()
        room_key = self._fields(result)["room"]
        room_choices = result["data_schema"].schema[room_key].container
        assert "garage" in room_choices
        assert room_key.default() == "garage"

    @pytest.mark.asyncio
    async def test_rename_and_rebind(self):
        _, store, flow = await self._setup()
        result = await flow.async_step_device_edit(
            {"name": "Kitchen Button", "room": "bedroom", "revoked": False}
        )
        assert result["type"] == "create_entry"
        device = store.get(MAC)
        assert device["name"] == "Kitchen Button"
        assert device["room"] == "bedroom"
        # rooms preserved in options (create_entry replaces options wholesale)
        assert result["data"][CONF_ROOMS].keys() == ROOMS.keys()

    @pytest.mark.asyncio
    async def test_blank_name_keeps_current(self):
        _, store, flow = await self._setup()
        await flow.async_step_device_edit({"name": "   ", "room": "", "revoked": False})
        assert store.get(MAC)["name"] == "Device EE:FF"

    @pytest.mark.asyncio
    async def test_revoke_and_unrevoke(self):
        _, store, flow = await self._setup()
        await flow.async_step_device_edit({"name": "Device EE:FF", "room": "", "revoked": True})
        assert store.get(MAC)["revoked"] is True

        # un-revoke via the same form
        await flow.async_step_device_edit({"name": "Device EE:FF", "room": "", "revoked": False})
        assert store.get(MAC)["revoked"] is False

    @pytest.mark.asyncio
    async def test_device_gone_aborts(self):
        hass, store, flow = await self._setup()
        store._devices.clear()
        result = await flow.async_step_device_edit()
        assert result == {"type": "abort", "reason": "device_not_found"}
