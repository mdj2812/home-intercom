"""HomeAssistantView endpoints for Home Intercom.

Maps the Flask routes from intercom_server.py to HomeAssistantView:
  /record        → RecordView        (PWA token; POST audio → WAV → play)
  /device/record → DeviceRecordView  (HA auth; POST audio → WAV → play)
  /chime         → ChimeView         (GET status; POST/DELETE custom chime via PWA token)
  /rooms/status  → StatusView  (GET speaker online status)
  /version       → VersionView (GET version only)
  /rooms         → RoomsView       (GET room config)
  /rooms/{id}    → RoomsItemView   (PUT/PATCH/DELETE via PWA token)
  /rooms/order   → RoomsOrderView  (PUT ordered id list via PWA token)
  /media_players  → MediaPlayersView (GET play_media speakers)
  /devices       → DevicesView        (GET registry)
  /devices/approve → DevicesApproveView
  /devices/manage  → DevicesManageView (approve/deapprove/revoke/unrevoke/delete/ota/buttons)
  /firmware        → FirmwareView (GET cached .bin)
  /firmware.sig    → FirmwareSigView
  /firmware/status → FirmwareStatusView (GET cached version)
  /firmware/sync   → FirmwareSyncView (POST GitHub fetch, PWA token)
  /audio/<path>  → AudioView   (GET recorded WAV files)
  /home_intercom  → PanelView        (GET PWA frontend HTML, legacy path)
  /home-intercom  → PanelAliasView   (GET PWA frontend HTML, sidebar-friendly)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ROOMS,
    DOMAIN,
    FIRMWARE_CACHE_SUBDIR,
    PANEL_PATH,
    PANEL_PATH_LEGACY,
    PCM_RATE,
    UI_UNIQUE_ID,
    WAV_HEADER_SIZE,
)
from .firmware import (
    FirmwareError,
    ensure_latest_firmware,
    firmware_cache_status,
    firmware_checksum_headers,
    load_cached_firmware,
    schedule_firmware_refresh,
    sync_firmware_cache,
)
from .media_players import media_player_catalog
from .player import play_announcement
from .rooms import (
    RoomValidationError,
    combined_entry_rooms,
    patch_room,
    put_room,
    reorder_rooms,
    validate_room_key,
)
from .shared import (
    buttons_from_manage_body,
    chime_public_url,
    chime_status_payload,
    config_payload,
    delete_custom_chime,
    device_hello_payload,
    device_ota_reject_reason,
    device_record_auth_error,
    devices_payload,
    is_wav,
    normalize_mac,
    parse_device_manage_body,
    pins_from_hello_body,
    resolve_chime_wav,
    wait_for_pending_hello,
    write_custom_chime_wav,
)
from .shared import concat_wavs as _concat_wavs
from .shared import handle_pcm_to_wav as _handle_pcm_to_wav
from .shared import handle_wav_passthrough as _handle_wav_passthrough

_LOGGER = logging.getLogger(__name__)
_INTEGRATION_DIR = Path(__file__).parent


def _guess_base_url(request: web.Request) -> str:
    """Guess the HA base URL from the incoming request.

    Uses X-Forwarded-Proto for reverse-proxy setups.
    Falls back to request scheme + host.
    """
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.host
    return f"{scheme}://{host}"


def _firmware_dir(hass: HomeAssistant) -> str:
    """HA cache: ``www/home_intercom_audio/firmware/`` (or explicit override)."""
    data = _get_hass_data(hass)
    explicit = data.get("firmware_dir")
    if explicit:
        return str(explicit)
    audio_dir = data.get("audio_dir", "")
    return os.path.join(audio_dir, FIRMWARE_CACHE_SUBDIR) if audio_dir else ""


def _kick_firmware_cache(hass: HomeAssistant) -> None:
    """Start a GitHub cache refresh after the first button registers."""
    schedule_firmware_refresh(_firmware_dir(hass))


def _get_hass_data(hass: HomeAssistant) -> dict:
    """Get integration data dict — guaranteed to exist after async_setup."""
    return hass.data.get(DOMAIN, {})


async def _handle_record(request: web.Request) -> web.Response:
    """Receive audio, write it as WAV, and play it on the requested targets."""
    hass = request.app["hass"]
    data = await request.read()
    target = request.query.get("target", "")

    if not target:
        return web.json_response({"ok": False, "error": "missing target"}, status=400)

    room_map = _get_hass_data(hass).get("rooms", {})

    if target == "all":
        targets = [(k, v) for k, v in room_map.items() if v.get("entity_id")]
        if not targets:
            return web.json_response({"ok": False, "error": "no rooms configured"}, status=500)
    else:
        room = room_map.get(target)
        if not room or not room.get("entity_id"):
            return web.json_response(
                {"ok": False, "error": f"unknown target: {target}"}, status=400
            )
        targets = [(target, room)]

    if len(data) < WAV_HEADER_SIZE:
        return web.json_response({"ok": False, "error": "no audio data"}, status=400)

    audio_dir = _get_hass_data(hass).get("audio_dir", "")
    filename = f"intercom_{target}.wav"
    filepath = os.path.join(audio_dir, filename)

    if is_wav(data):
        _rate, duration = await hass.async_add_executor_job(_handle_wav_passthrough, data, filepath)
    else:
        rate_obj = int(request.query.get("rate", PCM_RATE))
        duration = await hass.async_add_executor_job(_handle_pcm_to_wav, data, rate_obj, filepath)

    # Build public URL — absolute URL needed for DLNA/MiOT players.
    # Priority: configured external_url > internal_url > request host.
    base_url = hass.config.external_url or hass.config.internal_url or _guess_base_url(request)
    audio_url = f"{base_url.rstrip('/')}/local/home_intercom_audio/{filename}"

    chime_path = str(resolve_chime_wav(integration_dir=_INTEGRATION_DIR, audio_dir=audio_dir))
    chime_url = chime_public_url(base_url, audio_dir=audio_dir, deployment="ha")
    audio_url_with_chime = None
    duration_with_chime = None
    if os.path.exists(chime_path):
        filename_chime = f"intercom_{target}_chime.wav"
        filepath_chime = os.path.join(audio_dir, filename_chime)
        try:
            duration_with_chime = await hass.async_add_executor_job(
                _concat_wavs, chime_path, filepath, filepath_chime
            )
            audio_url_with_chime = (
                f"{base_url.rstrip('/')}/local/home_intercom_audio/{filename_chime}"
            )
        except Exception as exc:
            _LOGGER.warning("Failed to prepend chime: %s", exc)

    # Play on each target room
    ok_count = 0
    errors: list[dict] = []
    for _tgt_key, tgt_room in targets:
        announce_volume = tgt_room.get("announce_volume")
        pause_buffer = tgt_room.get("pause_buffer", 0.0)
        result = await play_announcement(
            hass,
            tgt_room["entity_id"],
            audio_url,
            duration if not duration_with_chime else 0,
            announce_volume=announce_volume,
            audio_url_with_chime=audio_url_with_chime,
            duration_with_chime=duration_with_chime,
            chime_url=chime_url,
            pause_buffer=pause_buffer,
        )
        if result.ok:
            ok_count += 1
        else:
            errors.append({"entity_id": tgt_room["entity_id"], "error": result.error or "unknown"})

    name = room_map[target]["name"] if target != "all" else "All Rooms"
    _LOGGER.info("played on %d/%d rooms for %s", ok_count, len(targets), name)

    return web.json_response(
        {
            "ok": True,
            "name": name,
            "rooms_sent": ok_count,
            "rooms_total": len(targets),
            "url": audio_url,
        }
    )


def _verify_pwa_token(request: web.Request, *, view: str) -> web.Response | None:
    """Return 401 response when PWA token is configured but header mismatches."""
    hass = request.app["hass"]
    pwa_token = hass.data.get(DOMAIN, {}).get("pwa_token", "")
    if pwa_token and request.headers.get("X-PWA-Token") != pwa_token:
        _LOGGER.warning("%s: invalid or missing X-PWA-Token", view)
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return None


def _find_ui_entry(hass: HomeAssistant):
    """Writable UI config entry (Options Flow / PWA store). Buttons are not writable."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "unique_id", None) == UI_UNIQUE_ID:
            return entry
    return None


