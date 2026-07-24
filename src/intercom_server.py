#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend."""

import json
import os
import sys

from const import DEVICE_REGISTRY_DEFAULT_PATH, PCM_RATE, WAV_HEADER_SIZE
from flask import Flask, jsonify, request, send_from_directory
from shared import (
    concat_wavs,
    config_payload,
    device_hello_payload,
    device_record_auth_error,
    devices_payload,
    handle_pcm_to_wav,
    handle_wav_passthrough,
    is_wav,
)

from device_store import DeviceStore
from ha_client import DEFAULT_STATE_TIMEOUT, HAClient

app = Flask(__name__)

HA_URL = os.environ.get("HA_URL", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def _parse_pause_buffer() -> float:
    raw = os.environ.get("PAUSE_BUFFER", "0")
    try:
        return float(raw)
    except ValueError:
        app.logger.info(f"[intercom] invalid PAUSE_BUFFER '{raw}', using 0")
        return 0.0


PAUSE_BUFFER = _parse_pause_buffer()


def _parse_state_timeout() -> int:
    raw = os.environ.get("STATE_TIMEOUT", str(DEFAULT_STATE_TIMEOUT))
    try:
        val = int(raw)
        if val < 1:
            raise ValueError
        return val
    except ValueError:
        app.logger.warning(
            f"[intercom] invalid STATE_TIMEOUT '{raw}', using {DEFAULT_STATE_TIMEOUT}"
        )
        return DEFAULT_STATE_TIMEOUT


STATE_TIMEOUT = _parse_state_timeout()

haclient = HAClient(HA_URL, HA_TOKEN, pause_buffer=PAUSE_BUFFER, state_timeout=STATE_TIMEOUT)

CHIME_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pre_announce.wav")

# ——— Version ———
try:
    with open("/app/.docker-image") as f:
        VERSION = f.read().strip().split(":")[-1]
except Exception:
    VERSION = os.environ.get("VERSION", "dev")

# Load room config from rooms.json
ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
with open(ROOMS_FILE) as f:
    ROOM_MAP = json.load(f)

# Device registry for ESP32 intercom buttons (issue #40)
DEVICE_REGISTRY_FILE = os.environ.get("DEVICE_REGISTRY_FILE", DEVICE_REGISTRY_DEFAULT_PATH)
device_store = DeviceStore(DEVICE_REGISTRY_FILE)


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "intercom.html")


@app.route("/rooms.json")
def rooms_json():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "rooms.json")


@app.route("/rooms")
def rooms_alias():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "rooms.json")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), filename
    )


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


@app.route("/rooms/status")
def rooms_status():
    """Query Xiaomi speaker online status from HA."""
    if not HA_TOKEN:
        return jsonify({"error": "no HA_TOKEN"}), 500
    return jsonify(haclient.query_statuses(ROOM_MAP))


@app.route("/version")
def version():
    return jsonify({"version": VERSION})


@app.route("/config")
def config():
    """Global audio settings (issue #39) — public, same fields as the hello payload."""
    return jsonify(config_payload())


@app.route("/devices")
def devices_list():
    """Read-only registry listing for the PWA (issue #52). LAN trust, same as /record."""
    return jsonify(devices_payload(device_store))


