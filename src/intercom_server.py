#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend."""

import os
import sys
from pathlib import Path

from const import (
    DEVICE_REGISTRY_DEFAULT_PATH,
    FIRMWARE_DIR_DEFAULT,
    PCM_RATE,
    ROOMS_STORE_DEFAULT,
    WAV_HEADER_SIZE,
)
from firmware import (
    FirmwareError,
    ensure_latest_firmware,
    firmware_checksum_headers,
    load_cached_firmware,
    schedule_firmware_refresh,
    start_firmware_poller,
)
from flask import Flask, jsonify, request, send_from_directory
from rooms import (
    RoomValidationError,
    load_rooms,
    patch_room,
    put_room,
    room_entity,
    save_rooms,
    validate_room_key,
)
from shared import (
    buttons_from_manage_body,
    chime_public_url,
    chime_status_payload,
    concat_wavs,
    config_payload,
    delete_custom_chime,
    device_hello_payload,
    device_ota_reject_reason,
    device_record_auth_error,
    devices_payload,
    handle_pcm_to_wav,
    handle_wav_passthrough,
    is_wav,
    parse_device_manage_body,
    pins_from_hello_body,
    resolve_chime_wav,
    wait_for_pending_hello,
    write_custom_chime_wav,
)

from device_store import DeviceStore
from ha_client import DEFAULT_STATE_TIMEOUT, HAClient

app = Flask(__name__)

HA_URL = os.environ.get("HA_URL", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
FIRMWARE_DIR = os.environ.get("FIRMWARE_DIR", FIRMWARE_DIR_DEFAULT)


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

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
CHIME_WAV = os.path.join(_APP_DIR, "static", "pre_announce.wav")

# ——— Version ———
try:
    with open("/app/.docker-image") as f:
        VERSION = f.read().strip().split(":")[-1]
except Exception:
    VERSION = os.environ.get("VERSION", "dev")

# Writable room catalog (#72). Empty until rooms are added in the PWA.
ROOMS_STORE = os.environ.get("ROOMS_FILE", ROOMS_STORE_DEFAULT)
ROOM_MAP = load_rooms(ROOMS_STORE)

# Device registry for ESP32 intercom buttons (issue #40)
DEVICE_REGISTRY_FILE = os.environ.get("DEVICE_REGISTRY_FILE", DEVICE_REGISTRY_DEFAULT_PATH)
device_store = DeviceStore(DEVICE_REGISTRY_FILE)


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "intercom.html")


@app.route("/rooms")
def rooms():
    """Public room map (issue #38). PWA-managed catalog on /data/rooms.json."""
    return jsonify(ROOM_MAP)


def _persist_room_map():
    """Write ROOM_MAP to the Docker store. Raises OSError if the path is not writable."""
    save_rooms(ROOMS_STORE, ROOM_MAP)


@app.route("/rooms/<room_id>", methods=["PUT", "PATCH", "DELETE"])
def rooms_item(room_id):
    """Create, update, or delete one room. LAN trust, same as /chime POST (#72)."""
    try:
        key = validate_room_key(room_id)
    except RoomValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if request.method == "DELETE":
        if key not in ROOM_MAP:
            return jsonify({"ok": False, "error": "unknown room"}), 404
        removed = ROOM_MAP.pop(key)
        try:
            _persist_room_map()
        except OSError:
            ROOM_MAP[key] = removed
            return jsonify({"ok": False, "error": "cannot persist rooms"}), 500
        return jsonify({"ok": True, "rooms": ROOM_MAP})

    body = request.get_json(silent=True)
    try:
        if request.method == "PUT":
            room = put_room(body, entity_key="entity")
        elif key not in ROOM_MAP:
            return jsonify({"ok": False, "error": "unknown room"}), 404
        else:
            room = patch_room(ROOM_MAP[key], body, entity_key="entity")
    except RoomValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    previous = ROOM_MAP.get(key)
    ROOM_MAP[key] = room
    try:
        _persist_room_map()
    except OSError:
        if previous is None:
            ROOM_MAP.pop(key, None)
        else:
            ROOM_MAP[key] = previous
        return jsonify({"ok": False, "error": "cannot persist rooms"}), 500
    return jsonify({"ok": True, "rooms": ROOM_MAP})


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), filename
    )


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


