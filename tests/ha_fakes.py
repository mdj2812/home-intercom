"""Shared fake `homeassistant` package for tests.

All test modules call install_fake_homeassistant() at import time.
Idempotent — later calls simply re-register the same sys.modules entries.
"""

from __future__ import annotations

import copy
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class _FakeFlowBase:
    """Minimal data-entry-flow base — captures results as plain dicts."""

    hass: Any = None

    def async_show_form(
        self, *, step_id, data_schema=None, errors=None, description_placeholders=None, **kw
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders or {},
        }

    def async_show_menu(self, *, step_id, menu_options, **kw):
        return {"type": "menu", "step_id": step_id, "menu_options": list(menu_options)}

    def async_abort(self, *, reason, **kw):
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, *, title, data, **kw):
        return {"type": "create_entry", "title": title, "data": data}


class _FakeConfigFlow(_FakeFlowBase):
    """Accepts and ignores the `domain=...` subclass kwarg."""

    def __init_subclass__(cls, **kwargs):
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)


class FakeStore:
    """Functional stand-in for homeassistant.helpers.storage.Store.

    Persists to a class-level dict so a second instance with the same
    key sees previously saved data (round-trip tests).
    """

    _disk: dict = {}

    def __init__(self, hass, version, key):
        self._version = version
        self._key = key

    async def async_load(self):
        data = self._disk.get(self._key)
        return copy.deepcopy(data) if data is not None else None

    async def async_save(self, data):
        self._disk[self._key] = copy.deepcopy(data)

    @classmethod
    def reset(cls):
        cls._disk = {}


def install_fake_homeassistant() -> None:
    """Register a fake `homeassistant` package hierarchy in sys.modules."""
    _ha = types.ModuleType("homeassistant")
    _ha.const = types.ModuleType("homeassistant.const")
    _ha.config_entries = types.ModuleType("homeassistant.config_entries")
    _ha.exceptions = types.ModuleType("homeassistant.exceptions")
    _ha.core = types.ModuleType("homeassistant.core")
    _ha.setup = types.ModuleType("homeassistant.setup")
    _ha.helpers = types.ModuleType("homeassistant.helpers")
    _ha.helpers.device_registry = MagicMock()
    _ha.helpers.entity_registry = MagicMock()
    _ha.helpers.area_registry = MagicMock()

    _ha.const.CONF_ENTITY_ID = "entity_id"
    _ha.const.CONF_NAME = "name"
    _ha.const.CONF_ROOMS = "rooms"
    _ha.const.CONF_AREA_ID = "area_id"
    _ha.const.CONF_ANNOUNCE_VOLUME = "announce_volume"
    _ha.const.CONF_PAUSE_BUFFER = "pause_buffer"
    _ha.const.ATTR_ENTITY_ID = "entity_id"
    _ha.const.DOMAIN = "home_intercom"
    _ha.const.EVENT_HOMEASSISTANT_START = "home_assistant_start"
    _ha.const.EVENT_HOMEASSISTANT_STOP = "home_assistant_stop"

    _ha.config_entries.ConfigEntry = MagicMock
    _ha.config_entries.ConfigFlow = _FakeConfigFlow
    _ha.config_entries.OptionsFlow = _FakeFlowBase
    _ha.config_entries.SOURCE_IMPORT = "source_import"
    _ha.config_entries.HAS_OPTIONS_FLOW = True

    _ha.data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    _ha.data_entry_flow.FlowResult = dict

    _ha.exceptions.HomeAssistantError = Exception
    _ha.exceptions.ConfigEntryNotReady = Exception

    _ha.core.HomeAssistant = MagicMock
    _ha.core.ServiceCall = MagicMock
    _ha.setup.async_setup_entry = AsyncMock(return_value=True)

    _ha.helpers.config_validation = MagicMock()
    _ha.helpers.typing = types.ModuleType("homeassistant.helpers.typing")
    _ha.helpers.typing.ConfigType = dict
    _ha.helpers.storage = types.ModuleType("homeassistant.helpers.storage")
    _ha.helpers.storage.Store = FakeStore

    sys.modules["homeassistant"] = _ha
    sys.modules["homeassistant.const"] = _ha.const
    sys.modules["homeassistant.config_entries"] = _ha.config_entries
    sys.modules["homeassistant.data_entry_flow"] = _ha.data_entry_flow
    sys.modules["homeassistant.exceptions"] = _ha.exceptions
    sys.modules["homeassistant.core"] = _ha.core
    sys.modules["homeassistant.setup"] = _ha.setup
    sys.modules["homeassistant.helpers"] = _ha.helpers
    sys.modules["homeassistant.helpers.device_registry"] = _ha.helpers.device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = _ha.helpers.entity_registry
    sys.modules["homeassistant.helpers.area_registry"] = _ha.helpers.area_registry
    sys.modules["homeassistant.helpers.config_validation"] = _ha.helpers.config_validation
    sys.modules["homeassistant.helpers.typing"] = _ha.helpers.typing
    sys.modules["homeassistant.helpers.storage"] = _ha.helpers.storage
    sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components.http"] = MagicMock()
    sys.modules["homeassistant.components.http"].HomeAssistantView = type(
        "HomeAssistantView", (), {"requires_auth": False}
    )
    sys.modules["homeassistant.components.http"].KEY_HASS_USER = "hass_user"
