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
    UI_UNIQUE_ID,
    YAML_UNIQUE_ID,
)

_LOGGER = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


# SUPPORT_PLAY_MEDIA = 1 << 9 (MediaPlayerEntityFeature.PLAY_MEDIA)
_PLAY_MEDIA = 1 << 9


def _media_player_choices(hass):
    """Return {entity_id: friendly_name} for media_player entities that support play_media.

    Sorted by friendly_name. Does NOT filter by area because some
    integrations (e.g. Music Assistant / DLNA) do not populate area info.
    """
    choices: dict[str, str] = {}
    for state in sorted(
        hass.states.async_all("media_player"),
        key=lambda s: (s.attributes.get("friendly_name") or s.entity_id).lower(),
    ):
        supported = state.attributes.get("supported_features", 0)
        if not (supported & _PLAY_MEDIA):
            continue
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
        # Abort early if already configured — user should use Configure → Options
        await self.async_set_unique_id(UI_UNIQUE_ID)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        areas = _area_choices(self.hass)
        entities = _media_player_choices(self.hass)

        if not entities:
            return self.async_abort(reason="no_media_players")

        if user_input is not None:
            area_id = user_input[CONF_AREA_ID]
            entity_id = user_input[CONF_ENTITY_ID]
            area_name = areas.get(area_id, area_id)

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
        await self.async_set_unique_id(YAML_UNIQUE_ID)
        self._abort_if_unique_id_configured(
            updates={CONF_ROOMS: dict(import_data.get(CONF_ROOMS, {}))}
        )
        return self.async_create_entry(
            title="Home Intercom (YAML)",
            data={CONF_ROOMS: dict(import_data.get(CONF_ROOMS, {}))},
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
        """Entry point — YAML shows info, UI goes straight to Add Room."""
        if self._entry.unique_id == YAML_UNIQUE_ID:
            return self.async_show_form(
                step_id="yaml_info",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "room_count": str(len(self._get_rooms())),
                },
            )
        return await self.async_step_add_room()

    async def async_step_add_room(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Form for adding a new room. Room name = area name."""
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
                rooms[room_id] = {
                    CONF_NAME: user_input.get(CONF_NAME) or areas.get(room_id, room_id),
                    CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                    CONF_ANNOUNCE_VOLUME: user_input.get(CONF_ANNOUNCE_VOLUME),
                    CONF_PAUSE_BUFFER: user_input.get(CONF_PAUSE_BUFFER, 0.0),
                }
                return self.async_create_entry(
                    title="",
                    data={CONF_ROOMS: rooms},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_AREA_ID): vol.In(areas),
                vol.Optional(CONF_NAME, default=""): str,
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
        """Get combined rooms from config entry data + options."""
        data_rooms = dict(self._entry.data.get(CONF_ROOMS, {}))
        options_rooms = dict(self._entry.options.get(CONF_ROOMS, {}))
        return {**data_rooms, **options_rooms}

    def _save_rooms(self, rooms: dict[str, dict[str, Any]]) -> None:
        """Persist rooms to config entry options."""
        options = dict(self._entry.options)
        options[CONF_ROOMS] = rooms
        self.hass.config_entries.async_update_entry(self._entry, options=options)
