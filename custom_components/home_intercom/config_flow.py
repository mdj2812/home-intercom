"""Config flow for Home Intercom integration.

Replaces manual rooms.json editing with a HA config flow UI.
Users can add, edit, and remove rooms via the HA UI.

Flow:
  1. Add Room → enter key, name, entity_id, optional announce_volume
  2. Options → same form for editing existing rooms
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Schema for a single room
ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("key", default=""): cv.string,
        vol.Required(CONF_NAME, default=""): cv.string,
        vol.Required(CONF_ENTITY_ID): EntitySelector(
            EntitySelectorConfig(domain="media_player")
        ),
        vol.Optional("announce_volume", default=50): NumberSelector(
            NumberSelectorConfig(min=1, max=100, step=5, mode=NumberSelectorMode.SLIDER)
        ),
    }
)

# Schema for room management (list of rooms)
ROOM_LIST_SCHEMA = vol.Schema({})


class HomeIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Home Intercom."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._rooms: dict[str, dict[str, Any]] = {}
        self._editing_key: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — configure rooms."""
        if user_input is not None:
            return self.async_create_entry(
                title="Home Intercom",
                data={"rooms": user_input.get("rooms", {})},
            )

        # Show the room management UI
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional("rooms_text"): TextSelector(
                    TextSelectorConfig(
                        multiline=True,
                        type=TextSelectorType.TEXT,
                    )
                ),
            }),
            description_placeholders={
                "example": (
                    "living:\n"
                    "  name: Living Room\n"
                    "  entity: media_player.living_room_speaker\n"
                    "  announce_volume: 50\n"
                    "bedroom:\n"
                    "  name: Bedroom\n"
                    "  entity: media_player.bedroom_speaker"
                ),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return HomeIntercomOptionsFlow(config_entry)


class HomeIntercomOptionsFlow(OptionsFlow):
    """Handle options flow for Home Intercom — edit rooms."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._rooms: dict[str, dict[str, Any]] = dict(
            config_entry.data.get("rooms", {})
        )
        self._current_room: dict[str, Any] | None = None
        self._current_key: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage rooms — add, edit, or remove."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                return await self.async_step_add_room()
            elif action == "edit":
                self._current_key = user_input.get("edit_key", "")
                room = self._rooms.get(self._current_key, {})
                self._current_room = dict(room)
                return await self.async_step_edit_room()
            elif action == "remove":
                key = user_input.get("remove_key", "")
                self._rooms.pop(key, None)
                return await self._save_and_finish()
            elif action == "done":
                return await self._save_and_finish()

        # Build current rooms display
        rooms_display = ""
        for key, room in self._rooms.items():
            entity = room.get("entity", "")
            name = room.get("name", "")
            vol_str = f", vol={room['announce_volume']}" if "announce_volume" in room else ""
            rooms_display += f"  {key}: {name} → {entity}{vol_str}\n"

        options = ["add", "done"]
        room_keys = list(self._rooms.keys())
        if room_keys:
            options = ["add", "edit", "remove"] + options

        schema_dict: dict[vol.Required | vol.Optional, Any] = {
            vol.Required("action", default="add"): vol.In(options),
        }

        if room_keys:
            schema_dict[vol.Optional("edit_key", default=room_keys[0])] = vol.In(room_keys)
            schema_dict[vol.Optional("remove_key", default=room_keys[0])] = vol.In(room_keys)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"rooms": rooms_display or "(no rooms configured)"},
        )

    async def async_step_add_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a new room."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input.get("key", "").strip()
            if not key:
                errors["key"] = "key_required"
            elif key in self._rooms and self._current_key != key:
                errors["key"] = "key_exists"
            else:
                self._rooms[key] = {
                    "name": user_input.get(CONF_NAME, key),
                    "entity": user_input.get(CONF_ENTITY_ID, ""),
                }
                vol_val = user_input.get("announce_volume")
                if vol_val is not None:
                    self._rooms[key]["announce_volume"] = int(vol_val)
                return await self.async_step_init({})

        return self.async_show_form(
            step_id="add_room",
            data_schema=ROOM_SCHEMA,
            errors=errors,
        )

    async def async_step_edit_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit an existing room."""
        errors: dict[str, str] = {}
        if user_input is not None and self._current_key:
            new_key = user_input.get("key", "").strip()
            if not new_key:
                errors["key"] = "key_required"
            elif new_key != self._current_key and new_key in self._rooms:
                errors["key"] = "key_exists"
            else:
                # Remove old key, add new
                self._rooms.pop(self._current_key, None)
                self._rooms[new_key] = {
                    "name": user_input.get(CONF_NAME, new_key),
                    "entity": user_input.get(CONF_ENTITY_ID, ""),
                }
                vol_val = user_input.get("announce_volume")
                if vol_val is not None:
                    self._rooms[new_key]["announce_volume"] = int(vol_val)
                self._current_key = None
                return await self.async_step_init({})

        # Pre-fill with current values
        if self._current_room and self._current_key:
            default_schema = vol.Schema({
                vol.Required("key", default=self._current_key): cv.string,
                vol.Required(CONF_NAME, default=self._current_room.get("name", "")): cv.string,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=self._current_room.get("entity", ""),
                ): cv.string,
                vol.Optional(
                    "announce_volume",
                    default=self._current_room.get("announce_volume", 50),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=100, step=5, mode=NumberSelectorMode.SLIDER
                    )
                ),
            })
            return self.async_show_form(
                step_id="edit_room",
                data_schema=default_schema,
                errors=errors,
            )

        return await self.async_step_init({})

    async def _save_and_finish(self) -> ConfigFlowResult:
        """Save room config and finish."""
        data = dict(self._config_entry.data)
        data["rooms"] = self._rooms
        self.hass.config_entries.async_update_entry(self._config_entry, data=data)
        return self.async_create_entry(title="", data={})