def _request_base_url() -> str:
    public_base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return public_base or f"{scheme}://{request.host}"


@app.route("/chime", methods=["GET", "POST", "DELETE"])
def chime():
    """Custom pre-announce chime upload/reset (#66). POST/DELETE: LAN trust (same as /record)."""
    base = _request_base_url()

    if request.method == "GET":
        return jsonify(
            chime_status_payload(base_url=base, audio_dir=AUDIO_DIR, deployment="docker")
        )

    data = request.get_data()
    if request.method == "POST":
        if len(data) < WAV_HEADER_SIZE:
            return jsonify({"ok": False, "error": "no audio data"}), 400
        try:
            write_custom_chime_wav(data, AUDIO_DIR)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        url = chime_public_url(base, audio_dir=AUDIO_DIR, deployment="docker")
        return jsonify({"ok": True, "custom": True, "url": url})

    delete_custom_chime(AUDIO_DIR)
    return jsonify({"ok": True, "custom": False})


@app.route("/rooms/status")
def rooms_status():
    """Query Xiaomi speaker online status from HA."""
    if not HA_TOKEN:
        return jsonify({"error": "no HA_TOKEN"}), 500
    return jsonify(haclient.query_statuses(ROOM_MAP))


@app.route("/media_players")
def media_players():
    """HA media_player entities that support play_media (issue #73). Public like GET /rooms."""
    return jsonify(haclient.media_player_catalog())


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
    cached = load_cached_firmware(FIRMWARE_DIR)
    latest = cached.version if cached is not None else ""
    return jsonify(devices_payload(device_store, latest))


@app.route("/devices/approve", methods=["POST"])
def devices_approve():
    """Approve a pending intercom button (issue #51). LAN trust, same as /chime POST."""
    body = request.get_json(silent=True) or {}
    mac = body.get("mac", "") if isinstance(body, dict) else ""
    if not mac:
        return jsonify({"ok": False, "error": "missing mac"}), 400
    device = device_store.approve(mac)
    if device is None:
        return jsonify({"ok": False, "error": "unknown device"}), 404
    return jsonify({"ok": True, "pending": False})


@app.route("/devices/manage", methods=["POST"])
def devices_manage():
    """Approve, deapprove, revoke, unrevoke, delete, OTA, cancel OTA, or GPIO map a button. LAN trust."""
    body = request.get_json(silent=True) or {}
    parsed = parse_device_manage_body(body)
    if isinstance(parsed, str):
        return jsonify({"ok": False, "error": parsed}), 400
    mac, action = parsed

    if action == "delete":
        # Store-only: Docker has no HA device registry. HA delete also
        # removes the native device card (DevicesManageView).
        if device_store.get(mac) is None:
            return jsonify({"ok": False, "error": "unknown device"}), 404
        device_store.remove(mac)
        return jsonify({"ok": True, "deleted": True})

    if action == "ota":
        reason = device_ota_reject_reason(device_store.get(mac))
        if reason == "unknown device":
            return jsonify({"ok": False, "error": reason}), 404
        if reason:
            return jsonify({"ok": False, "error": reason}), 400
        try:
            cached = ensure_latest_firmware(FIRMWARE_DIR)
        except FirmwareError as exc:
            app.logger.warning("[intercom] OTA firmware fetch failed: %s", exc)
            return jsonify({"ok": False, "error": "firmware unavailable"}), 502
        updated = device_store.request_ota(mac, cached.version)
        if updated is None:
            return jsonify({"ok": False, "error": "unknown device"}), 404
        return jsonify({"ok": True, "target_version": cached.version})

    if action == "ota_cancel":
        device = device_store.cancel_ota(mac)
        if device is None:
            return jsonify({"ok": False, "error": "unknown device"}), 404
        return jsonify({"ok": True, "ota_requested": False})

    if action == "buttons":
        mapping = buttons_from_manage_body(body, set(ROOM_MAP))
        if isinstance(mapping, str):
            return jsonify({"ok": False, "error": mapping}), 400
        device = device_store.update_field(mac, "buttons", mapping)
        if device is None:
            return jsonify({"ok": False, "error": "unknown device"}), 404
        return jsonify({"ok": True, "buttons": device.get("buttons") or {}})

    if action == "approve":
        device = device_store.approve(mac)
    elif action == "deapprove":
        device = device_store.update_field(mac, "pending", True)
    elif action == "revoke":
        device = device_store.revoke(mac)
    else:
        device = device_store.update_field(mac, "revoked", False)

    if device is None:
        return jsonify({"ok": False, "error": "unknown device"}), 404
    return jsonify(
        {
            "ok": True,
            "pending": bool(device.get("pending")),
            "revoked": bool(device.get("revoked")),
        }
    )