def _entry_rooms(entry) -> dict:
    """Combined data + options rooms for one config entry (options key order wins)."""
    return combined_entry_rooms(entry)


def _persist_ui_rooms(hass: HomeAssistant, entry, rooms: dict) -> dict:
    """Save rooms on the UI entry and refresh the merged in-memory map.

    ``async_update_entry`` triggers the options update listener → reload,
    which rebuilds room devices. In-memory state is updated immediately so
    GET /rooms matches the write response without waiting for reload.
    """
    options = dict(entry.options)
    options[CONF_ROOMS] = rooms
    hass.config_entries.async_update_entry(entry, options=options)
    data = _get_hass_data(hass)
    data.setdefault("entry_rooms", {})[entry.entry_id] = rooms
    merged: dict = {}
    for room_map in data.get("entry_rooms", {}).values():
        merged.update(room_map)
    data["rooms"] = merged
    return merged


def _remove_button_ha_device(hass: HomeAssistant, mac: str) -> None:
    """Remove the HA device registry entry for a button MAC (PWA delete).

    Home Assistant only — Docker has no device registry and never calls this.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, normalize_mac(mac))})
    if device is not None:
        _LOGGER.info("Removing HA device for button %s", normalize_mac(mac))
        registry.async_remove_device(device.id)


def _remove_room_ha_device(hass: HomeAssistant, room_id: str, entry_id: str) -> None:
    """Remove the HA device (and its entities) for a PWA-deleted room.

    Home Assistant only — Docker has no device registry and never calls this.
    Look up by the owning config entry: HA 2026.9 identifiers are unique per
    entry, and ``async_get_device(identifiers=...)`` is deprecated / unreliable.
    Reload also runs ``_reconcile_room_devices`` so leftovers still disappear.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    ident = (DOMAIN, room_id)
    for device in list(registry.devices.get_devices_for_config_entry_id(entry_id)):
        if ident in device.identifiers:
            _LOGGER.info("Removing HA device for room %s", room_id)
            registry.async_remove_device(device.id)
            return


