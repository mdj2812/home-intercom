"""Config flow and Options flow for Home Intercom.

Config flow creates an empty writable entry; rooms are added from the PWA
(issue #75). Options Flow remains a fallback editor.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import area_registry as ar

from .const import (
    BUTTONS_UNIQUE_ID,
    CONF_ANNOUNCE_VOLUME,
    CONF_AREA_ID,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
    UI_UNIQUE_ID,
)
from .media_players import media_player_catalog

_LOGGER = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _media_player_choices(hass):
    """Return {entity_id: friendly_name} for media_player entities that support play_media."""
    return {entry["entity_id"]: entry["name"] for entry in media_player_catalog(hass)}


def _area_choices(hass):
    """Return {area_id: area_name} for all HA areas, sorted by name."""
    registry = ar.async_get(hass)
    areas = sorted(registry.async_list_areas(), key=lambda a: a.name)
    return {a.id: a.name for a in areas}


# ═══════════════════════════════════════════════════════════════════════
# ConfigFlow
# ═══════════════════════════════════════════════════════════════════════


class HomeIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Home Intercom — single step."""

    VERSION = 1  # Config flow schema version (increment on breaking changes)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Create an empty UI entry. Rooms are added from the PWA (issue #75)."""
        await self.async_set_unique_id(UI_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return self.async_create_entry(title="Home Intercom", data={CONF_ROOMS: {}})

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Import leftover YAML rooms into the writable UI entry (issue #75)."""
        await self.async_set_unique_id(UI_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Home Intercom",
            data={CONF_ROOMS: dict(import_data.get(CONF_ROOMS, {}))},
        )

    async def async_step_buttons(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Auto-create a config entry for intercom button devices (issue #48).

        Called programmatically by _ensure_button_entry(). No user interaction —
        creates a placeholder entry that owns the button HA devices and entities.
        """
        await self.async_set_unique_id(BUTTONS_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Home Intercom Buttons",
            data={CONF_ROOMS: {}},  # no rooms — buttons only
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler for any entry."""
        return HomeIntercomOptionsFlow(config_entry)


# ═══════════════════════════════════════════════════════════════════════
# OptionsFlow
# ═══════════════════════════════════════════════════════════════════════


class HomeIntercomOptionsFlow(OptionsFlow):
    """Handle options flow for Home Intercom — room management."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry point — pick Add Room or select a room to edit."""
        if self._entry.unique_id == BUTTONS_UNIQUE_ID:
            return self.async_abort(reason="buttons_read_only")

        rooms = dict(self._entry.data.get(CONF_ROOMS, {}))
        rooms.update(self._entry.options.get(CONF_ROOMS, {}))

        if user_input is not None:
            choice = user_input["room_choice"]
            if choice == "__new__":
                return await self.async_step_add_room()
            self._edit_room_id = choice
            return await self.async_step_edit_room()

        room_choices = {"__new__": "➕ Add Room..."}
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self.hass)
        for rid, room in rooms.items():
            # Look up OUR device (home_intercom, room_id), not the entity's device.
            display_name = room.get(CONF_NAME, rid)
            dev = dev_reg.async_get_device(identifiers={(DOMAIN, rid)})
            if dev:
                display_name = dev.name_by_user or dev.name or display_name
            room_choices[rid] = display_name

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("room_choice"): vol.In(room_choices)}),
            description_placeholders={"room_count": str(len(rooms))},
        )

    async def async_step_add_room(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Form for adding a new room. Room name = area name. 0 = use default."""
        errors: dict[str, str] = {}
        entities = _media_player_choices(self.hass)
        areas = _area_choices(self.hass)

        if not entities:
            return self.async_abort(reason="no_media_players")

        if user_input is not None:
            room_id = user_input[CONF_AREA_ID]
            rooms = self._get_rooms()
            if room_id in rooms:
                errors["base"] = "room_exists"
            else:
                room: dict[str, Any] = {
                    CONF_NAME: user_input.get(CONF_NAME) or areas.get(room_id, room_id),
                    CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                }
                self._apply_optional_fields(room, user_input)
                rooms[room_id] = room
                return self.async_create_entry(
                    title="",
                    data={CONF_ROOMS: rooms},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA_ID): vol.In(areas),
                vol.Optional(CONF_NAME, default=""): str,
                vol.Required(CONF_ENTITY_ID): vol.In(entities),
                vol.Required(CONF_ANNOUNCE_VOLUME, default=0): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
                vol.Required(CONF_PAUSE_BUFFER, default=0): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
            }
        )

        return self.async_show_form(step_id="add_room", data_schema=schema, errors=errors)

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Form for editing a room — entity, volume, buffer. 0 = use default."""
        errors: dict[str, str] = {}
        entities = _media_player_choices(self.hass)

        rooms = dict(self._entry.data.get(CONF_ROOMS, {}))
        rooms.update(self._entry.options.get(CONF_ROOMS, {}))
        current = rooms.get(self._edit_room_id, {})

        if user_input is not None:
            new_room: dict[str, Any] = {
                **current,
                CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
            }
            # 0 = "not configured" → remove from config
            self._apply_optional_fields(new_room, user_input)
            rooms[self._edit_room_id] = new_room
            return self.async_create_entry(
                title="",
                data={CONF_ROOMS: rooms},
            )

        # Use Required (not Optional) — HA Optional+default always sends default
        # even when unchecked. Required always sends the value: 0 = use default.
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENTITY_ID,
                    default=current.get(CONF_ENTITY_ID, ""),
                ): vol.In(entities),
                vol.Required(
                    CONF_ANNOUNCE_VOLUME,
                    default=current.get(CONF_ANNOUNCE_VOLUME) or 0,
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Required(
                    CONF_PAUSE_BUFFER,
                    default=current.get(CONF_PAUSE_BUFFER) or 0,
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
            }
        )

        return self.async_show_form(
            step_id="edit_room",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "room_name": current.get(CONF_NAME, self._edit_room_id),
            },
        )

    async def async_step_confirm_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirmation dialog before deleting a room."""
        rooms = self._get_rooms()
        room = rooms.get(self._delete_room_id, {})
        room_name = room.get(CONF_NAME, self._delete_room_id)

        if user_input is not None:
            rooms.pop(self._delete_room_id, None)
            self._save_rooms(rooms)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="confirm_delete",
            data_schema=vol.Schema({}),
            description_placeholders={"room_name": room_name},
        )

    # ——— helpers ———

    def _get_rooms(self) -> dict[str, dict[str, Any]]:
        """Get combined rooms from config entry data + options."""
        data_rooms = dict(self._entry.data.get(CONF_ROOMS, {}))
        options_rooms = dict(self._entry.options.get(CONF_ROOMS, {}))
        return {**data_rooms, **options_rooms}

    def _save_rooms(self, rooms: dict[str, dict[str, Any]]) -> None:
        """Persist rooms to config entry options."""
        options = dict(self._entry.options)
        options[CONF_ROOMS] = rooms
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    @staticmethod
    def _apply_optional_fields(room: dict[str, Any], user_input: dict[str, Any]) -> None:
        """Apply announce_volume / pause_buffer. 0 = remove from config."""
        vol_val = user_input.get(CONF_ANNOUNCE_VOLUME)
        if vol_val not in (None, 0):
            room[CONF_ANNOUNCE_VOLUME] = vol_val
        else:
            room.pop(CONF_ANNOUNCE_VOLUME, None)
        buf_val = user_input.get(CONF_PAUSE_BUFFER)
        if buf_val not in (None, 0.0, 0):
            room[CONF_PAUSE_BUFFER] = buf_val
        else:
            room.pop(CONF_PAUSE_BUFFER, None)