@app.route("/record", methods=["POST"])
def record():
    """Receive audio → write WAV → prepend chime → HA playback.

    Supports two input formats:
    - Raw PCM (PWA): body is 16-bit mono PCM, wrapped into WAV
    - WAV passthrough (ESP32): body is a complete WAV file, written as-is

    Auth (issue #47): when X-Device-ID is present the MAC must be
    registered and not revoked. Without the header the route stays open
    for the PWA (LAN trust), same as before.
    """
    mac = request.headers.get("X-Device-ID", "")
    if mac:
        error = device_record_auth_error(device_store.get(mac))
        if error:
            app.logger.warning(f"[intercom] /record rejected for {mac}: {error}")
            return jsonify({"ok": False, "error": error}), 403

    target = request.args.get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "missing target"}), 400

    if target == "all":
        targets = [(k, v) for k, v in ROOM_MAP.items() if v.get("entity")]
        if not targets:
            return jsonify({"ok": False, "error": "no rooms configured"}), 500
    else:
        room = ROOM_MAP.get(target)
        if not room or not room.get("entity"):
            return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400
        targets = [(target, room)]

    data = request.get_data()
    if len(data) < WAV_HEADER_SIZE:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    filename = f"intercom_{target}.wav"
    filepath = os.path.join(AUDIO_DIR, filename)

    if is_wav(data):
        _rate, duration = handle_wav_passthrough(data, filepath)
    else:
        rate = int(request.args.get("rate", PCM_RATE))
        duration = handle_pcm_to_wav(data, rate, filepath)

    # Prepend chime — creates a copy with chime for standard players
    filename_chime = f"intercom_{target}_chime.wav"
    filepath_chime = os.path.join(AUDIO_DIR, filename_chime)
    duration_with_chime = concat_wavs(CHIME_WAV, filepath, filepath_chime)

    # Build public URLs
    public_base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    base = public_base or f"{scheme}://{request.host}"
    audio_url = f"{base}/audio/{filename}"
    audio_url_with_chime = f"{base}/audio/{filename_chime}"

    ok_count = 0
    errors = []
    for _tgt_key, tgt_room in targets:
        announce_volume = tgt_room.get("announce_volume")
        result = haclient.play_announcement(
            tgt_room["entity"],
            audio_url,
            duration,
            announce_volume=announce_volume,
            audio_url_with_chime=audio_url_with_chime,
            duration_with_chime=duration_with_chime,
        )
        if result["ok"]:
            ok_count += 1
        else:
            errors.append({"entity": tgt_room["entity"], "error": result.get("error", "unknown")})

    name = ROOM_MAP[target]["name"] if target != "all" else "全部"
    app.logger.info(f"[intercom] played on {ok_count}/{len(targets)} rooms for {name}")
    return jsonify(
        {
            "ok": True,
            "name": name,
            "rooms_sent": ok_count,
            "rooms_total": len(targets),
            "errors": errors or None,
            "url": audio_url,
        }
    )


@app.route("/devices/hello", methods=["POST"])
def devices_hello():
    """ESP32 boot registration + config delivery (issue #37).

    Trust-on-first-use: unknown MACs auto-register with a default name.
    Revoked devices are rejected. No secrets on the device — MAC identity only.
    """
    mac = request.headers.get("X-Device-ID", "")
    if not mac:
        return jsonify({"status": "error", "error": "missing X-Device-ID header"}), 400

    body = request.get_json(silent=True) or {}
    firmware_version = body.get("firmware_version", "") if isinstance(body, dict) else ""

    existing = device_store.get(mac)
    if existing and existing.get("revoked"):
        app.logger.warning(f"[intercom] hello from revoked device {mac} — rejected")
        return jsonify({"status": "error", "error": "device revoked"}), 403

    try:
        device = device_store.register_or_update(mac, firmware_version)
    except ValueError:
        return jsonify({"status": "error", "error": "invalid X-Device-ID (MAC)"}), 400

    return jsonify(device_hello_payload(device))


# ── HA-compatible `/api/home_intercom/…` aliases ─────────────────────────
# Registered before __main__ so both `python intercom_server.py` and
# `gunicorn intercom_server:app` pick up the extra routes.
_HA_PREFIX = "/api/home_intercom"
app.add_url_rule(f"{_HA_PREFIX}/devices/hello", "ha_devices_hello", devices_hello, methods=["POST"])
app.add_url_rule(f"{_HA_PREFIX}/devices", "ha_devices", devices_list)
app.add_url_rule(f"{_HA_PREFIX}/rooms", "ha_rooms", rooms_alias)
app.add_url_rule(f"{_HA_PREFIX}/rooms/status", "ha_rooms_status", rooms_status)
app.add_url_rule(f"{_HA_PREFIX}/version", "ha_version", version)
app.add_url_rule(f"{_HA_PREFIX}/config", "ha_config", config)
app.add_url_rule(f"{_HA_PREFIX}/record", "ha_record", record, methods=["POST"])
app.add_url_rule(f"{_HA_PREFIX}/audio/<path:filename>", "ha_audio", serve_audio)
app.add_url_rule(f"{_HA_PREFIX}/static/<path:filename>", "ha_static", static_files)


if __name__ == "__main__":
    import logging

    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="[intercom] %(message)s", stream=sys.stdout)

    # trusted_proxy: set via TRUSTED_PROXY env (default '*' for homelab)
    trusted_proxy = os.environ.get("TRUSTED_PROXY", "*")

    print(f"[intercom] HA URL: {HA_URL}", flush=True)
    print(f"[intercom] Audio dir: {AUDIO_DIR}", flush=True)
    print(f"[intercom] Trusted proxy: {trusted_proxy}", flush=True)
    print("[intercom] Starting on http://0.0.0.0:8764", flush=True)
    serve(
        app,
        host="0.0.0.0",
        port=8764,
        trusted_proxy=trusted_proxy,
        trusted_proxy_headers={"x-forwarded-proto"},
    )