class RecordView(HomeAssistantView):
    """POST /api/home_intercom/record using the PWA shared token."""

    url = "/api/home_intercom/record"
    name = "api:home_intercom:record"
    requires_auth = False  # auth via X-PWA-Token header

    async def post(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="RecordView")
        if denied is not None:
            return denied
        return await _handle_record(request)


class ChimeView(HomeAssistantView):
    """GET/POST/DELETE /api/home_intercom/chime — custom pre-announce chime (#66)."""

    url = "/api/home_intercom/chime"
    name = "api:home_intercom:chime"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        data = _get_hass_data(hass)
        audio_dir = data.get("audio_dir", "")
        base_url = hass.config.external_url or hass.config.internal_url or _guess_base_url(request)
        payload = chime_status_payload(base_url=base_url, audio_dir=audio_dir, deployment="ha")
        return web.json_response(payload)

    async def post(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="ChimeView")
        if denied is not None:
            return denied

        hass = request.app["hass"]
        audio_dir = _get_hass_data(hass).get("audio_dir", "")
        if not audio_dir:
            return web.json_response({"ok": False, "error": "audio dir not configured"}, status=500)

        data = await request.read()
        if len(data) < WAV_HEADER_SIZE:
            return web.json_response({"ok": False, "error": "no audio data"}, status=400)

        try:
            await hass.async_add_executor_job(write_custom_chime_wav, data, audio_dir)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        base_url = hass.config.external_url or hass.config.internal_url or _guess_base_url(request)
        url = chime_public_url(base_url, audio_dir=audio_dir, deployment="ha")
        return web.json_response({"ok": True, "custom": True, "url": url})

    async def delete(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="ChimeView")
        if denied is not None:
            return denied

        hass = request.app["hass"]
        audio_dir = _get_hass_data(hass).get("audio_dir", "")
        if audio_dir:
            await hass.async_add_executor_job(delete_custom_chime, audio_dir)
        return web.json_response({"ok": True, "custom": False})