@app.route("/record", methods=["POST"])
def record():
    """Receive audio → write WAV → prepend chime → HA playback.

    Supports two input formats:
    - Raw PCM (PWA): body is 16-bit mono PCM, wrapped into WAV
    - WAV passthrough (ESP32): body is a complete WAV file, written as-is

    Auth (issue #47, #51): when X-Device-ID is present the MAC must be
    registered, approved, and not revoked. Without the header the route
    stays open for the PWA (LAN trust), same as before.
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
        targets = [(k, v) for k, v in ROOM_MAP.items() if room_entity(v)]
        if not targets:
            return jsonify({"ok": False, "error": "no rooms configured"}), 500
    else:
        room = ROOM_MAP.get(target)
        if not room or not room_entity(room):
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
    chime_path = str(resolve_chime_wav(integration_dir=Path(_APP_DIR), audio_dir=AUDIO_DIR))
    filename_chime = f"intercom_{target}_chime.wav"
    filepath_chime = os.path.join(AUDIO_DIR, filename_chime)
    duration_with_chime = concat_wavs(chime_path, filepath, filepath_chime)

    # Build public URLs
    base = _request_base_url()
    audio_url = f"{base}/audio/{filename}"
    audio_url_with_chime = f"{base}/audio/{filename_chime}"
    chime_url = chime_public_url(base, audio_dir=AUDIO_DIR, deployment="docker")

    ok_count = 0
    errors = []
    for _tgt_key, tgt_room in targets:
        entity = room_entity(tgt_room)
        announce_volume = tgt_room.get("announce_volume")
        result = haclient.play_announcement(
            entity,
            audio_url,
            duration,
            announce_volume=announce_volume,
            audio_url_with_chime=audio_url_with_chime,
            duration_with_chime=duration_with_chime,
            chime_url=chime_url,
            pause_buffer=tgt_room.get("pause_buffer"),
        )
        if result["ok"]:
            ok_count += 1
        else:
            errors.append({"entity": entity, "error": result.get("error", "unknown")})

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

    Trust-on-first-use: unknown MACs auto-register as pending (issue #51)
    until an admin approves. Revoked devices are rejected. No secrets on
    the device — MAC identity only.
    """
    mac = request.headers.get("X-Device-ID", "")
    if not mac:
        return jsonify({"status": "error", "error": "missing X-Device-ID header"}), 400

    body = request.get_json(silent=True) or {}
    firmware_version = body.get("firmware_version", "") if isinstance(body, dict) else ""
    pins = pins_from_hello_body(body)

    existing = device_store.get(mac)
    if existing and existing.get("revoked"):
        app.logger.warning(f"[intercom] hello from revoked device {mac} — rejected")
        return jsonify({"status": "error", "error": "device revoked"}), 403

    was_empty = not device_store.devices
    try:
        device = device_store.register_or_update(mac, firmware_version, pins=pins)
    except ValueError:
        return jsonify({"status": "error", "error": "invalid X-Device-ID (MAC)"}), 400
    if was_empty:
        schedule_firmware_refresh(FIRMWARE_DIR)

    device = wait_for_pending_hello(device_store.get, mac)
    if device is None:
        return jsonify({"status": "error", "error": "unknown device"}), 404
    if device.get("revoked"):
        return jsonify({"status": "error", "error": "device revoked"}), 403

    return jsonify(device_hello_payload(device, valid_rooms=set(ROOM_MAP)))


