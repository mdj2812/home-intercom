"""HomeAssistantView endpoints for Home Intercom.

Maps the Flask routes from intercom_server.py to HomeAssistantView:
  /record        → RecordView  (POST audio → WAV → play)
  /rooms/status  → StatusView  (GET speaker online status)
  /version       → VersionView (GET version + pcm_rate)
  /rooms         → RoomsView   (GET rooms.json)
  /audio/<path>  → AudioView   (GET recorded WAV files)
  /panel         → PanelView   (GET PWA frontend HTML)
"""

from __future__ import annotations

import json
import logging
import os
import wave
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PCM_BPS, PCM_RATE, WAV_HEADER_SIZE, WAV_MAGIC
from .player import play_announcement

_LOGGER = logging.getLogger(__name__)

# Path to the integration directory (where PWA frontend and static/ live)
_INTEGRATION_DIR = Path(__file__).parent


def _concat_wavs(chime_path: str, audio_path: str, output_path: str) -> float:
    """Prepend chime WAV to audio WAV. Returns total duration (seconds)."""
    import shutil

    with wave.open(chime_path, "rb") as wf_chime:
        chime_rate = wf_chime.getframerate()
        chime_frames = wf_chime.readframes(wf_chime.getnframes())
        chime_width = wf_chime.getsampwidth()
        chime_channels = wf_chime.getnchannels()

    with wave.open(audio_path, "rb") as wf_audio:
        audio_rate = wf_audio.getframerate()
        audio_frames = wf_audio.readframes(wf_audio.getnframes())
        audio_width = wf_audio.getsampwidth()
        audio_channels = wf_audio.getnchannels()

    if (chime_rate, chime_width, chime_channels) != (audio_rate, audio_width, audio_channels):
        _LOGGER.warning(
            "chime/audio format mismatch (chime=%dHz/%dB/%dch, audio=%dHz/%dB/%dch) — skipping chime",
            chime_rate, chime_width, chime_channels,
            audio_rate, audio_width, audio_channels,
        )
        shutil.copy2(audio_path, output_path)
        return len(audio_frames) / (audio_rate * audio_width)

    total_frames = (len(chime_frames) + len(audio_frames)) // audio_width
    duration = total_frames / audio_rate

    with wave.open(output_path, "wb") as wf_out:
        wf_out.setnchannels(audio_channels)
        wf_out.setsampwidth(audio_width)
        wf_out.setframerate(audio_rate)
        wf_out.writeframes(chime_frames + audio_frames)

    _LOGGER.info("chime prepended (%.1fs total)", duration)
    return duration


def _get_hass_data(hass: HomeAssistant) -> dict:
    """Get integration data dict — guaranteed to exist after async_setup_entry."""
    return hass.data.get(DOMAIN, {})


def _handle_pcm_to_wav(data: bytes, rate: int, filepath: str) -> float:
    """Raw 16-bit mono PCM → write WAV file. Returns duration (seconds)."""
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(PCM_BPS)
        wf.setframerate(rate)
        wf.writeframes(data)
    duration = len(data) / (rate * PCM_BPS)
    file_size = os.path.getsize(filepath)
    _LOGGER.info(
        "WAV written: %s (%dB, %.1fs, %dHz)",
        os.path.basename(filepath),
        file_size,
        duration,
        rate,
    )
    return duration


def _handle_wav_passthrough(data: bytes, filepath: str) -> tuple[int, float]:
    """ESP32 hardware button → complete WAV, write as-is.

    Returns (sample_rate, duration_seconds).
    """
    with open(filepath, "wb") as f:
        f.write(data)
    with wave.open(filepath, "rb") as wf:
        rate = wf.getframerate()
        duration = wf.getnframes() / rate
    _LOGGER.info(
        "WAV passthrough %dB, %dHz, %dch, %dbit, %.1fs",
        len(data),
        rate,
        wf.getnchannels(),
        wf.getsampwidth() * 8,
        duration,
    )
    return rate, duration