class DeviceRecordView(HomeAssistantView):
    """POST /api/home_intercom/device/record — hardware clients (issue #47).

    Auth, first match wins:
    1. X-Device-ID header → MAC checked against the device registry
       (unknown / revoked → 403). No HA token needed on the device.
    2. Otherwise the request must be HA-authenticated (Bearer token),
       attached by HA's auth middleware as hass_user.
    """

    url = "/api/home_intercom/device/record"
    name = "api:home_intercom:device-record"
    requires_auth = False  # custom dual auth: MAC registry or HA user

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        mac = request.headers.get("X-Device-ID", "")
        if mac:
            store = _get_hass_data(hass).get("device_store")
            device = store.get(mac) if store is not None else None
            error = device_record_auth_error(device)
            if error:
                _LOGGER.warning("device/record rejected for %s: %s", mac, error)
                return web.json_response({"ok": False, "error": error}, status=403)
            return await _handle_record(request)

        if request.get(KEY_HASS_USER) is not None:
            return await _handle_record(request)

        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)


class StatusView(HomeAssistantView):
    """GET /api/home_intercom/rooms/status — query speaker online status."""

    url = "/api/home_intercom/rooms/status"
    name = "api:home_intercom:status"
    requires_auth = False  # public: entity online/offline states only

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        room_map = _get_hass_data(hass).get("rooms", {})

        # Query entity states via hass (no REST round-trip)
        status = {}
        for key, room in room_map.items():
            entity_id = room.get("entity_id", "")
            if not entity_id:
                status[key] = {"status": "online", "friendly_name": room.get("name", key)}
                continue
            state = hass.states.get(entity_id)
            if not state or state.state == "unavailable":
                status[key] = {"status": "unavailable", "friendly_name": ""}
                continue
            # Check supported_features
            attrs = state.attributes
            friendly = attrs.get("friendly_name", entity_id)
            supported = attrs.get("supported_features", 0)
            if supported & (1 << 9):  # SUPPORT_PLAY_MEDIA
                status[key] = {"status": "online", "friendly_name": friendly}
            else:
                status[key] = {"status": "no_play_media", "friendly_name": friendly}

        return web.json_response(status)


class VersionView(HomeAssistantView):
    """GET /api/home_intercom/version — version + PCM rate (single source of truth)."""

    url = "/api/home_intercom/version"
    name = "api:home_intercom:version"
    requires_auth = False  # public: version + PCM rate only

    async def get(self, request: web.Request) -> web.Response:
        # Read version from manifest.json (offloaded to executor)
        version = "dev"
        manifest_path = _INTEGRATION_DIR / "manifest.json"

        def _read_version() -> str:
            try:
                import json as _json

                with open(manifest_path, encoding="utf-8") as f:
                    return _json.load(f).get("version", "dev")
            except (FileNotFoundError, Exception):
                return "dev"

        version = await request.app["hass"].async_add_executor_job(_read_version)

        return web.json_response({"version": version})


class ConfigView(HomeAssistantView):
    """GET /api/home_intercom/config — global audio settings (issue #39).

    Public like /version and /rooms: ESP32 holds zero secrets. Same audio
    fields the hello payload delivers, discoverable pre-registration.
    """

    url = "/api/home_intercom/config"
    name = "api:home_intercom:config"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(config_payload())


class RoomsView(HomeAssistantView):
    """GET /api/home_intercom/rooms — room configuration."""

    url = "/api/home_intercom/rooms"
    name = "api:home_intercom:rooms"
    requires_auth = False  # public: room names only, no secrets

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(_get_hass_data(request.app["hass"]).get("rooms", {}))


class RoomsItemView(HomeAssistantView):
    """PUT/PATCH/DELETE /api/home_intercom/rooms/{id} — PWA room writes (#72)."""

    url = "/api/home_intercom/rooms/{room_id}"
    name = "api:home_intercom:rooms-item"
    requires_auth = False  # auth via X-PWA-Token header

    async def put(self, request: web.Request, room_id: str) -> web.Response:
        return await _rooms_write(request, room_id, method="PUT")

    async def patch(self, request: web.Request, room_id: str) -> web.Response:
        return await _rooms_write(request, room_id, method="PATCH")

    async def delete(self, request: web.Request, room_id: str) -> web.Response:
        return await _rooms_write(request, room_id, method="DELETE")


