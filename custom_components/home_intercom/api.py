"""HomeAssistantView endpoints for Home Intercom.

Maps the Flask routes from intercom_server.py to HomeAssistantView:
  /record        → RecordView        (PWA token; POST audio → WAV → play)
  /device/record → DeviceRecordView  (HA auth; POST audio → WAV → play)
  /rooms/status  → StatusView  (GET speaker online status)
  /version       → VersionView (GET version only)
  /rooms         → RoomsView   (GET room config)
  /audio/<path>  → AudioView   (GET recorded WAV files)
  /              → PanelView   (GET PWA frontend HTML)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PCM_RATE, WAV_HEADER_SIZE
from .player import play_announcement
from .shared import concat_wavs as _concat_wavs
from .shared import config_payload, device_hello_payload, device_record_auth_error, is_wav
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

    # Chime prepend (same logic as Flask version)
    chime_path = str(_INTEGRATION_DIR / "static" / "pre_announce.wav")
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


class RecordView(HomeAssistantView):
    """POST /api/home_intercom/record using the PWA shared token."""

    url = "/api/home_intercom/record"
    name = "api:home_intercom:record"
    requires_auth = False  # auth via X-PWA-Token header

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]

        # Verify shared secret token (injected into PWA HTML by PanelView)
        pwa_token = hass.data.get(DOMAIN, {}).get("pwa_token", "")
        if pwa_token and request.headers.get("X-PWA-Token") != pwa_token:
            _LOGGER.warning("RecordView: invalid or missing X-PWA-Token")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        return await _handle_record(request)


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


class DevicesHelloView(HomeAssistantView):
    """POST /api/home_intercom/devices/hello — ESP32 boot registration (issue #37).

    Trust-on-first-use: unknown MACs auto-register with a default name.
    Revoked devices are rejected. No secrets on the device — it identifies
    by MAC address only.
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

        store = _get_hass_data(hass).get("device_store")
        if store is None:
            return web.json_response(
                {"status": "error", "error": "device registry unavailable"}, status=500
            )

        existing = store.get(mac)
        if existing and existing.get("revoked"):
            _LOGGER.warning("hello from revoked device %s — rejected", mac)
            return web.json_response({"status": "error", "error": "device revoked"}, status=403)

        is_new = existing is None  # before register_or_update
        try:
            device = await store.register_or_update(mac, firmware_version)
        except ValueError:
            return web.json_response(
                {"status": "error", "error": "invalid X-Device-ID (MAC)"}, status=400
            )

        # New device → reload to register HA device + entities (issue #48)
        if is_new:
            button_entry_id = _get_hass_data(hass).get("button_entry_id")
            if button_entry_id:
                _LOGGER.info("New device %s — reloading button entry %s", mac, button_entry_id)
                hass.async_create_task(hass.config_entries.async_reload(button_entry_id))
            else:
                _LOGGER.warning(
                    "New device %s but no button entry — entities won't appear until restart", mac
                )

        return web.json_response(device_hello_payload(device))


class PanelView(HomeAssistantView):
    """GET /home_intercom — PWA frontend HTML.

    Serves the intercom.html PWA page with static asset paths rewritten
    for the HA panel context (/home_intercom/static/...).
    """

    url = "/home_intercom"
    name = "home_intercom:panel"
    requires_auth = False  # HTML page only — API endpoints still require auth

    async def get(self, request: web.Request) -> web.Response:
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

        # Rewrite static asset paths for HA panel context.
        # JS handles API paths via window.API_BASE detection,
        # but <link>/<script> in <head> load before JS runs.
        html = html.replace('src="/static/', 'src="/home_intercom/static/')
        html = html.replace('href="/static/', 'href="/home_intercom/static/')

        # Inject server-generated API token for Companion App compatibility.
        # Web browsers use localStorage.hassTokens; Companion App WebView
        # uses OAuth and doesn't populate localStorage — server-side injection
        # is the only reliable way to get a token into the PWA.
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


class StaticView(HomeAssistantView):
    """Serve static assets (CSS, JS, icons, manifest) for the PWA.

    Replaces async_register_static_paths which has API compatibility issues
    across HA versions.
    """

    url = "/home_intercom/static/{filename}"
    name = "home_intercom:static"
    requires_auth = False

    _MIME_TYPES = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".wav": "audio/wav",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
    }

    async def get(self, request: web.Request, filename: str) -> web.Response:
        static_dir = _INTEGRATION_DIR / "static"

        # Security: prevent path traversal
        if ".." in filename or filename.startswith("/"):
            return web.Response(status=404)

        filepath = static_dir / filename
        if not filepath.is_file():
            return web.Response(status=404)

        content_type = self._MIME_TYPES.get(filepath.suffix, "application/octet-stream")
        body = await request.app["hass"].async_add_executor_job(filepath.read_bytes)
        return web.Response(
            body=body,
            content_type=content_type,
        )


def register_api_views(hass: HomeAssistant) -> None:
    """Register all HomeAssistantView endpoints."""
    hass.http.register_view(RecordView)
    hass.http.register_view(DeviceRecordView)
    hass.http.register_view(StatusView)
    hass.http.register_view(VersionView)
    hass.http.register_view(ConfigView)
    hass.http.register_view(RoomsView)
    hass.http.register_view(DevicesHelloView)
    hass.http.register_view(PanelView)
    hass.http.register_view(StaticView)
