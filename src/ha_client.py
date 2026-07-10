"""Home Assistant REST API client.

Encapsulates all HA interactions: state queries, service calls, play + auto-pause.
"""

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from enum import StrEnum

_logger = logging.getLogger(__name__)


def _truncate(s: str, max_len: int) -> str:
    """Truncate string for logging, appending '…' if cut."""
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# ——— Retry constants ———
STATE_POLL_INTERVAL = 0.5  # poll interval for state checks (seconds)
PLAYING_CONFIRM_RETRIES = 10  # max attempts to confirm "playing" (10 × 0.5s = 5s)
PAUSE_RETRIES = 5  # pause retry count
DEFAULT_STATE_TIMEOUT = 5  # seconds for entity state queries
SERVICE_TIMEOUT = 10  # seconds for HA service calls
# MediaPlayerEntityFeature.REPEAT_SET from HA core:
#   homeassistant/components/media_player/const.py
# Used as modernity proxy: players that support repeat_set likely
# implement announce correctly (MA/HomePod/Chromecast).
SUPPORT_REPEAT_SET = 1 << 18  # = 262144
# MediaPlayerEntityFeature.PLAY_MEDIA from HA core (same file as above).
# Entities without this bit (e.g. Xiaomi official integration's WifiSpeaker)
# cannot call play_media at all and should be skipped early.
SUPPORT_PLAY_MEDIA = 1 << 9  # = 512
# Poll interval for speaker status (frontend setInterval, seconds).
# Also used as volume cache TTL — see _get_volume_level / query_statuses.
STATUS_POLL_INTERVAL = 30


class EntityStatus(StrEnum):
    """Shared entity status constants — used by both backend and frontend.

    Frontend equivalent: pollSpeakerStatus() in intercom.html maps these to
    green/red dot + i18n text (statusReady / statusSkipped / statusUnavailable).
    """

    ONLINE = "online"
    UNAVAILABLE = "unavailable"
    NO_PLAY_MEDIA = "no_play_media"


class PlayError(StrEnum):
    """Play-operation errors — distinct from EntityStatus (query/state).

    These are returned in play_announcement's {"ok": False, "error": ...}
    and surfaced to the frontend via the errors array in the response JSON.
    """

    PLAY_FAILED = "play_failed"
    MA_FAILED = "ma_failed"