class RecordView(HomeAssistantView):
    """POST /api/home_intercom/record — receive audio → write WAV → play.

    Replaces Flask's @app.route("/record", methods=["POST"]).
    """

    url = "/api/home_intercom/record"
    name = "api:home_intercom:record"
    requires_auth = False  # PWA is served from HA domain, inherits HA auth

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        data = await request.read()
        target = request.query.get("target", "")

        if not target:
            return web.json_response({"ok": False, "error": "missing target"}, status=400)

        room_map = _get_hass_data(hass).get("rooms", {})

        if target == "all":
            targets = [(k, v) for k, v in room_map.items() if v.get("entity_id")]
            if not targets:
                return web.json_response(
                    {"ok": False, "error": "no rooms configured"}, status=500
                )
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

        if data[: len(WAV_MAGIC)] == WAV_MAGIC:
            rate, duration = await hass.async_add_executor_job(
                _handle_wav_passthrough, data, filepath
            )
        else:
            rate_obj = int(request.query.get("rate", PCM_RATE))
            duration = await hass.async_add_executor_job(
                _handle_pcm_to_wav, data, rate_obj, filepath
            )

        # Build public URL — served from HA's own domain at /local/home_intercom_audio/
        audio_url = f"/local/home_intercom_audio/{filename}"

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
                audio_url_with_chime = f"/local/home_intercom_audio/{filename_chime}"
            except Exception as exc:
                _LOGGER.warning("Failed to prepend chime: %s", exc)

        # Play on each target room
        ok_count = 0
        errors: list[dict] = []
        for _tgt_key, tgt_room in targets:
            announce_volume = tgt_room.get("announce_volume")
            result = await play_announcement(
                hass,
                tgt_room["entity_id"],
                audio_url,
                duration if not duration_with_chime else 0,
                announce_volume=announce_volume,
                audio_url_with_chime=audio_url_with_chime,
                duration_with_chime=duration_with_chime,
            )
            if result.ok:
                ok_count += 1
            else:
                errors.append(
                    {"entity_id": tgt_room["entity_id"], "error": result.error or "unknown"}
                )

        name = room_map[target]["name"] if target != "all" else "全部"
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
            entity = room.get("entity_id", "")
            if not entity:
                status[key] = "online"
                continue
            state = hass.states.get(entity)
            if not state or state.state == "unavailable":
                status[key] = "unavailable"
                continue
            # Check supported_features
            attrs = state.attributes
            supported = attrs.get("supported_features", 0)
            if supported & (1 << 9):  # SUPPORT_PLAY_MEDIA
                status[key] = "online"
            else:
                status[key] = "no_play_media"

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

        return web.json_response({"version": version, "pcm_rate": PCM_RATE})


class RoomsView(HomeAssistantView):
    """GET /api/home_intercom/rooms — room configuration."""

    url = "/api/home_intercom/rooms"
    name = "api:home_intercom:rooms"
    requires_auth = False  # public: room names only, no secrets

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(_get_hass_data(request.app["hass"]).get("rooms", {}))


class PanelView(HomeAssistantView):
    """GET /home_intercom/panel — PWA frontend HTML.

    Served at the panel path registered via async_register_built_in_panel.
    """

    url = "/home_intercom/panel"
    name = "home_intercom:panel"
    requires_auth = False  # HTML page only — API endpoints still require auth

    async def get(self, request: web.Request) -> web.Response:
        html_path = _INTEGRATION_DIR / "intercom.html"
        try:
            html = await request.app["hass"].async_add_executor_job(
                html_path.read_text, encoding="utf-8"
            )
        except FileNotFoundError:
            return web.Response(
                text="<h1>Home Intercom</h1><p>Frontend not found</p>",
                content_type="text/html",
            )

        # Rewrite static asset paths for HA panel context.
        # JS handles API paths via window.API_BASE detection,
        # but <link>/<script> in <head> load before JS runs.
        html = html.replace('src="/static/', 'src="/home_intercom/static/')
        html = html.replace('href="/static/', 'href="/home_intercom/static/')

        return web.Response(text=html, content_type="text/html")


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
    hass.http.register_view(StatusView)
    hass.http.register_view(VersionView)
    hass.http.register_view(RoomsView)
    hass.http.register_view(PanelView)
    hass.http.register_view(StaticView)
