#!/usr/bin/env python3
"""家庭广播系统 — 手机对讲站后端
接收音频 → SCP 到 HA → 返回 url/entity 给 n8n 调度播放
"""
import os, json, subprocess, time
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

HA_HOST = "192.168.99.4"
HA_WWW = "/config/www/intercom/"

ROOM_MAP = {
    "living": {
        "name": "客厅",
        "entity": "media_player.xiaomi_x10a_ce5a_play_control",
    },
    "cinema": {
        "name": "影音室",
        "entity": "media_player.xiaomi_lx04_e135_play_control",
    },
    "media": {
        "name": "影音室",
        "entity": "media_player.xiaomi_lx04_e135_play_control",
    },
    "study": {
        "name": "书房",
        "entity": "media_player.xiaomi_l17a_db94_play_control",
    },
    "bedroom": {
        "name": "主卧",
        "entity": "media_player.xiaomi_lx06_627c_play_control",
    },
}

@app.route("/")
def index():
    return send_from_directory("/tmp", "intercom.html")

@app.route("/convert", methods=["POST"])
def convert():
    """n8n 调用的转换端点 — 接收 form-data (file field: input) 或 raw binary"""
    target = request.args.get("target", "media")
    
    # Try form-data file first, fall back to raw body
    file = request.files.get("input")
    if file and file.filename:
        raw_audio = file.read()
    else:
        raw_audio = request.get_data()

    if not raw_audio:
        return jsonify({"ok": False, "error": "no audio data"}), 400

    room = ROOM_MAP.get(target)
    if not room:
        return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400

    # 保存原始 webm
    ts = int(time.time())
    tmp_webm = f"/tmp/msg_{target}_{ts}.webm"
    tmp_wav = f"/tmp/msg_{target}_{ts}.wav"
    filename = f"msg_{target}_{ts}.wav"

    with open(tmp_webm, "wb") as f:
        f.write(raw_audio)
    print(f"[intercom/n8n] Received {len(raw_audio)} bytes raw for {room['name']}")

    # 转换 webm → wav
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp_webm,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            tmp_wav
        ], check=True, timeout=15, capture_output=True)
        os.unlink(tmp_webm)
        size_out = os.path.getsize(tmp_wav)
    except subprocess.CalledProcessError as e:
        print(f"[intercom/n8n] ffmpeg failed: {e.stderr.decode()}")
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
        print(f"[intercom/n8n] SCP failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "upload failed"}), 500

    audio_url = f"http://{HA_HOST}:8123/local/intercom/{filename}"
    print(f"[intercom/n8n] Converted → {audio_url}")
    return jsonify({"ok": True, "url": audio_url, "entity": room["entity"], "name": room["name"], "size": size_out})


@app.route("/upload", methods=["POST"])
def upload():
    """接收音频上传"""
    audio = request.files.get("audio")
    target = request.form.get("target", "media")

    if not audio:
        return jsonify({"ok": False, "error": "no audio file"}), 400

    room = ROOM_MAP.get(target)
    if not room:
        return jsonify({"ok": False, "error": f"unknown target: {target}"}), 400

    # 保存原始文件
    ts = int(time.time())
    tmp_webm = f"/tmp/msg_{target}_{ts}.webm"
    tmp_wav = f"/tmp/msg_{target}_{ts}.wav"
    filename = f"msg_{target}_{ts}.wav"
    audio.save(tmp_webm)
    size_in = os.path.getsize(tmp_webm)
    print(f"[intercom] Received {size_in} bytes webm for {room['name']}")

    # 转换 webm/opus → WAV (PCM 16kHz mono, 小爱兼容)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp_webm,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            tmp_wav
        ], check=True, timeout=15, capture_output=True)
        os.unlink(tmp_webm)
        size_out = os.path.getsize(tmp_wav)
        print(f"[intercom] Converted webm→wav: {size_in}B → {size_out}B")
    except subprocess.CalledProcessError as e:
        print(f"[intercom] ffmpeg failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "audio conversion failed"}), 500

    # SCP 到 HA www
    remote_path = f"{HA_HOST}:{HA_WWW}{filename}"
    try:
        subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", tmp_wav, remote_path],
            check=True, timeout=10, capture_output=True
        )
        os.unlink(tmp_wav)
    except subprocess.CalledProcessError as e:
        print(f"[intercom] SCP failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "upload failed"}), 500

    audio_url = f"http://{HA_HOST}:8123/local/intercom/{filename}"
    print(f"[intercom] Converted → {audio_url}")

    # /upload 直连模式：自己调 HA play_media（n8n 不可用时的降级路径）
    import urllib.request
    try:
        token = None
        env_file = os.path.expanduser("~/.hermes/.env")
        with open(env_file) as f:
            for line in f:
                if line.startswith("HA_TOKEN="):
                    token = line.strip().split("=", 1)[1]
                    break
        if token:
            body = json.dumps({
                "entity_id": room["entity"],
                "media_content_id": audio_url,
                "media_content_type": "music",
            }).encode()
            req = urllib.request.Request(
                f"http://{HA_HOST}:8123/api/services/media_player/play_media",
                data=body, method="POST"
            )
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
            print(f"[intercom] play_media OK → {room['name']}")
    except Exception as e:
        print(f"[intercom] play_media FAIL: {e}")
    return jsonify({"ok": True, "url": audio_url, "entity": room["entity"], "name": room["name"], "size": size_out})


if __name__ == "__main__":
    # 确保 HA www intercom 目录存在
    subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", HA_HOST, f"mkdir -p {HA_WWW}"],
        check=False, timeout=5
    )
    print("[intercom] Starting on http://0.0.0.0:8765")
    app.run(host="0.0.0.0", port=8765)
