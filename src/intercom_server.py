#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend.

PWA POST audio → Flask convert → local serve → direct HA API playback.
"""

import json
import os
import shutil
import subprocess
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

# ——— Version ———
try:
    with open("/app/.docker-image") as f:
        VERSION = f.read().strip().split(":")[-1]
except Exception:
    VERSION = os.environ.get("VERSION", "dev")

# ——— Audio processing constants ———
WAV_MAGIC = b"RIFF"  # WAV file magic bytes
TMP_PREFIX = "/tmp/intercom_"  # temp file prefix
FFMPEG_SR = 16000  # ffmpeg output sample rate (Hz)
FFMPEG_BPS = 2  # s16le = 2 bytes/sample
FFMPEG_BYTERATE = FFMPEG_SR * FFMPEG_BPS  # 16000 Hz × 2 = 32000 B/s

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
    return jsonify({"version": VERSION})


def _handle_wav_passthrough(raw_audio, tmp_wav):
    """ESP32 hardware button → PCM WAV passthrough, parse header for duration."""
    with open(tmp_wav, "wb") as f:
        f.write(raw_audio)
    with wave.open(tmp_wav, "rb") as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
        sampwidth = wf.getsampwidth()
        duration = nframes / sr
    print(
        f"[intercom] WAV passthrough {len(raw_audio)} bytes, "
        f"{sr}Hz, {sampwidth * 8}-bit, {duration:.1f}s"
    )
    return duration


def _handle_webm_convert(raw_audio, target, tmp_wav):
    """PWA webm/opus → ffmpeg to 16kHz mono WAV, return duration."""
    tmp_webm = f"{TMP_PREFIX}{target}.webm"
    with open(tmp_webm, "wb") as f:
        f.write(raw_audio)
    webm_size = os.path.getsize(tmp_webm)
    print(f"[intercom] webm: {webm_size} bytes → ffmpeg")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                tmp_webm,
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                str(FFMPEG_SR),
                tmp_wav,
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        stderr_text = e.stderr.decode(errors="replace") if e.stderr else "(no stderr)"
        print(
            f"[intercom] ffmpeg FAILED webm={webm_size}b exit={e.returncode}: {stderr_text[:500]}"
        )
        raise
    finally:
        if os.path.exists(tmp_webm):
            os.unlink(tmp_webm)
    size_out = os.path.getsize(tmp_wav)
    duration = size_out / FFMPEG_BYTERATE
    return duration


@app.route("/convert", methods=["POST"])
def convert():
    """PWA direct: receive audio → convert → local serve → direct HA playback."""
    target = request.args.get("target", "")

    raw_audio = request.get_data()
    if not raw_audio:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    # Broadcast to all: iterate rooms with an entity
    if target == "all":
        targets = [(k, v) for k, v in ROOM_MAP.items() if v.get("entity")]
        if not targets:
            return jsonify({"ok": False, "error": "no rooms configured"}), 500
    else:
        room = ROOM_MAP.get(target)
        if not room or not room.get("entity"):
            return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400
        targets = [(target, room)]

    name = ROOM_MAP[target]["name"] if target != "all" else "\u5168\u90e8"
    print(f"[intercom] Received {len(raw_audio)} bytes for {name}")

    tmp_wav = f"{TMP_PREFIX}{target}.wav"
    filename = f"intercom_{target}.wav"

    # Branch: WAV passthrough vs webm transcode (magic byte detection)
    if raw_audio[: len(WAV_MAGIC)] == WAV_MAGIC:
        duration = _handle_wav_passthrough(raw_audio, tmp_wav)
    else:
        duration = _handle_webm_convert(raw_audio, target, tmp_wav)

    # Move to local audio dir for Flask serve
    dest = os.path.join(AUDIO_DIR, filename)
    shutil.move(tmp_wav, dest)
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    audio_url = f"{scheme}://{request.host}/audio/{filename}"
    print(f"[intercom] Converted → {audio_url}")

    # Direct HA API playback with background auto-pause
    ok_count = 0
    for _tgt_key, tgt_room in targets:
        entity = tgt_room["entity"]
        haclient.play_and_auto_pause(entity, audio_url, duration)
        ok_count += 1
        print(f"[intercom] HA play → {tgt_room['name']}")

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
