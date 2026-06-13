#!/usr/bin/env python3
"""家庭广播系统 — 手机对讲站后端
PWA POST 音频 → Flask 转换+SCP → 回调 n8n webhook → HA 播放
"""
import os, json, subprocess, time
import urllib.request
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

HA_HOST = "192.168.99.4"
HA_WWW = "/config/www/intercom/"
N8N_HOOK = "https://n8n.home.mdj2812.top/webhook/intercom/play"

# 从 rooms.json 加载房间配置
ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
with open(ROOMS_FILE) as f:
    ROOM_MAP = json.load(f)

@app.route("/")
def index():
    return send_from_directory("/tmp", "intercom.html")

@app.route("/rooms.json")
def rooms():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "rooms.json")

@app.route("/convert", methods=["POST"])
def convert():
    """PWA 直连：接收音频 → 转换 → SCP → 回调 n8n"""
    target = request.args.get("target", "media")

    # 接收原始音频
    raw_audio = request.get_data()
    if not raw_audio:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    room = ROOM_MAP.get(target)
    if not room:
        return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400

    ts = int(time.time())
    tmp_webm = f"/tmp/msg_{target}_{ts}.webm"
    tmp_wav = f"/tmp/msg_{target}_{ts}.wav"
    filename = f"msg_{target}_{ts}.wav"

    with open(tmp_webm, "wb") as f:
        f.write(raw_audio)
    print(f"[intercom] Received {len(raw_audio)} bytes for {room['name']}")

    # ffmpeg webm → wav
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp_webm,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            tmp_wav
        ], check=True, timeout=15, capture_output=True)
        os.unlink(tmp_webm)
        size_out = os.path.getsize(tmp_wav)
        # WAV PCM 16kHz mono 16bit → 32000 bytes/sec
        duration = size_out / 32000
    except subprocess.CalledProcessError as e:
        print(f"[intercom] ffmpeg failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "conversion failed"}), 500

    # SCP 到 HA
    try:
        subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", tmp_wav,
             f"{HA_HOST}:{HA_WWW}{filename}"],
            check=True, timeout=10, capture_output=True
        )
        os.unlink(tmp_wav)
    except subprocess.CalledProcessError as e:
        print(f"[intercom] SCP failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "upload failed"}), 500

    audio_url = f"http://{HA_HOST}:8123/local/intercom/{filename}"
    print(f"[intercom] Converted → {audio_url}")

    # 回调 n8n 触发播放
    try:
        body = json.dumps({
            "entity": room["entity"],
            "url": audio_url,
            "duration": duration
        }).encode()
        req = urllib.request.Request(N8N_HOOK, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10)
        print(f"[intercom] → n8n play {room['name']}")
    except Exception as e:
        print(f"[intercom] n8n hook failed: {e}")

    return jsonify({"ok": True, "name": room["name"], "url": audio_url})


if __name__ == "__main__":
    subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", HA_HOST, f"mkdir -p {HA_WWW}"],
        check=False, timeout=5
    )
    print("[intercom] Starting on http://0.0.0.0:8765")
    app.run(host="0.0.0.0", port=8765)