@app.route("/api/home_intercom/firmware")
def firmware_bin():
    """Cached GitHub .bin for ESP32 OTA (LAN HTTP)."""
    cached = load_cached_firmware(FIRMWARE_DIR)
    if cached is None:
        return ("", 404)
    resp = send_from_directory(
        FIRMWARE_DIR,
        os.path.basename(cached.bin_path),
        mimetype="application/octet-stream",
    )
    for key, value in firmware_checksum_headers(cached.sha256).items():
        resp.headers[key] = value
    return resp


@app.route("/api/home_intercom/firmware.sig")
def firmware_sig():
    """Optional ECDSA signature; 404 until CI publishes one."""
    cached = load_cached_firmware(FIRMWARE_DIR)
    if cached is None or not cached.sig_path:
        return ("", 404)
    return send_from_directory(
        FIRMWARE_DIR,
        os.path.basename(cached.sig_path),
        mimetype="application/octet-stream",
    )


# ── HA-compatible `/api/home_intercom/…` aliases ─────────────────────────
# Registered before __main__ so both `python intercom_server.py` and
# `gunicorn intercom_server:app` pick up the extra routes.
_HA_PREFIX = "/api/home_intercom"
app.add_url_rule(f"{_HA_PREFIX}/devices/hello", "ha_devices_hello", devices_hello, methods=["POST"])
app.add_url_rule(
    f"{_HA_PREFIX}/devices/approve", "ha_devices_approve", devices_approve, methods=["POST"]
)
app.add_url_rule(
    f"{_HA_PREFIX}/devices/manage", "ha_devices_manage", devices_manage, methods=["POST"]
)
app.add_url_rule(f"{_HA_PREFIX}/devices", "ha_devices", devices_list)
app.add_url_rule(f"{_HA_PREFIX}/rooms", "ha_rooms", rooms)
app.add_url_rule(
    f"{_HA_PREFIX}/rooms/<room_id>",
    "ha_rooms_item",
    rooms_item,
    methods=["PUT", "PATCH", "DELETE"],
)
app.add_url_rule(f"{_HA_PREFIX}/rooms/status", "ha_rooms_status", rooms_status)
app.add_url_rule(f"{_HA_PREFIX}/media_players", "ha_media_players", media_players)
app.add_url_rule(f"{_HA_PREFIX}/version", "ha_version", version)
app.add_url_rule(f"{_HA_PREFIX}/config", "ha_config", config)
app.add_url_rule(f"{_HA_PREFIX}/record", "ha_record", record, methods=["POST"])
# Firmware (intercom-button#31) posts here — same handler as /record (issue #70).
app.add_url_rule("/device/record", "device_record", record, methods=["POST"])
app.add_url_rule(f"{_HA_PREFIX}/device/record", "ha_device_record", record, methods=["POST"])
app.add_url_rule(f"{_HA_PREFIX}/chime", "ha_chime", chime, methods=["GET", "POST", "DELETE"])
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
    print(f"[intercom] Firmware dir: {FIRMWARE_DIR}", flush=True)
    print(f"[intercom] Rooms store: {ROOMS_STORE} ({len(ROOM_MAP)} rooms)", flush=True)
    print(f"[intercom] Trusted proxy: {trusted_proxy}", flush=True)
    print("[intercom] Starting on http://0.0.0.0:8764", flush=True)
    start_firmware_poller(FIRMWARE_DIR, should_run=lambda: bool(device_store.devices))
    serve(
        app,
        host="0.0.0.0",
        port=8764,
        trusted_proxy=trusted_proxy,
        trusted_proxy_headers={"x-forwarded-proto"},
    )
