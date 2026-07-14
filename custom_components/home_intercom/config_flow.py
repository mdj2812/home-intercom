"""Config flow and Options flow for Home Intercom.

Provides UI-driven setup (Settings → Devices & Services → Add Integration)
and room management (Configure → Options).

Config flow: area + media_player selection in a single step.
Options flow: add / edit / delete rooms via UI dialogs.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import area_registry as ar

from .const import (
    CONF_ANNOUNCE_VOLUME,
    CONF_AREA_ID,
    CONF_PAUSE_BUFFER,
    CONF_ROOMS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _media_player_choices(hass):
    """Return {entity_id: friendly_name} for all media_player entities.

    Sorted by friendly_name. Does NOT filter by area because some
    integrations (e.g. Music Assistant / DLNA) do not populate area info.
    """
    choices: dict[str, str] = {}
    for state in sorted(
        hass.states.async_all("media_player"),
        key=lambda s: (s.attributes.get("friendly_name") or s.entity_id).lower(),
    ):
        choices[state.entity_id] = state.attributes.get("friendly_name", state.entity_id)
    return choices


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

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step: pick area + media_player + optional params in one form."""
        errors: dict[str, str] = {}

        areas = _area_choices(self.hass)
        entities = _media_player_choices(self.hass)

        if not entities:
            return self.async_abort(reason="no_media_players")

        if user_input is not None:
            area_id = user_input[CONF_AREA_ID]
            entity_id = user_input[CONF_ENTITY_ID]
            area_name = areas.get(area_id, area_id)

            # Prevent duplicate config entries (one per domain is enough)
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="Home Intercom",
                data={
                    CONF_ROOMS: {
                        area_id: {
                            CONF_NAME: area_name,
                            CONF_ENTITY_ID: entity_id,
                            CONF_ANNOUNCE_VOLUME: user_input.get(CONF_ANNOUNCE_VOLUME),
                            CONF_PAUSE_BUFFER: user_input.get(CONF_PAUSE_BUFFER, 0.0),
                        }
                    }
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA_ID): vol.In(areas) if areas else vol.In({"_": "no areas"}),
                vol.Required(CONF_ENTITY_ID): vol.In(entities),
                vol.Optional(CONF_ANNOUNCE_VOLUME): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
                vol.Optional(CONF_PAUSE_BUFFER): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Import from YAML configuration.yaml."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        rooms = import_data.get(CONF_ROOMS, {})
        return self.async_create_entry(
            title="Home Intercom",
            data={CONF_ROOMS: dict(rooms)},
        )


# ═══════════════════════════════════════════════════════════════════════
# OptionsFlow
# ═══════════════════════════════════════════════════════════════════════


class HomeIntercomOptionsFlow(OptionsFlow):
    """Handle options flow for Home Intercom — room management."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show room list with add / edit / delete options."""
        if user_input is not None:
            choice = user_input["next_step_id"]
            if choice == "add_room":
                return await self.async_step_add_room()
            if choice.startswith("edit_"):
                self._edit_room_id = choice.removeprefix("edit_")
                return await self.async_step_edit_room()
            if choice.startswith("delete_"):
                self._delete_room_id = choice.removeprefix("delete_")
                return await self.async_step_confirm_delete()

        rooms = self._get_rooms()
        menu_options = ["add_room"]
        for room_id, _cfg in rooms.items():
            menu_options.append(f"edit_{room_id}")
            menu_options.append(f"delete_{room_id}")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            # HA renders menu_options as a list; we provide context via description_placeholders
            description_placeholders={
                "room_count": str(len(rooms)),
            },
        )

    async def async_step_add_room(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Form for adding a new room."""
        errors: dict[str, str] = {}
        entities = _media_player_choices(self.hass)

        if not entities:
            return self.async_abort(reason="no_media_players")

        if user_input is not None:
            room_id = user_input[CONF_AREA_ID]
            rooms = self._get_rooms()
            if room_id in rooms:
                errors["base"] = "room_exists"
            else:
                rooms[room_id] = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                    CONF_ANNOUNCE_VOLUME: user_input.get(CONF_ANNOUNCE_VOLUME),
                    CONF_PAUSE_BUFFER: user_input.get(CONF_PAUSE_BUFFER, 0.0),
                }
                self._save_rooms(rooms)
                return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA_ID): str,
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_ENTITY_ID): vol.In(entities),
                vol.Optional(CONF_ANNOUNCE_VOLUME): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
                vol.Optional(CONF_PAUSE_BUFFER): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=10)
                ),
            }
        )

        return self.async_show_form(step_id="add_room", data_schema=schema, errors=errors)

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Form for editing an existing room (pre-filled with current values)."""
        errors: dict[str, str] = {}
        entities = _media_player_choices(self.hass)
        rooms = self._get_rooms()
        current = rooms.get(self._edit_room_id, {})

        if user_input is not None:
            rooms[self._edit_room_id] = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                CONF_ANNOUNCE_VOLUME: user_input.get(CONF_ANNOUNCE_VOLUME),
                CONF_PAUSE_BUFFER: user_input.get(CONF_PAUSE_BUFFER, 0.0),
            }
            self._save_rooms(rooms)
            return await self.async_step_init()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=current.get(CONF_NAME, "")): str,
                vol.Required(CONF_ENTITY_ID, default=current.get(CONF_ENTITY_ID, "")): vol.In(
                    entities
                ),
                vol.Optional(
                    CONF_ANNOUNCE_VOLUME,
                    default={"default": current.get(CONF_ANNOUNCE_VOLUME)},
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                vol.Optional(
                    CONF_PAUSE_BUFFER,
                    default=current.get(CONF_PAUSE_BUFFER, 0.0),
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
        """Get current rooms dict from config entry options (or data fallback)."""
        options = dict(self._entry.options)
        rooms = options.get(CONF_ROOMS)
        if rooms is not None:
            return dict(rooms)
        # Fall back to entry.data for backward compatibility
        return dict(self._entry.data.get(CONF_ROOMS, {}))

    def _save_rooms(self, rooms: dict[str, dict[str, Any]]) -> None:
        """Persist rooms to config entry options."""
        options = dict(self._entry.options)
        options[CONF_ROOMS] = rooms
        self.hass.config_entries.async_update_entry(self._entry, options=options)
