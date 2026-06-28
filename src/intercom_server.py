#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend.

PWA POST PCM → Flask write WAV → local serve → direct HA API playback.
"""

import json
import os
import sys
import wave

from flask import Flask, jsonify, request, send_from_directory

from ha_client import HAClient

app = Flask(__name__)

PCM_RATE = 16000  # 16kHz mono PCM

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

# Load room config from rooms.json
ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
with open(ROOMS_FILE) as f:
    ROOM_MAP = json.load(f)


@app.route("/")
def index():
    return send_from_directory(".", "intercom.html")


@app.route("/rooms.json")
def rooms():
    return send_from_directory(".", "rooms.json")


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)


@app.route("/rooms/status")
def rooms_status():
    status = haclient.query_statuses(ROOM_MAP)
    return jsonify(status)


@app.route("/version")
def version():
    return jsonify({"version": VERSION})


# ═══════════════════════════════════════════════════════════════════
#  PCM streaming route  (issue #7)
# ═══════════════════════════════════════════════════════════════════

_STREAM_CHUNK = 4096  # read request.stream in 4 KiB chunks


def _resolve_targets(target: str) -> list[tuple[str, dict]]:
    """Resolve a target string into a list of (key, room_dict) pairs."""
    if target == "all":
        return [(k, v) for k, v in ROOM_MAP.items() if v.get("entity")]
    room = ROOM_MAP.get(target)
    if not room or not room.get("entity"):
        return []
    return [(target, room)]


@app.route("/stream/start", methods=["POST"])
def stream_start():
    """Receive PCM stream from PWA → write WAV → HA playback.

    PWA sends a ReadableStream body (16kHz mono 16-bit signed int PCM).
    PCM is accumulated and written as a complete WAV file with correct header.
    HA play_and_auto_pause plays the file — Xiaomi gateway downloads and plays it.
    """
    import uuid

    target = request.args.get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "missing target"}), 400

    targets = _resolve_targets(target)
    if not targets:
        return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400

    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.host
    public_base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if public_base:
        base = public_base
    else:
        base = f"{scheme}://{host}"

    # ── Accumulate PCM from request.stream ─────────────────────────
    pcm = bytearray()
    pcm_started = False

    try:
        while True:
            chunk = request.stream.read(_STREAM_CHUNK)
            if not chunk:
                break
            if not pcm_started:
                pcm_started = True
                print(f"[intercom] stream PCM arriving for {target}")
            pcm.extend(chunk)
    except Exception as e:
        print(f"[intercom] stream read error: {e}")

    if len(pcm) < 44:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    # ── Write WAV file (16kHz mono 16-bit PCM) ─────────────────────
    filename = f"{uuid.uuid4().hex}.wav"
    filepath = os.path.join(AUDIO_DIR, filename)
    n_samples = len(pcm) // 2

    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(PCM_RATE)
        wf.writeframes(pcm)

    duration = n_samples / PCM_RATE
    file_size = os.path.getsize(filepath)
    print(f"[intercom] WAV written: {filename} ({file_size}B, {duration:.1f}s)")

    # ── Play on all targets ────────────────────────────────────────
    audio_url = f"{base}/audio/{filename}"
    ok_count = 0
    for _tgt_key, tgt_room in targets:
        entity = tgt_room["entity"]
        ok = haclient.play_and_auto_pause(entity, audio_url, duration)
        if not ok:
            print(f"[intercom] HA play failed for {tgt_room['name']}")
            continue
        ok_count += 1
        print(f"[intercom] HA play → {tgt_room['name']}")

    name = ROOM_MAP[target]["name"] if target != "all" else "全部"
    print(f"[intercom] stream ended for {name} → {ok_count}/{len(targets)} rooms")
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
