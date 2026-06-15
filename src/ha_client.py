"""Home Assistant REST API 客户端
封装所有 HA 交互：状态查询、服务调用、播放+自动暂停
"""
import os, json, ssl, threading, time
import urllib.request
import urllib.error
import urllib.parse

# ——— 重试常量 ———
STATE_POLL_INTERVAL = 0.5   # 轮询 state 间隔 (s)
PLAYING_CONFIRM_RETRIES = 10  # 确认 "playing" 最多 10 次 (10×0.5s=5s)
PAUSE_RETRIES = 5           # pause 重试次数


class HAClient:
    """Home Assistant REST API 客户端"""

    def __init__(self, ha_url: str, token: str):
        """ha_url: full HA URL like http://192.168.99.4:8123 or https://ha.example.com"""
        parsed = urllib.parse.urlparse(ha_url)
        self._base = f"{parsed.scheme}://{parsed.netloc}/api"
        self._token = token
        self._ctx = ssl._create_unverified_context()

    def _request(self, method: str, path: str, data: dict | None = None,
                 timeout: int = 10) -> tuple[int, dict | str]:
        """发送 HA API 请求，返回 (http_status, response_data_or_error_string)"""
        url = f"{self._base}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=self._ctx)
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return e.code, f"HTTP {e.code}"
        except Exception as e:
            return 0, str(e)

    def state(self, entity_id: str) -> str:
        """查询 entity state，失败返回空字符串"""
        if not self._token:
            return ""
        code, result = self._request("GET", f"/states/{entity_id}", timeout=3)
        if code == 200 and isinstance(result, dict):
            return result.get("state", "")
        return ""

    def call(self, service: str, data: dict) -> bool:
        """调用 HA 服务，返回成功/失败"""
        if not self._token:
            return False
        code, _ = self._request("POST", f"/services/{service}", data=data, timeout=10)
        ok = code == 200
        if not ok:
            print(f"[intercom] HA call failed ({service}): {_}")
        return ok

    def play_and_auto_pause(self, entity_id: str, audio_url: str, duration: float):
        """后台线程：播放音频 → 确认 playing → 等 duration → pause + retry

        等效于 n8n 的 play → poll state → wait → pause → verify 流水线。
        duration 参数为音频时长（秒）。
        """
        ok = self.call("media_player/play_media", {
            "entity_id": entity_id,
            "media_content_id": audio_url,
            "media_content_type": "music",
        })
        if not ok:
            print(f"[intercom] HA play failed for {entity_id}")
            return

        # 后台线程处理 pause
        threading.Thread(
            target=self._auto_pause_bg,
            args=(entity_id, duration),
            daemon=True,
        ).start()

    def _auto_pause_bg(self, entity_id: str, wait_sec: float):
        """后台线程逻辑：确认播放 → 等待 → pause + 验证"""
        t0 = time.monotonic()

        # 1) 轮询确认开始播放
        for attempt in range(1, PLAYING_CONFIRM_RETRIES + 1):
            state = self.state(entity_id)
            if state == "playing":
                print(f"[intercom] {entity_id} playing confirmed (attempt {attempt})")
                break
            time.sleep(STATE_POLL_INTERVAL)
        else:
            print(f"[intercom] WARNING: {entity_id} never reached 'playing', pausing anyway")

        # 2) 等剩余时长
        elapsed = time.monotonic() - t0
        remaining = max(0, wait_sec - elapsed)
        if remaining > 0:
            print(f"[intercom] {entity_id} elapsed {elapsed:.1f}s, sleeping {remaining:.1f}s")
            time.sleep(remaining)

        # 3) pause + 确认已停
        for attempt in range(1, PAUSE_RETRIES + 1):
            self.call("media_player/media_pause", {"entity_id": entity_id})
            time.sleep(STATE_POLL_INTERVAL)
            state = self.state(entity_id)
            if state != "playing":
                print(f"[intercom] {entity_id} paused (attempt {attempt})")
                return
            print(f"[intercom] {entity_id} still playing, retry pause ({attempt}/{PAUSE_RETRIES})")
        print(f"[intercom] WARNING: {entity_id} may still be playing after {PAUSE_RETRIES} retries")

    def query_statuses(self, room_map: dict) -> dict[str, bool]:
        """批量查询房间音箱在线状态"""
        status = {}
        for key, room in room_map.items():
            entity = room.get("entity", "")
            if not entity:
                status[key] = True
                continue
            state = self.state(entity)
            status[key] = state != "unavailable" if state else False
        return status
