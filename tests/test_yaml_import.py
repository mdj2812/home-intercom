"""YAML rooms imported once into the writable UI entry (issue #75)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .ha_fakes import install_fake_homeassistant

install_fake_homeassistant()

from custom_components.home_intercom.__init__ import (  # noqa: E402
    CONF_ROOMS,
    DOMAIN,
    UI_UNIQUE_ID,
    YAML_UNIQUE_ID,
    _import_yaml_rooms,
    async_setup,
    merge_incoming_rooms,
)

LIVING = {"name": "客厅", "entity_id": "media_player.living"}
CINEMA = {"name": "影音室", "entity_id": "media_player.cinema"}
STUDY = {"name": "书房", "entity_id": "media_player.study"}


def test_merge_skips_existing_keys():
    merged, added = merge_incoming_rooms(
        {"living": dict(LIVING), "study": dict(STUDY)},
        {
            "living": {"name": "YAML Living", "entity_id": "media_player.other"},
            "cinema": dict(CINEMA),
        },
    )
    assert added == ["cinema"]
    assert merged["living"]["entity_id"] == "media_player.living"
    assert merged["cinema"] == CINEMA
    assert merged["study"] == STUDY


def _entry(unique_id: str, entry_id: str, rooms: dict, *, options: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.entry_id = entry_id
    entry.data = {CONF_ROOMS: rooms}
    entry.options = options or {}
    return entry


@pytest.mark.asyncio
async def test_import_merges_yaml_into_ui_and_removes_yaml_entry():
    ui = _entry(UI_UNIQUE_ID, "ui-entry", {"study": dict(STUDY)})
    yaml_entry = _entry(
        YAML_UNIQUE_ID, "yaml-entry", {"living": dict(LIVING), "cinema": dict(CINEMA)}
    )
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [ui, yaml_entry]
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_remove = AsyncMock()

    with patch("custom_components.home_intercom.__init__._move_room_devices") as move:
        await _import_yaml_rooms(hass, {})

    hass.config_entries.async_update_entry.assert_called_once()
    _opts = hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert set(_opts[CONF_ROOMS]) == {"study", "living", "cinema"}
    assert _opts[CONF_ROOMS]["study"] == STUDY
    move.assert_called_once_with(hass, "yaml-entry", "ui-entry")
    hass.config_entries.async_remove.assert_awaited_once_with("yaml-entry")


@pytest.mark.asyncio
async def test_import_does_not_overwrite_ui_rooms():
    ui = _entry(
        UI_UNIQUE_ID,
        "ui-entry",
        {"living": {"name": "PWA Living", "entity_id": "media_player.pwa"}},
    )
    yaml_entry = _entry(YAML_UNIQUE_ID, "yaml-entry", {"living": dict(LIVING)})
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [ui, yaml_entry]
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_remove = AsyncMock()

    with patch("custom_components.home_intercom.__init__._move_room_devices"):
        await _import_yaml_rooms(hass, {})

    hass.config_entries.async_update_entry.assert_not_called()
    hass.config_entries.async_remove.assert_awaited_once_with("yaml-entry")


@pytest.mark.asyncio
async def test_async_setup_warns_when_yaml_block_present(caplog):
    ui = _entry(UI_UNIQUE_ID, "ui-entry", {"study": dict(STUDY)})
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [ui]
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_remove = AsyncMock()

    with caplog.at_level("WARNING"):
        ok = await async_setup(hass, {DOMAIN: {CONF_ROOMS: {"cinema": dict(CINEMA)}}})
    assert ok is True
    assert "deprecated" in caplog.text
    hass.config_entries.async_update_entry.assert_called_once()
    rooms = hass.config_entries.async_update_entry.call_args.kwargs["options"][CONF_ROOMS]
    assert rooms["cinema"] == CINEMA
    assert rooms["study"] == STUDY


@pytest.mark.asyncio
async def test_import_creates_ui_entry_when_missing():
    yaml_entry = _entry(YAML_UNIQUE_ID, "yaml-entry", {"living": dict(LIVING)})
    ui = _entry(UI_UNIQUE_ID, "ui-entry", {"living": dict(LIVING)})
    hass = MagicMock()
    entries = [yaml_entry]
    scheduled: list = []

    def _entries(_domain=None):
        return list(entries)

    async def _init(*_args, **_kwargs):
        entries.append(ui)

    def _create_task(coro, *_args, **_kwargs):
        scheduled.append(coro)
        return coro

    hass.config_entries.async_entries.side_effect = _entries
    hass.config_entries.flow.async_init = AsyncMock(side_effect=_init)
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_remove = AsyncMock()
    hass.async_create_task = _create_task

    with patch("custom_components.home_intercom.__init__._move_room_devices") as move:
        await _import_yaml_rooms(hass, {})
        assert len(scheduled) == 1
        await scheduled[0]

    hass.config_entries.flow.async_init.assert_awaited_once()
    hass.config_entries.async_update_entry.assert_not_called()
    move.assert_called_once_with(hass, "yaml-entry", "ui-entry")
    hass.config_entries.async_remove.assert_awaited_once_with("yaml-entry")


@pytest.mark.asyncio
async def test_yaml_config_schedules_ui_entry_when_none_exist():
    """Fresh HA with YAML only — smoke test path. Must not await the import flow."""
    hass = MagicMock()
    scheduled: list = []
    hass.config_entries.async_entries.return_value = []
    hass.config_entries.flow.async_init = AsyncMock()
    hass.async_create_task = lambda coro, *_a, **_k: scheduled.append(coro) or coro

    await _import_yaml_rooms(hass, {"test": dict(LIVING)})
    assert len(scheduled) == 1
    hass.config_entries.flow.async_init.assert_not_awaited()
    await scheduled[0]
    hass.config_entries.flow.async_init.assert_awaited_once()
    data = hass.config_entries.flow.async_init.await_args.kwargs["data"]
    assert "test" in data[CONF_ROOMS]
