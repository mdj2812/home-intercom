#!/usr/bin/env python3
"""家庭广播系统 — 手机对讲站后端
PWA POST 音频 → Flask 转换 → 本地 serve → 直接调 HA API 播放
"""
import os, json, subprocess, shutil, sys, wave
from flask import Flask, request, jsonify, send_from_directory
from ha_client import HAClient

app = Flask(__name__)

HA_URL = os.environ.get("HA_URL", "http://192.168.99.4:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

haclient = HAClient(HA_URL, HA_TOKEN)

# ——— 版本号 ———
try:
    with open("/app/.docker-image") as f:
        VERSION = f.read().strip().split(":")[-1]
except Exception:
    VERSION = os.environ.get("VERSION", "dev")

# ——— 音频处理常量 ———
WAV_MAGIC = b'RIFF'           # WAV 文件魔数
TMP_PREFIX = "/tmp/intercom_"  # 临时文件前缀
FFMPEG_SR = 16000              # ffmpeg 输出采样率 (Hz)
FFMPEG_BPS = 2                 # s16le = 2 bytes/sample
FFMPEG_BYTERATE = FFMPEG_SR * FFMPEG_BPS  # 16000 Hz × 2 = 32000 B/s

# 从 rooms.json 加载房间配置
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
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), filename)

@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route("/rooms/status")
def rooms_status():
    """查询 HA 中小爱音箱的在线状态"""
    if not HA_TOKEN:
        return jsonify({"error": "no HA_TOKEN"}), 500
    return jsonify(haclient.query_statuses(ROOM_MAP))


@app.route("/version")
def version():
    return jsonify({"version": VERSION})


def _handle_wav_passthrough(raw_audio, tmp_wav):
    """ESP32 硬件按键发来的 PCM WAV → 直通，解析头返回 duration"""
    with open(tmp_wav, "wb") as f:
        f.write(raw_audio)
    with wave.open(tmp_wav, 'rb') as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
        sampwidth = wf.getsampwidth()
        duration = nframes / sr
    print(f"[intercom] WAV passthrough {len(raw_audio)} bytes, "
          f"{sr}Hz, {sampwidth*8}-bit, {duration:.1f}s")
    return duration


def _handle_webm_convert(raw_audio, tmp_webm, tmp_wav):
    """PWA 发来的 webm/opus → ffmpeg 转 16kHz mono WAV，返回 duration"""
    with open(tmp_webm, "wb") as f:
        f.write(raw_audio)
    subprocess.run([
        "ffmpeg", "-y", "-i", tmp_webm,
        "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(FFMPEG_SR),
        tmp_wav
    ], check=True, timeout=60, capture_output=True)
    os.unlink(tmp_webm)
    size_out = os.path.getsize(tmp_wav)
    duration = size_out / FFMPEG_BYTERATE
    return duration


@app.route("/convert", methods=["POST"])
def convert():
    """PWA 直连：接收音频 → 转换 → 本地 serve → 直接调 HA 播放"""
    target = request.args.get("target", "")

    raw_audio = request.get_data()
    if not raw_audio:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    # 全部广播：遍历所有有 entity 的房间
    if target == "all":
        targets = [(k, v) for k, v in ROOM_MAP.items() if v.get("entity")]
        if not targets:
            return jsonify({"ok": False, "error": "no rooms configured"}), 500
    else:
        room = ROOM_MAP.get(target)
        if not room or not room.get("entity"):
            return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400
        targets = [(target, room)]

    name = ROOM_MAP[target]["name"] if target != "all" else "全部"
    print(f"[intercom] Received {len(raw_audio)} bytes for {name}")

    tmp_wav = f"{TMP_PREFIX}{target}.wav"
    filename = f"intercom_{target}.wav"

    # 分支：WAV 直通 vs webm 转码（魔数检测，比扩展名更可靠）
    if raw_audio[:len(WAV_MAGIC)] == WAV_MAGIC:
        duration = _handle_wav_passthrough(raw_audio, tmp_wav)
    else:
        tmp_webm = f"{TMP_PREFIX}{target}.webm"
        try:
            duration = _handle_webm_convert(raw_audio, tmp_webm, tmp_wav)
        except subprocess.CalledProcessError as e:
            print(f"[intercom] ffmpeg failed: {e.stderr.decode()}")
            os.unlink(tmp_wav)  # 清理 ffmpeg 残留的部分输出文件
            return jsonify({"ok": False, "error": "conversion failed"}), 500

    # 移动到本地音频目录，Flask 直接 serve
    dest = os.path.join(AUDIO_DIR, filename)
    shutil.move(tmp_wav, dest)
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    audio_url = f"{scheme}://{request.host}/audio/{filename}"
    print(f"[intercom] Converted → {audio_url}")

    # 直接调 HA API 播放，后台线程自动 pause
    ok_count = 0
    for tgt_key, tgt_room in targets:
        entity = tgt_room["entity"]
        haclient.play_and_auto_pause(entity, audio_url, duration)
        ok_count += 1
        print(f"[intercom] HA play → {tgt_room['name']}")

    return jsonify({"ok": True, "name": name, "rooms_sent": ok_count, "url": audio_url})


if __name__ == "__main__":
    from waitress import serve
    import logging

    logging.basicConfig(level=logging.INFO, format="[intercom] %(message)s", stream=sys.stdout)

    # trusted_proxy: set via TRUSTED_PROXY env (default '*' for homelab, restrict for production)
    trusted_proxy = os.environ.get("TRUSTED_PROXY", "*")

    print(f"[intercom] Audio dir: {AUDIO_DIR}", flush=True)
    print(f"[intercom] Trusted proxy: {trusted_proxy}", flush=True)
    print("[intercom] Starting on http://0.0.0.0:8764", flush=True)
    serve(app, host="0.0.0.0", port=8764,
          trusted_proxy=trusted_proxy,
          trusted_proxy_headers={"x-forwarded-proto"})
