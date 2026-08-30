"""Shared auto-pause logic for basic media_player announcements (Tier 3).

Confirm playback started → sleep remaining duration → pause with retries.
Used by the Docker container (sync REST/WS) and HA integration (async services).
"""

from __future__ import annotations

import logging
from typing import Protocol

_LOGGER = logging.getLogger(__name__)

STATE_POLL_INTERVAL = 0.5
PLAYING_CONFIRM_RETRIES = 10  # 10 × 0.5s = 5s max
PAUSE_RETRIES = 5
WS_PLAYING_TIMEOUT = 5.0
WS_PAUSE_TIMEOUT = 1.5


class SyncPauseBackend(Protocol):
    """Sync I/O primitives for run_auto_pause."""

    def monotonic(self) -> float: ...

    def get_state(self, entity_id: str) -> str | None: ...

    def sleep(self, seconds: float) -> None: ...

    def media_pause(self, entity_id: str) -> None: ...

    def wait_for_playing(self, entity_id: str, timeout: float) -> bool | None:
        """True=confirmed, False=WS timeout, None=WS unavailable."""

    def wait_for_paused(self, entity_id: str, timeout: float) -> bool | None:
        """True=confirmed stopped, False=WS timeout, None=WS unavailable."""


class AsyncPauseBackend(Protocol):
    """Async I/O primitives for async_run_auto_pause."""

    def monotonic(self) -> float: ...

    def get_state(self, entity_id: str) -> str | None: ...

    async def sleep(self, seconds: float) -> None: ...

    async def media_pause(self, entity_id: str) -> None: ...

    async def wait_for_playing(self, entity_id: str, timeout: float) -> bool | None: ...

    async def wait_for_paused(self, entity_id: str, timeout: float) -> bool | None: ...


def _remaining_wait(t0: float, wait_sec: float, pause_buffer: float, now: float) -> float:
    return max(0.0, wait_sec - (now - t0) + pause_buffer)


def run_auto_pause(
    entity_id: str,
    wait_sec: float,
    pause_buffer: float,
    backend: SyncPauseBackend,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Confirm playing → sleep remaining duration → pause. Sync implementation."""
    log = logger or _LOGGER
    t0 = backend.monotonic()
    playing_confirmed = False

    # 1) Confirm "playing" state
    if backend.get_state(entity_id) == "playing":
        log.info("[intercom] %s already playing (already there)", entity_id)
        playing_confirmed = True
    else:
        ws_result = backend.wait_for_playing(entity_id, WS_PLAYING_TIMEOUT)
        if ws_result is True:
            log.info("[intercom] %s playing confirmed (ws)", entity_id)
            playing_confirmed = True
        elif ws_result is False:
            log.info("[intercom] %s ws timeout for playing, polling", entity_id)
        elif ws_result is None:
            log.info("[intercom] %s ws not ready, polling", entity_id)

    if not playing_confirmed:
        for attempt in range(1, PLAYING_CONFIRM_RETRIES + 1):
            if backend.get_state(entity_id) == "playing":
                log.info(
                    "[intercom] %s playing confirmed (poll attempt %d)",
                    entity_id,
                    attempt,
                )
                playing_confirmed = True
                break
            backend.sleep(STATE_POLL_INTERVAL)
        if not playing_confirmed:
            log.info(
                "[intercom] %s short audio (polling missed 'playing'), pausing",
                entity_id,
            )

    # 2) Wait for remaining duration + buffer
    remaining = _remaining_wait(t0, wait_sec, pause_buffer, backend.monotonic())
    if remaining > 0:
        elapsed = backend.monotonic() - t0
        log.info(
            "[intercom] %s elapsed %.1fs, sleeping %.1fs (buffer +%.1fs)",
            entity_id,
            elapsed,
            remaining,
            pause_buffer,
        )
        backend.sleep(remaining)

    # 3) Pause + confirm stopped
    # If we never saw "playing", HA state may stay "idle" while audio is still
    # playing (common on Xiaomi/miot) — do not treat that as already stopped.
    if backend.get_state(entity_id) != "playing" and playing_confirmed:
        log.info("[intercom] %s paused (already stopped)", entity_id)
        return

    for attempt in range(1, PAUSE_RETRIES + 1):
        backend.media_pause(entity_id)

        ws_paused = backend.wait_for_paused(entity_id, WS_PAUSE_TIMEOUT)
        if ws_paused is True:
            log.info("[intercom] %s paused (ws)", entity_id)
            return

        backend.sleep(STATE_POLL_INTERVAL)
        if backend.get_state(entity_id) != "playing":
            log.info("[intercom] %s paused (attempt %d)", entity_id, attempt)
            return
        log.info(
            "[intercom] %s still playing, retry pause (%d/%d)",
            entity_id,
            attempt,
            PAUSE_RETRIES,
        )

    log.warning(
        "[intercom] %s may still be playing after %d retries",
        entity_id,
        PAUSE_RETRIES,
    )


async def async_run_auto_pause(
    entity_id: str,
    wait_sec: float,
    pause_buffer: float,
    backend: AsyncPauseBackend,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Confirm playing → sleep remaining duration → pause. Async implementation."""
    log = logger or _LOGGER
    t0 = backend.monotonic()
    playing_confirmed = False

    if backend.get_state(entity_id) == "playing":
        log.info("[intercom] %s already playing (already there)", entity_id)
        playing_confirmed = True
    else:
        ws_result = await backend.wait_for_playing(entity_id, WS_PLAYING_TIMEOUT)
        if ws_result is True:
            log.info("[intercom] %s playing confirmed (ws)", entity_id)
            playing_confirmed = True
        elif ws_result is False:
            log.info("[intercom] %s ws timeout for playing, polling", entity_id)
        elif ws_result is None:
            log.info("[intercom] %s ws not ready, polling", entity_id)

    if not playing_confirmed:
        for attempt in range(1, PLAYING_CONFIRM_RETRIES + 1):
            if backend.get_state(entity_id) == "playing":
                log.info(
                    "[intercom] %s playing confirmed (poll attempt %d)",
                    entity_id,
                    attempt,
                )
                playing_confirmed = True
                break
            await backend.sleep(STATE_POLL_INTERVAL)
        if not playing_confirmed:
            log.info(
                "[intercom] %s short audio (polling missed 'playing'), pausing",
                entity_id,
            )

    remaining = _remaining_wait(t0, wait_sec, pause_buffer, backend.monotonic())
    if remaining > 0:
        elapsed = backend.monotonic() - t0
        log.info(
            "[intercom] %s elapsed %.1fs, sleeping %.1fs (buffer +%.1fs)",
            entity_id,
            elapsed,
            remaining,
            pause_buffer,
        )
        await backend.sleep(remaining)

    if backend.get_state(entity_id) != "playing" and playing_confirmed:
        log.info("[intercom] %s paused (already stopped)", entity_id)
        return

    for attempt in range(1, PAUSE_RETRIES + 1):
        await backend.media_pause(entity_id)

        ws_paused = await backend.wait_for_paused(entity_id, WS_PAUSE_TIMEOUT)
        if ws_paused is True:
            log.info("[intercom] %s paused (ws)", entity_id)
            return

        await backend.sleep(STATE_POLL_INTERVAL)
        if backend.get_state(entity_id) != "playing":
            log.info("[intercom] %s paused (attempt %d)", entity_id, attempt)
            return
        log.info(
            "[intercom] %s still playing, retry pause (%d/%d)",
            entity_id,
            attempt,
            PAUSE_RETRIES,
        )

    log.warning(
        "[intercom] %s may still be playing after %d retries",
        entity_id,
        PAUSE_RETRIES,
    )