async def _rooms_write(request: web.Request, room_id: str, *, method: str) -> web.Response:
    """Mutate one room on the UI config entry and persist immediately."""
    denied = _verify_pwa_token(request, view="RoomsItemView")
    if denied is not None:
        return denied
    try:
        key = validate_room_key(room_id)
    except RoomValidationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    hass = request.app["hass"]
    entry = _find_ui_entry(hass)
    if entry is None:
        return web.json_response({"ok": False, "error": "no writable config entry"}, status=409)

    ui_rooms = _entry_rooms(entry)

    if method == "DELETE":
        if key not in ui_rooms:
            return web.json_response({"ok": False, "error": "unknown room"}, status=404)
        ui_rooms.pop(key)
        rooms = _persist_ui_rooms(hass, entry, ui_rooms)
        _remove_room_ha_device(hass, key, entry.entry_id)
        return web.json_response({"ok": True, "rooms": rooms})

    try:
        body = await request.json()
    except Exception:
        body = None
    try:
        if method == "PUT":
            room = put_room(body, entity_key="entity_id")
        elif key not in ui_rooms:
            return web.json_response({"ok": False, "error": "unknown room"}, status=404)
        else:
            room = patch_room(ui_rooms[key], body, entity_key="entity_id")
    except RoomValidationError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    ui_rooms[key] = room
    rooms = _persist_ui_rooms(hass, entry, ui_rooms)
    return web.json_response({"ok": True, "rooms": rooms})


class RoomsOrderView(HomeAssistantView):
    """PUT /api/home_intercom/rooms/order — persist settings-list order (#76)."""

    url = "/api/home_intercom/rooms/order"
    name = "api:home_intercom:rooms-order"
    requires_auth = False  # auth via X-PWA-Token header

    async def put(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="RoomsOrderView")
        if denied is not None:
            return denied
        hass = request.app["hass"]
        entry = _find_ui_entry(hass)
        if entry is None:
            return web.json_response({"ok": False, "error": "no writable config entry"}, status=409)
        try:
            body = await request.json()
        except Exception:
            body = None
        try:
            rooms = reorder_rooms(_entry_rooms(entry), (body or {}).get("order"))
        except RoomValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        persisted = _persist_ui_rooms(hass, entry, rooms)
        return web.json_response({"ok": True, "rooms": persisted})


class MediaPlayersView(HomeAssistantView):
    """GET /api/home_intercom/media_players — play_media speakers for the PWA picker (#73)."""

    url = "/api/home_intercom/media_players"
    name = "api:home_intercom:media-players"
    requires_auth = False  # public like GET /rooms; names only, no secrets

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(media_player_catalog(request.app["hass"]))


class DevicesHelloView(HomeAssistantView):
    """POST /api/home_intercom/devices/hello — ESP32 boot registration (issue #37).

    Trust-on-first-use: unknown MACs auto-register as pending (issue #51)
    until an admin approves. Revoked devices are rejected. No secrets on
    the device — it identifies by MAC address only.
    """

    url = "/api/home_intercom/devices/hello"
    name = "api:home_intercom:devices-hello"
    requires_auth = False  # MAC identity per trust-on-first-use model

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        mac = request.headers.get("X-Device-ID", "")
        if not mac:
            return web.json_response(
                {"status": "error", "error": "missing X-Device-ID header"}, status=400
            )

        try:
            body = await request.json()
        except Exception:
            body = {}
        firmware_version = body.get("firmware_version", "") if isinstance(body, dict) else ""
        pins = pins_from_hello_body(body)

        store = _get_hass_data(hass).get("device_store")
        if store is None:
            return web.json_response(
                {"status": "error", "error": "device registry unavailable"}, status=500
            )

        existing = store.get(mac)
        if existing and existing.get("revoked"):
            _LOGGER.warning("hello from revoked device %s — rejected", mac)
            return web.json_response({"status": "error", "error": "device revoked"}, status=403)

        was_empty = not store.devices
        is_new = existing is None  # before register_or_update
        try:
            device = await store.register_or_update(mac, firmware_version, pins=pins)
        except ValueError:
            return web.json_response(
                {"status": "error", "error": "invalid X-Device-ID (MAC)"}, status=400
            )

        if was_empty:
            _kick_firmware_cache(hass)

        # New device → reload to register HA device + entities (issue #48)
        if is_new:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            async_dispatcher_send(hass, f"{DOMAIN}_device_store_changed")

        # Hold this hello until approve so the ESP32 gets status=ok without
        # waiting for its next heartbeat (issue #51).
        device = await hass.async_add_executor_job(wait_for_pending_hello, store.get, mac)
        if device is None:
            return web.json_response({"status": "error", "error": "unknown device"}, status=404)
        if device.get("revoked"):
            return web.json_response({"status": "error", "error": "device revoked"}, status=403)

        rooms = _get_hass_data(hass).get("rooms") or {}
        return web.json_response(device_hello_payload(device, valid_rooms=set(rooms)))


