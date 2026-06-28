#!/usr/bin/env python3
"""Home Intercom — PWA-based family broadcast system backend."""

import json
import os
import sys
import wave

from flask import Flask, jsonify, request, send_from_directory

from ha_client import DEFAULT_STATE_TIMEOUT, HAClient

app = Flask(__name__)

HA_URL = os.environ.get("HA_URL", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")


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

OTA_LOG_DIR = os.environ.get("DATA_DIR", "/data")
OTA_LOG_PATH = os.path.join(OTA_LOG_DIR, "ota_log.json")

haclient = HAClient(HA_URL, HA_TOKEN, pause_buffer=PAUSE_BUFFER, state_timeout=STATE_TIMEOUT)

PCM_RATE = 16000  # target sample rate (Hz) for Xiaomi speaker WAV output
PCM_BPS = 2  # 16-bit audio = 2 bytes per sample
WAV_MAGIC = b"RIFF"
WAV_HEADER_SIZE = 44  # RIFF(12) + fmt(24) + data(8) = minimum valid WAV header
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
    app.logger.info(
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
    app.logger.info(
        f"[intercom] WAV written: {os.path.basename(filepath)} "
        f"({file_size}B, {duration:.1f}s, {rate}Hz)"
    )
    return duration


def _concat_wavs(chime_path, audio_path, output_path):
    """Prepend chime WAV to audio WAV. Returns total duration (seconds).

    Reads chime + audio, writes combined to output_path.
    Both files must have the same sample rate, channels, and sample width.
    """
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

    # Guard: chime and audio must be compatible
    if (chime_rate, chime_width, chime_channels) != (audio_rate, audio_width, audio_channels):
        app.logger.warning(
            f"[intercom] chime/audio format mismatch "
            f"(chime={chime_rate}Hz/{chime_width}B/{chime_channels}ch, "
            f"audio={audio_rate}Hz/{audio_width}B/{audio_channels}ch) — skipping chime"
        )
        # Copy original audio so output_path always exists (avoids 404)
        import shutil

        shutil.copy2(audio_path, output_path)
        return len(audio_frames) / (audio_rate * audio_width)

    # Write combined
    total_frames = (len(chime_frames) + len(audio_frames)) // audio_width
    duration = total_frames / audio_rate

    with wave.open(output_path, "wb") as wf_out:
        wf_out.setnchannels(audio_channels)
        wf_out.setsampwidth(audio_width)
        wf_out.setframerate(audio_rate)
        wf_out.writeframes(chime_frames + audio_frames)

    app.logger.info(f"[intercom] chime prepended ({duration:.1f}s total)")
    return duration


@app.route("/record", methods=["POST"])
def record():
    """Receive audio → write WAV → prepend chime → HA playback.

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

    os.makedirs(AUDIO_DIR, exist_ok=True)
    filename = f"intercom_{target}.wav"
    filepath = os.path.join(AUDIO_DIR, filename)

    if data[: len(WAV_MAGIC)] == WAV_MAGIC:
        _rate, duration = _handle_wav_passthrough(data, filepath)
    else:
        rate = int(request.args.get("rate", PCM_RATE))
        duration = _handle_pcm_to_wav(data, rate, filepath)

    # Prepend chime — creates a copy with chime for standard players
    filename_chime = f"intercom_{target}_chime.wav"
    filepath_chime = os.path.join(AUDIO_DIR, filename_chime)
    duration_with_chime = _concat_wavs(CHIME_WAV, filepath, filepath_chime)

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
