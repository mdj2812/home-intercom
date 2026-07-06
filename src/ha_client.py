"""Home Assistant REST API client.

Encapsulates all HA interactions: state queries, service calls, play + auto-pause.
"""

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ——— Retry constants ———
STATE_POLL_INTERVAL = 0.5  # poll interval for state checks (seconds)
PLAYING_CONFIRM_RETRIES = 10  # max attempts to confirm "playing" (10 × 0.5s = 5s)
PAUSE_RETRIES = 5  # pause retry count
# MediaPlayerEntityFeature.REPEAT_SET from HA core:
#   homeassistant/components/media_player/const.py
# Used as modernity proxy: players that support repeat_set likely
# implement announce correctly (MA/HomePod/Chromecast).
SUPPORT_REPEAT_SET = 1 << 18  # = 262144


class HAClient:
    """Home Assistant REST API client."""

    def __init__(self, ha_url: str, token: str, pause_buffer: float = 0.0):
        """ha_url: full HA URL like http://homeassistant.local:8123 or https://ha.example.com

        pause_buffer: extra seconds to wait before pausing (default 0).
        """
        parsed = urllib.parse.urlparse(ha_url)
        self._base = f"{parsed.scheme}://{parsed.netloc}/api"
        self._token = token
        self._ctx = ssl._create_unverified_context()
        self._pause_buffer = pause_buffer
        self._entity_cache: dict[str, dict] = {}  # entity_id → {app_id, supported_features}

    def _request(
        self, method: str, path: str, data: dict | None = None, timeout: int = 10
    ) -> tuple[int, dict | str]:
        """Send HA API request, returns (http_status, response_data_or_error_string)."""
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

    def state(self, entity_id: str, with_attrs: bool = False) -> str | tuple[str, dict]:
        """Query entity state. Returns str normally, or (state, attrs) if with_attrs=True.

        Returns empty string / ({},) on failure.
        """
        if not self._token:
            return ("", {}) if with_attrs else ""
        code, result = self._request("GET", f"/states/{entity_id}", timeout=3)
        if code == 200 and isinstance(result, dict):
            s = result.get("state", "")
            if with_attrs:
                return s, result.get("attributes", {})
            return s
        return ("", {}) if with_attrs else ""

    def call(self, service: str, data: dict) -> bool:
        """Call HA service, returns success/failure."""
        if not self._token:
            return False
        code, _ = self._request("POST", f"/services/{service}", data=data, timeout=10)
        ok = code == 200
        if not ok:
            print(f"[intercom] HA call failed ({service}): {_}")
        return ok

    def supports_repeat_set(self, entity_id: str) -> bool:
        """Check if entity supports repeat_set — used as modernity proxy.

        Players with repeat_set (MA/HomePod/Chromecast) likely implement
        announce correctly and don't need a pause timer.
        """
        return bool(
            self._get_entity_info(entity_id)["supported_features"] & SUPPORT_REPEAT_SET
        )

    def _play_media(self, entity_id: str, audio_url: str) -> bool:
        """Call media_player.play_media with announce=True.

        announce=True tells the player this is an intercom announcement —
        it should resume/stop naturally after the audio finishes.
        """
        return self.call(
            "media_player/play_media",
            {
                "entity_id": entity_id,
                "media_content_id": audio_url,
                "media_content_type": "music",
                "announce": True,
            },
        )

    def _get_entity_info(self, entity_id: str) -> dict:
        """Fetch entity attrs once, cache {app_id, supported_features} per entity.

        These are static hardware capabilities — safe to cache indefinitely.
        """
        if entity_id not in self._entity_cache:
            _, attrs = self.state(entity_id, with_attrs=True)
            self._entity_cache[entity_id] = {
                "app_id": attrs.get("app_id", ""),
                "supported_features": attrs.get("supported_features", 0),
            }
        return self._entity_cache[entity_id]

    def play_and_auto_pause(self, entity_id: str, audio_url: str, duration: float) -> bool:
        """Play audio — tiers: MA announcement > modern announce > basic + timer.

        1. Music Assistant (app_id == "music_assistant"): play_announcement
        2. Modern player (supports repeat_set): play_media(announce=True)
        3. Basic player (Xiaomi): play_media(announce=True) + pause timer

        Returns True if play_media succeeded.
        """
        info = self._get_entity_info(entity_id)
        app_id = info["app_id"]

        # Tier 1: Music Assistant native announcement
        if app_id == "music_assistant":
            print(f"[intercom] {entity_id} MA player — using play_announcement")
            ok = self.call(
                "music_assistant/play_announcement",
                {"entity_id": entity_id, "url": audio_url},
            )
            if ok:
                print(f"[intercom] {entity_id} MA announcement (self-stopping)")
            else:
                print(f"[intercom] {entity_id} MA announcement failed")
            return ok

        # Tier 2/3: standard media_player path
        modern = bool(info["supported_features"] & SUPPORT_REPEAT_SET)
        print(
            f"[intercom] {entity_id} modern={modern} "
            f"(features=0x{info['supported_features']:x})"
        )

        ok = self._play_media(entity_id, audio_url)
        if not ok:
            print(f"[intercom] HA play failed for {entity_id}")
            return False

        if modern:
            print(f"[intercom] {entity_id} modern player — announce mode (self-stopping)")
            return True

        # Basic player: pause timer stops any looping
        threading.Thread(
            target=self._auto_pause_bg,
            args=(entity_id, duration),
            daemon=True,
        ).start()
        return True

    def _auto_pause_bg(self, entity_id: str, wait_sec: float):
        """Background thread: confirm playback → wait → pause + verify."""
        t0 = time.monotonic()

        # 1) Poll until "playing" state is confirmed
        for attempt in range(1, PLAYING_CONFIRM_RETRIES + 1):
            state = self.state(entity_id)
            if state == "playing":
                print(f"[intercom] {entity_id} playing confirmed (attempt {attempt})")
                break
            time.sleep(STATE_POLL_INTERVAL)
        else:
            print(f"[intercom] {entity_id} short audio (polling missed 'playing'), pausing")

        # 2) Wait for remaining duration + buffer
        elapsed = time.monotonic() - t0
        remaining = max(0, wait_sec - elapsed + self._pause_buffer)
        if remaining > 0:
            print(
                f"[intercom] {entity_id} elapsed {elapsed:.1f}s, sleeping {remaining:.1f}s (buffer +{self._pause_buffer:.1f}s)"
            )
            time.sleep(remaining)

        # 3) Pause + confirm stopped
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
        """Batch query speaker online status for all rooms."""
        status = {}
        for key, room in room_map.items():
            entity = room.get("entity", "")
            if not entity:
                status[key] = True
                continue
            state = self.state(entity)
            status[key] = state != "unavailable" if state else False
        return status