def _panel_base_from_path(path: str) -> str:
    """Return panel URL prefix matching the incoming request path."""
    if path.startswith(PANEL_PATH):
        return PANEL_PATH
    return PANEL_PATH_LEGACY


async def _serve_panel(request: web.Request) -> web.Response:
    """Serve intercom.html with static paths rewritten for the HA panel context."""
    panel_base = _panel_base_from_path(request.path)
    html_path = _INTEGRATION_DIR / "intercom.html"
    try:
        html = await request.app["hass"].async_add_executor_job(
            lambda: html_path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return web.Response(
            text="<h1>Home Intercom</h1><p>Frontend not found</p>",
            content_type="text/html",
        )
    except Exception as exc:
        _LOGGER.exception("PanelView failed")
        return web.Response(
            text=f"<h1>500 Internal Server Error</h1><p>{exc}</p>",
            content_type="text/html",
            status=500,
        )

    html = html.replace('src="/static/', f'src="{panel_base}/static/')
    html = html.replace('href="/static/', f'href="{panel_base}/static/')

    pwa_token = request.app["hass"].data.get(DOMAIN, {}).get("pwa_token", "")
    if pwa_token:
        html = html.replace(
            "</head>",
            f'<script>window._PWA_TOKEN="{pwa_token}";</script>\n</head>',
        )

    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


class DevicesView(HomeAssistantView):
    """GET /api/home_intercom/devices — read-only registry listing (issue #52).

    Gated by the PWA shared token (same as RecordView): device names and
    MACs are the registry's only auth material, so this isn't public.
    Mutations use DevicesApproveView / DevicesManageView.
    """

    url = "/api/home_intercom/devices"
    name = "api:home_intercom:devices"
    requires_auth = False  # auth via X-PWA-Token header

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        pwa_token = hass.data.get(DOMAIN, {}).get("pwa_token", "")
        if pwa_token and request.headers.get("X-PWA-Token") != pwa_token:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        store = _get_hass_data(hass).get("device_store")
        latest = ""
        cached = await hass.async_add_executor_job(load_cached_firmware, _firmware_dir(hass))
        if cached is not None:
            latest = cached.version
        return web.json_response(devices_payload(store, latest) if store is not None else {})


class DevicesApproveView(HomeAssistantView):
    """POST /api/home_intercom/devices/approve — pending → active (issue #51).

    Same PWA token as GET /devices. Body: ``{"mac": "AA:BB:..."}``.
    """

    url = "/api/home_intercom/devices/approve"
    name = "api:home_intercom:devices-approve"
    requires_auth = False  # auth via X-PWA-Token header

    async def post(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="DevicesApproveView")
        if denied is not None:
            return denied
        hass = request.app["hass"]
        store = _get_hass_data(hass).get("device_store")
        if store is None:
            return web.json_response(
                {"ok": False, "error": "device registry unavailable"}, status=500
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        mac = body.get("mac", "") if isinstance(body, dict) else ""
        if not mac:
            return web.json_response({"ok": False, "error": "missing mac"}, status=400)
        device = await store.approve(mac)
        if device is None:
            return web.json_response({"ok": False, "error": "unknown device"}, status=404)
        return web.json_response({"ok": True, "pending": False})


class DevicesManageView(HomeAssistantView):
    """POST /api/home_intercom/devices/manage — PWA device actions.

    Body: ``{"mac": "AA:BB:...", "action": "approve"|"deapprove"|"revoke"|"unrevoke"|"delete"|"ota"|"ota_cancel"|"buttons"}``.
    """

    url = "/api/home_intercom/devices/manage"
    name = "api:home_intercom:devices-manage"
    requires_auth = False  # auth via X-PWA-Token header

    async def post(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="DevicesManageView")
        if denied is not None:
            return denied
        hass = request.app["hass"]
        store = _get_hass_data(hass).get("device_store")
        if store is None:
            return web.json_response(
                {"ok": False, "error": "device registry unavailable"}, status=500
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        parsed = parse_device_manage_body(body)
        if isinstance(parsed, str):
            return web.json_response({"ok": False, "error": parsed}, status=400)
        mac, action = parsed

        if action == "delete":
            if store.get(mac) is None:
                return web.json_response({"ok": False, "error": "unknown device"}, status=404)
            await store.remove(mac)
            _remove_button_ha_device(hass, mac)
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            async_dispatcher_send(hass, f"{DOMAIN}_device_store_changed")
            return web.json_response({"ok": True, "deleted": True})

        if action == "ota":
            reason = device_ota_reject_reason(store.get(mac))
            if reason == "unknown device":
                return web.json_response({"ok": False, "error": reason}, status=404)
            if reason:
                return web.json_response({"ok": False, "error": reason}, status=400)
            try:
                cached = await hass.async_add_executor_job(
                    ensure_latest_firmware, _firmware_dir(hass)
                )
            except FirmwareError as exc:
                _LOGGER.warning("OTA firmware fetch failed: %s", exc)
                return web.json_response({"ok": False, "error": "firmware unavailable"}, status=502)
            updated = await store.request_ota(mac, cached.version)
            if updated is None:
                return web.json_response({"ok": False, "error": "unknown device"}, status=404)
            return web.json_response({"ok": True, "target_version": cached.version})

        if action == "ota_cancel":
            device = await store.cancel_ota(mac)
            if device is None:
                return web.json_response({"ok": False, "error": "unknown device"}, status=404)
            return web.json_response({"ok": True, "ota_requested": False})

        if action == "buttons":
            rooms = _get_hass_data(hass).get("rooms") or {}
            mapping = buttons_from_manage_body(body, set(rooms))
            if isinstance(mapping, str):
                return web.json_response({"ok": False, "error": mapping}, status=400)
            device = await store.update_field(mac, "buttons", mapping)
            if device is None:
                return web.json_response({"ok": False, "error": "unknown device"}, status=404)
            return web.json_response({"ok": True, "buttons": device.get("buttons") or {}})

        if action == "approve":
            device = await store.approve(mac)
        elif action == "deapprove":
            device = await store.update_field(mac, "pending", True)
        elif action == "revoke":
            device = await store.revoke(mac)
        else:
            device = await store.update_field(mac, "revoked", False)

        if device is None:
            return web.json_response({"ok": False, "error": "unknown device"}, status=404)
        return web.json_response(
            {
                "ok": True,
                "pending": bool(device.get("pending")),
                "revoked": bool(device.get("revoked")),
            }
        )


class PanelView(HomeAssistantView):
    """GET /home_intercom — PWA frontend HTML (legacy underscore path)."""

    url = PANEL_PATH_LEGACY
    name = "home_intercom:panel"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return await _serve_panel(request)


class PanelAliasView(HomeAssistantView):
    """GET /home-intercom — PWA frontend HTML (hyphen path for HA sidebar/dashboard)."""

    url = PANEL_PATH
    name = "home_intercom:panel-hyphen"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return await _serve_panel(request)


_STATIC_MIME_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".wav": "audio/wav",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


async def _serve_static(request: web.Request, filename: str) -> web.Response:
    """Serve a file from the integration static directory."""
    static_dir = _INTEGRATION_DIR / "static"

    if ".." in filename or filename.startswith("/"):
        return web.Response(status=404)

    filepath = static_dir / filename
    if not filepath.is_file():
        return web.Response(status=404)

    content_type = _STATIC_MIME_TYPES.get(filepath.suffix, "application/octet-stream")
    body = await request.app["hass"].async_add_executor_job(filepath.read_bytes)
    return web.Response(
        body=body,
        content_type=content_type,
    )


class FirmwareStatusView(HomeAssistantView):
    """GET /api/home_intercom/firmware/status — cached GitHub version for the PWA."""

    url = "/api/home_intercom/firmware/status"
    name = "api:home_intercom:firmware-status"
    requires_auth = False  # public: version string only, same as /version

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        return web.json_response(firmware_cache_status(_firmware_dir(hass)))


class FirmwareSyncView(HomeAssistantView):
    """POST /api/home_intercom/firmware/sync — fetch latest GitHub release into cache."""

    url = "/api/home_intercom/firmware/sync"
    name = "api:home_intercom:firmware-sync"
    requires_auth = False  # auth via X-PWA-Token header

    async def post(self, request: web.Request) -> web.Response:
        denied = _verify_pwa_token(request, view="FirmwareSyncView")
        if denied is not None:
            return denied
        hass = request.app["hass"]
        try:
            cached, updated = await hass.async_add_executor_job(
                sync_firmware_cache, _firmware_dir(hass)
            )
        except FirmwareError as exc:
            _LOGGER.warning("firmware sync failed: %s", exc)
            return web.json_response({"ok": False, "error": "firmware unavailable"}, status=502)
        return web.json_response({"ok": True, "version": cached.version, "updated": updated})


class FirmwareView(HomeAssistantView):
    """GET /api/home_intercom/firmware — cached GitHub .bin for ESP32 OTA."""

    url = "/api/home_intercom/firmware"
    name = "api:home_intercom:firmware"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        cached = load_cached_firmware(_firmware_dir(hass))
        if cached is None:
            return web.Response(status=404)
        body = await hass.async_add_executor_job(Path(cached.bin_path).read_bytes)
        return web.Response(
            body=body,
            content_type="application/octet-stream",
            headers=firmware_checksum_headers(cached.sha256),
        )


class FirmwareSigView(HomeAssistantView):
    """GET /api/home_intercom/firmware.sig — optional ECDSA signature bytes."""

    url = "/api/home_intercom/firmware.sig"
    name = "api:home_intercom:firmware-sig"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        cached = load_cached_firmware(_firmware_dir(hass))
        if cached is None or not cached.sig_path:
            return web.Response(status=404)
        body = await hass.async_add_executor_job(Path(cached.sig_path).read_bytes)
        return web.Response(body=body, content_type="application/octet-stream")


class StaticView(HomeAssistantView):
    """Serve static assets under the legacy panel path."""

    url = f"{PANEL_PATH_LEGACY}/static/{{filename}}"
    name = "home_intercom:static"
    requires_auth = False

    async def get(self, request: web.Request, filename: str) -> web.Response:
        return await _serve_static(request, filename)


class StaticAliasView(HomeAssistantView):
    """Serve static assets under the hyphen panel path."""

    url = f"{PANEL_PATH}/static/{{filename}}"
    name = "home_intercom:static-hyphen"
    requires_auth = False

    async def get(self, request: web.Request, filename: str) -> web.Response:
        return await _serve_static(request, filename)


def register_api_views(hass: HomeAssistant) -> None:
    """Register all HomeAssistantView endpoints."""
    hass.http.register_view(RecordView)
    hass.http.register_view(ChimeView)
    hass.http.register_view(DeviceRecordView)
    hass.http.register_view(StatusView)
    hass.http.register_view(VersionView)
    hass.http.register_view(ConfigView)
    hass.http.register_view(RoomsView)
    hass.http.register_view(RoomsOrderView)
    hass.http.register_view(RoomsItemView)
    hass.http.register_view(MediaPlayersView)
    hass.http.register_view(DevicesHelloView)
    hass.http.register_view(DevicesApproveView)
    hass.http.register_view(DevicesManageView)
    hass.http.register_view(DevicesView)
    hass.http.register_view(FirmwareStatusView)
    hass.http.register_view(FirmwareSyncView)
    hass.http.register_view(FirmwareView)
    hass.http.register_view(FirmwareSigView)
    hass.http.register_view(PanelView)
    hass.http.register_view(PanelAliasView)
    hass.http.register_view(StaticView)
    hass.http.register_view(StaticAliasView)
