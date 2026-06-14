#!/usr/bin/env python3
"""家庭广播系统 — 手机对讲站后端
PWA POST 音频 → Flask 转换 → 本地 serve → 回调 n8n webhook → HA 播放
"""
import os, json, subprocess, ssl, shutil
import urllib.request
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

HA_HOST = os.environ.get("HA_HOST", "192.168.99.4")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
N8N_HOOK = os.environ.get("N8N_HOOK", "")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

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

    status = {}
    ctx = ssl._create_unverified_context()

    for key, room in ROOM_MAP.items():
        entity = room.get("entity", "")
        if not entity:
            status[key] = True
            continue
        try:
            url = f"http://{HA_HOST}:8123/api/states/{entity}"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {HA_TOKEN}")
            resp = urllib.request.urlopen(req, timeout=3, context=ctx)
            data = json.loads(resp.read())
            status[key] = data.get("state") != "unavailable"
        except Exception as e:
            print(f"[intercom] HA query failed for {key}: {e}")
            status[key] = False

    return jsonify(status)


@app.route("/convert", methods=["POST"])
def convert():
    """PWA 直连：接收音频 → 转换 → 本地 serve → 回调 n8n"""
    target = request.args.get("target", "")

    # 接收原始音频
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

    tmp_webm = f"/tmp/msg_{target}.webm"
    tmp_wav = f"/tmp/msg_{target}.wav"
    filename = f"intercom_{target}.wav"

    with open(tmp_webm, "wb") as f:
        f.write(raw_audio)

    # ffmpeg webm → wav
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp_webm,
            "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
            tmp_wav
        ], check=True, timeout=60, capture_output=True)
        os.unlink(tmp_webm)
        size_out = os.path.getsize(tmp_wav)
        duration = size_out / 32000
    except subprocess.CalledProcessError as e:
        print(f"[intercom] ffmpeg failed: {e.stderr.decode()}")
        return jsonify({"ok": False, "error": "conversion failed"}), 500

    # 移动到本地音频目录，Flask 直接 serve
    dest = os.path.join(AUDIO_DIR, filename)
    shutil.move(tmp_wav, dest)
    audio_url = f"{request.host_url}audio/{filename}"
    print(f"[intercom] Converted → {audio_url}")

    # 回调 n8n 触发播放 — 全部广播时逐个触发每个房间
    ok_count = 0
    for tgt_key, tgt_room in targets:
        try:
            body = json.dumps({
                "entity": tgt_room["entity"],
                "url": audio_url,
                "duration": duration
            }).encode()
            req = urllib.request.Request(N8N_HOOK, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            ctx = ssl._create_unverified_context()
            urllib.request.urlopen(req, timeout=10, context=ctx)
            ok_count += 1
            print(f"[intercom] → n8n play {tgt_room['name']}")
        except Exception as e:
            print(f"[intercom] n8n hook failed ({tgt_room['name']}): {e}")

    return jsonify({"ok": True, "name": name, "rooms_sent": ok_count, "url": audio_url})


if __name__ == "__main__":
    print(f"[intercom] Audio dir: {AUDIO_DIR}")
    print("[intercom] Starting on http://0.0.0.0:8764")
    app.run(host="0.0.0.0", port=8764)
