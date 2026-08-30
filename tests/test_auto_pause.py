"""Unit tests for shared auto_pause logic."""

import pytest

from auto_pause import (
    PAUSE_RETRIES,
    PLAYING_CONFIRM_RETRIES,
    STATE_POLL_INTERVAL,
    async_run_auto_pause,
    run_auto_pause,
)


class _SyncBackend:
    def __init__(self, states: list[str], monotonic_values: list[float] | None = None):
        self._states = states
        self._index = 0
        self._mono_index = 0
        self._monotonic_values = monotonic_values or [0.0]
        self.pause_calls: list[str] = []
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        if self._mono_index < len(self._monotonic_values):
            value = self._monotonic_values[self._mono_index]
            self._mono_index += 1
            return value
        return self._monotonic_values[-1]

    def get_state(self, entity_id: str) -> str | None:
        if self._index < len(self._states):
            state = self._states[self._index]
            self._index += 1
            return state
        return self._states[-1] if self._states else None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def media_pause(self, entity_id: str) -> None:
        self.pause_calls.append(entity_id)

    def wait_for_playing(self, entity_id: str, timeout: float) -> bool | None:
        return None

    def wait_for_paused(self, entity_id: str, timeout: float) -> bool | None:
        return None


class _AsyncBackend(_SyncBackend):
    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def media_pause(self, entity_id: str) -> None:
        self.pause_calls.append(entity_id)

    async def wait_for_playing(self, entity_id: str, timeout: float) -> bool | None:
        return None

    async def wait_for_paused(self, entity_id: str, timeout: float) -> bool | None:
        return None


class TestRunAutoPause:
    def test_confirms_playing_then_pauses(self):
        backend = _SyncBackend(
            ["idle", "idle", "playing", "playing", "idle"],
            [0.0, 0.5, 3.5],
        )
        run_auto_pause("media_player.test", 3.0, 0.0, backend)
        assert backend.pause_calls == ["media_player.test"]
        assert backend.sleeps == [STATE_POLL_INTERVAL, 2.5, STATE_POLL_INTERVAL]

    def test_short_audio_no_playing_detected_still_pauses(self):
        """Idle state throughout (never confirmed) — must still call pause."""
        backend = _SyncBackend(
            ["idle"] * (PLAYING_CONFIRM_RETRIES + 3),
            [0.0, 5.0, 5.5],
        )
        run_auto_pause("media_player.test", 0.5, 0.0, backend)
        assert backend.pause_calls == ["media_player.test"]

    def test_already_stopped_skips_pause(self):
        backend = _SyncBackend(["playing", "idle"], [0.0, 0.0, 3.0])
        run_auto_pause("media_player.test", 3.0, 0.0, backend)
        assert backend.pause_calls == []

    def test_pause_retries_when_still_playing(self):
        states = ["playing"] + ["playing"] * (PAUSE_RETRIES * 2) + ["idle"]
        backend = _SyncBackend(states, [0.0, 0.0, 3.0, 3.0, 3.5, 4.0])
        run_auto_pause("media_player.test", 3.0, 0.0, backend)
        assert len(backend.pause_calls) == PAUSE_RETRIES


class TestAsyncRunAutoPause:
    @pytest.mark.asyncio
    async def test_confirms_playing_then_pauses(self):
        backend = _AsyncBackend(
            ["idle", "idle", "playing", "playing", "idle"],
            [0.0, 0.5, 3.5],
        )
        await async_run_auto_pause("media_player.test", 3.0, 0.0, backend)
        assert backend.pause_calls == ["media_player.test"]

    @pytest.mark.asyncio
    async def test_idle_without_playing_confirm_still_pauses(self):
        """Xiaomi-style: never reports playing, stays idle — must pause anyway."""
        backend = _AsyncBackend(
            ["idle"] * (PLAYING_CONFIRM_RETRIES + 3),
            [0.0, 5.0, 5.5],
        )
        await async_run_auto_pause("media_player.test", 2.0, 0.0, backend)
        assert backend.pause_calls == ["media_player.test"]

    @pytest.mark.asyncio
    async def test_uses_remaining_duration_after_confirm_poll(self):
        backend = _AsyncBackend(["idle", "playing", "playing", "idle"], [0.0, 1.0, 4.0])
        await async_run_auto_pause("media_player.test", 5.0, 0.5, backend)
        assert backend.sleeps[0] == pytest.approx(4.5, abs=0.01)