class HAClient:
    """Home Assistant REST API client."""

    def __init__(
        self,
        ha_url: str,
        token: str,
        pause_buffer: float = 0.0,
        state_timeout: int = DEFAULT_STATE_TIMEOUT,
    ):
        """ha_url: full HA URL like http://homeassistant.local:8123 or https://ha.example.com

        pause_buffer: extra seconds to wait before pausing (default 0).
        state_timeout: seconds to wait for entity state queries (default 5).
            Increase for slow entities (Bluetooth via MA, etc.).
        """
        parsed = urllib.parse.urlparse(ha_url)
        self._base = f"{parsed.scheme}://{parsed.netloc}/api"
        self._token = token
        self._ctx = ssl._create_unverified_context()
        self._pause_buffer = pause_buffer
        self._state_timeout = state_timeout
        self._entity_cache: dict[str, dict] = {}  # entity_id → {app_id, supported_features}
        self._cache_lock = threading.Lock()
        # entity_id → (attrs_dict, timestamp) — refreshed by query_statuses poll
        self._state_cache: dict[str, tuple[dict, float]] = {}

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
            _logger.warning("[intercom] HA_TOKEN is empty — cannot query entity state")
            return ("", {}) if with_attrs else ""
        code, result = self._request("GET", f"/states/{entity_id}", timeout=self._state_timeout)
        if code == 200 and isinstance(result, dict):
            s = result.get("state", "")
            if with_attrs:
                return s, result.get("attributes", {})
            return s
        _logger.warning(
            f"[intercom] state query failed for {entity_id}: HTTP {code}"
            if code
            else f"HTTP error: {result}"
        )
        return ("", {}) if with_attrs else ""

    def call(self, service: str, data: dict) -> bool:
        """Call HA service, returns success/failure."""
        if not self._token:
            return False
        code, body = self._request(
            "POST", f"/services/{service}", data=data, timeout=SERVICE_TIMEOUT
        )
        ok = code == 200
        if not ok:
            _logger.info(
                f"[intercom] HA call failed ({service}): HTTP {code}"
                + (f" — {_truncate(str(body), 200)}" if body else "")
            )
        return ok

    def supports_repeat_set(self, entity_id: str) -> bool:
        """Check if entity supports repeat_set — used as modernity proxy.

        Players with repeat_set (MA/HomePod/Chromecast) likely implement
        announce correctly and don't need a pause timer.
        """
        info, _ = self._get_entity_info(entity_id)
        return bool(info["supported_features"] & SUPPORT_REPEAT_SET)

    def _play_media(self, entity_id: str, audio_url: str) -> bool:
        """Call media_player.play_media with announce=True.

        announce=True signals an intercom announcement. Modern players
        (MA/HomePod/Chromecast) respect it and stop naturally; basic
        players (Xiaomi) ignore it and rely on the pause timer instead.
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

    def _get_entity_info(self, entity_id: str) -> tuple[dict, bool]:
        """Fetch entity attrs once, cache {app_id, supported_features} per entity.

        Returns (info_dict, success). success=False means the state query
        timed out or failed — callers should use a fallback strategy.

        These are static hardware capabilities — safe to cache indefinitely.
        """
        # Fast path: already cached (lock-free)
        if entity_id in self._entity_cache:
            return self._entity_cache[entity_id], True

        with self._cache_lock:
            # Double-check inside lock
            if entity_id in self._entity_cache:
                return self._entity_cache[entity_id], True

            _, attrs = self.state(entity_id, with_attrs=True)
            if attrs:
                self._entity_cache[entity_id] = {
                    "app_id": attrs.get("app_id", ""),
                    "supported_features": attrs.get("supported_features", 0),
                }
                return self._entity_cache[entity_id], True
            info = self._entity_cache.get(entity_id, {"app_id": "", "supported_features": 0})
            return info, False

    def play_announcement(
        self,
        entity_id: str,
        audio_url: str,
        duration: float,
        announce_volume: int | None = None,
        audio_url_with_chime: str | None = None,
        duration_with_chime: float | None = None,
    ) -> dict:
        """Play audio — tiers: MA announcement > modern announce > basic + timer.

        1. Music Assistant (app_id == "music_assistant"): play_announcement
        2. Modern player (supports repeat_set): play_media(announce=True)
        3. Basic player (Xiaomi): play_media(announce=True) + pause timer

        announce_volume: optional volume override (0-100) for MA players only.
        audio_url_with_chime: WAV with pre-announce chime prepended (for standard players).
        duration_with_chime: total duration including chime.
        Returns {"ok": True} on success,
        {"ok": False, "error": "reason"} on failure.
        """
        state = self.state(entity_id)
        if state == EntityStatus.UNAVAILABLE:
            return {"ok": False, "error": EntityStatus.UNAVAILABLE}

        info_ok = True
        if not state:
            _logger.warning(
                f"[intercom] {entity_id} state query timed out, "
                f"skipping entity info (would also time out)"
            )
            info: dict = {"app_id": "", "supported_features": 0}
            info_ok = False
        else:
            info, info_ok = self._get_entity_info(entity_id)

        if info["app_id"] == "music_assistant":
            return self._play_ma_announcement(entity_id, audio_url, volume=announce_volume)
        # Standard player: use concatenated audio (chime + recording in one file)
        url = audio_url_with_chime or audio_url
        dur = duration_with_chime or duration
        return self._play_standard(
            entity_id, url, dur, info, announce_volume=announce_volume, info_ok=info_ok
        )

    def _play_ma_announcement(
        self,
        entity_id: str,
        audio_url: str,
        volume: int | None = None,
    ) -> dict:
        """Tier 1: Music Assistant play_announcement (self-stopping).

        volume: optional volume override (0-100). None = use player's current volume.
        """
        data: dict = {
            "entity_id": entity_id,
            "url": audio_url,
            "use_pre_announce": True,
        }
        if volume is not None:
            data["announce_volume"] = volume
            _logger.info(
                f"[intercom] {entity_id} MA player — using play_announcement"
                f" (volume={volume}, url={audio_url})"
            )
        else:
            _logger.info(
                f"[intercom] {entity_id} MA player — using play_announcement (url={audio_url})"
            )
        ok = self.call("music_assistant/play_announcement", data)
        if ok:
            _logger.info(f"[intercom] {entity_id} MA announcement (self-stopping)")
            return {"ok": True}
        _logger.info(f"[intercom] {entity_id} MA announcement failed")
        return {"ok": False, "error": PlayError.MA_FAILED}

    def _has_play_media(self, info: dict) -> bool:
        """Check if entity supports media_player.play_media (bit 9)."""
        return bool(info["supported_features"] & SUPPORT_PLAY_MEDIA)

    def _play_standard(
        self,
        entity_id: str,
        audio_url: str,
        duration: float,
        info: dict,
        announce_volume: int | None = None,
        info_ok: bool = True,
    ) -> dict:
        """Tier 2/3: standard media_player — guard, optional volume boost, play."""
        if not self._has_play_media(info):
            if not info_ok:
                _logger.warning(
                    f"[intercom] {entity_id} features unknown (timeout), trying play_media anyway"
                )
            else:
                _logger.warning(
                    f"[intercom] {entity_id} does not support play_media "
                    f"(features=0x{info['supported_features']:x}) — skip"
                )
                return {"ok": False, "error": EntityStatus.NO_PLAY_MEDIA}

        modern = bool(info["supported_features"] & SUPPORT_REPEAT_SET)
        _logger.info(
            f"[intercom] {entity_id} modern={modern} (features=0x{info['supported_features']:x})"
        )

        # Volume boost: save current → set announce volume → restore after playback
        saved_volume: float | None = None
        if announce_volume is not None:
            saved_volume = self._get_volume_level(entity_id)
            if saved_volume is not None and saved_volume * 100 < announce_volume:
                target = announce_volume / 100.0
                _logger.info(
                    f"[intercom] {entity_id} volume boost {saved_volume:.2f} → {target:.2f}"
                )
                self._set_volume_level(entity_id, target)

        ok = self._play_media(entity_id, audio_url)
        if not ok:
            self._restore_volume(entity_id, saved_volume)
            _logger.info(f"[intercom] HA play failed for {entity_id}")
            return {"ok": False, "error": PlayError.PLAY_FAILED}

        if modern:
            _logger.info(f"[intercom] {entity_id} modern player — announce mode (self-stopping)")
            if saved_volume is not None:
                threading.Thread(
                    target=self._volume_restore_bg,
                    args=(entity_id, saved_volume, duration),
                    daemon=True,
                ).start()
            return {"ok": True}

        # Basic player: pause timer + volume restore
        threading.Thread(
            target=self._auto_pause_bg,
            args=(entity_id, duration, saved_volume),
            daemon=True,
        ).start()
        return {"ok": True}

    def _get_volume_level(self, entity_id: str) -> float | None:
        """Query current volume_level (0.0–1.0), short-TTL cached."""
        now = time.monotonic()
        with self._cache_lock:
            cached = self._state_cache.get(entity_id)
            if cached is not None and now - cached[1] < STATUS_POLL_INTERVAL:
                return cached[0].get("volume_level")

        _state, attrs = self.state(entity_id, with_attrs=True)
        if isinstance(attrs, dict):
            with self._cache_lock:
                self._state_cache[entity_id] = (attrs, now)
            return attrs.get("volume_level")
        return None

    def _set_volume_level(self, entity_id: str, level: float):
        """Set volume_level via media_player.volume_set (0.0–1.0)."""
        ok = self.call("media_player/volume_set", {"entity_id": entity_id, "volume_level": level})
        if not ok:
            _logger.warning(
                f"[intercom] {entity_id} volume_set({level:.2f}) failed — volume may be wrong"
            )

    def _restore_volume(self, entity_id: str, saved_volume: float | None):
        """Restore original volume if it was changed."""
        if saved_volume is not None:
            _logger.info(f"[intercom] {entity_id} restoring volume to {saved_volume:.2f}")
            self._set_volume_level(entity_id, saved_volume)

    def _volume_restore_bg(self, entity_id: str, saved_volume: float, wait_sec: float):
        """Background thread: wait for modern player to finish, then restore volume."""
        time.sleep(wait_sec + self._pause_buffer)
        self._restore_volume(entity_id, saved_volume)

    def _auto_pause_bg(self, entity_id: str, wait_sec: float, saved_volume: float | None = None):
        """Background thread: confirm playback → wait → pause + restore volume."""
        t0 = time.monotonic()

        try:
            # 1) Poll until "playing" state is confirmed
            for attempt in range(1, PLAYING_CONFIRM_RETRIES + 1):
                state = self.state(entity_id)
                if state == "playing":
                    _logger.info(f"[intercom] {entity_id} playing confirmed (attempt {attempt})")
                    break
                time.sleep(STATE_POLL_INTERVAL)
            else:
                _logger.info(
                    f"[intercom] {entity_id} short audio (polling missed 'playing'), pausing"
                )

            # 2) Wait for remaining duration + buffer
            elapsed = time.monotonic() - t0
            remaining = max(0, wait_sec - elapsed + self._pause_buffer)
            if remaining > 0:
                _logger.info(
                    f"[intercom] {entity_id} elapsed {elapsed:.1f}s, "
                    f"sleeping {remaining:.1f}s (buffer +{self._pause_buffer:.1f}s)"
                )
                time.sleep(remaining)

            # 3) Pause + confirm stopped
            for attempt in range(1, PAUSE_RETRIES + 1):
                self.call("media_player/media_pause", {"entity_id": entity_id})
                time.sleep(STATE_POLL_INTERVAL)
                state = self.state(entity_id)
                if state != "playing":
                    _logger.info(f"[intercom] {entity_id} paused (attempt {attempt})")
                    return
                _logger.info(
                    f"[intercom] {entity_id} still playing, retry pause ({attempt}/{PAUSE_RETRIES})"
                )
            _logger.warning(
                f"[intercom] {entity_id} may still be playing after {PAUSE_RETRIES} retries"
            )
        finally:
            self._restore_volume(entity_id, saved_volume)

    def query_statuses(self, room_map: dict) -> dict[str, str]:
        """Batch query speaker online status for all rooms.

        Returns EntityStatus values: "online", "unavailable", "no_play_media".
        Only "online" rooms can receive broadcasts. The frontend uses
        this to show green/grey/red indicators and status text.

        Also refreshes state cache so _get_volume_level hits cache
        (polled every 30s by frontend).
        """
        status = {}
        for key, room in room_map.items():
            entity = room.get("entity", "")
            if not entity:
                status[key] = EntityStatus.ONLINE
                continue
            state, attrs = self.state(entity, with_attrs=True)
            if not state or state == EntityStatus.UNAVAILABLE:
                status[key] = EntityStatus.UNAVAILABLE
                continue
            # Refresh full state cache from background poll
            if isinstance(attrs, dict):
                with self._cache_lock:
                    self._state_cache[entity] = (attrs, time.monotonic())
            # Entity is online — still unavailable if it can't play_media
            info, info_ok = self._get_entity_info(entity)
            status[key] = (
                EntityStatus.ONLINE
                if info_ok and self._has_play_media(info)
                else EntityStatus.NO_PLAY_MEDIA
            )
        return status
