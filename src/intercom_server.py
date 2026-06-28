#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend."""

import json
import os
import sys
import wave

from flask import Flask, jsonify, request, send_from_directory

from ha_client import HAClient

app = Flask(__name__)

HA_URL = os.environ.get("HA_URL", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

haclient = HAClient(HA_URL, HA_TOKEN)

PCM_RATE = 16000  # target sample rate (Hz) for Xiaomi speaker WAV output
PCM_BPS = 2  # 16-bit audio = 2 bytes per sample
WAV_MAGIC = b"RIFF"
WAV_HEADER_SIZE = 44  # RIFF(12) + fmt(24) + data(8) = minimum valid WAV header

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


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "intercom.html")


@app.route("/rooms.json")
def rooms():
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
    return jsonify({"version": VERSION, "pcm_rate": PCM_RATE})


def _handle_wav_passthrough(data, filepath):
    """ESP32 hardware button → complete WAV file, write as-is.

    Returns (sample_rate, duration_seconds).
    """
    with open(filepath, "wb") as f:
        f.write(data)
    with wave.open(filepath, "rb") as wf:
        rate = wf.getframerate()
        nframes = wf.getnframes()
        duration = nframes / rate
    print(
        f"[intercom] WAV passthrough {len(data)}B, "
        f"{rate}Hz, {wf.getnchannels()}ch, {wf.getsampwidth() * 8}bit, {duration:.1f}s"
    )
    return rate, duration


def _handle_pcm_to_wav(data, rate, filepath):
    """Raw 16-bit mono PCM → write WAV file with correct header.

    Returns duration_seconds.
    """
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(PCM_BPS)
        wf.setframerate(rate)
        wf.writeframes(data)
    duration = len(data) / (rate * PCM_BPS)
    file_size = os.path.getsize(filepath)
    print(
        f"[intercom] WAV written: {os.path.basename(filepath)} "
        f"({file_size}B, {duration:.1f}s, {rate}Hz)"
    )
    return duration


@app.route("/record", methods=["POST"])
def record():
    """Receive audio → write WAV → HA playback.

    Supports two input formats:
    - Raw PCM (PWA): body is 16-bit mono PCM, wrapped into WAV
    - WAV passthrough (ESP32): body is a complete WAV file, written as-is
    """
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

    if data[: len(WAV_MAGIC)] == WAV_MAGIC:
        _rate, duration = _handle_wav_passthrough(data, filepath)
    else:
        rate = int(request.args.get("rate", PCM_RATE))
        duration = _handle_pcm_to_wav(data, rate, filepath)

    # Build public URL — PUBLIC_URL env for reverse proxy (e.g. Caddy),
    # fall back to X-Forwarded-Proto / request.host for direct access.
    public_base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    base = public_base or f"{scheme}://{request.host}"
    audio_url = f"{base}/audio/{filename}"

    ok_count = 0
    for _tgt_key, tgt_room in targets:
        if haclient.play_and_auto_pause(tgt_room["entity"], audio_url, duration):
            ok_count += 1

    name = ROOM_MAP[target]["name"] if target != "all" else "全部"
    print(f"[intercom] played on {ok_count}/{len(targets)} rooms for {name}")
    return jsonify({"ok": True, "name": name, "rooms_sent": ok_count, "url": audio_url})


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
