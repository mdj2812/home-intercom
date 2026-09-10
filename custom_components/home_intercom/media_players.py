"""HA media_player catalog for the PWA picker (issue #73).

Kept out of config_flow.py so api.py can import it without loading ConfigFlow
(unit tests use a fake homeassistant package).
"""

from __future__ import annotations

from .rooms import PLAY_MEDIA, sort_media_player_catalog


def media_player_catalog(hass) -> list[dict[str, str]]:
    """Players that support play_media, sorted by area then name."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    er_reg = er.async_get(hass)
    ar_reg = ar.async_get(hass)

    entries: list[dict[str, str]] = []
    for state in hass.states.async_all("media_player"):
        supported = state.attributes.get("supported_features", 0) or 0
        if not (supported & PLAY_MEDIA):
            continue
        friendly = state.attributes.get("friendly_name") or state.entity_id
        area_name = ""
        e_entry = er_reg.async_get(state.entity_id)
        if e_entry and e_entry.area_id:
            area = ar_reg.async_get_area(e_entry.area_id)
            if area and area.name:
                area_name = area.name.strip()
        entries.append({"entity_id": state.entity_id, "name": str(friendly), "area": area_name})
    return sort_media_player_catalog(entries)
