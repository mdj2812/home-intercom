"""Unit tests for _async_load_pwa_token (issue #54 — PWA token persistence).

The token must survive HA restarts / config-entry reloads so that
already-open PWA pages keep passing RecordView's X-PWA-Token check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from .ha_fakes import FakeStore, install_fake_homeassistant

install_fake_homeassistant()

from custom_components.home_intercom import _async_load_pwa_token  # noqa: E402
from custom_components.home_intercom.const import (  # noqa: E402
    PWA_TOKEN_STORAGE_KEY,
    PWA_TOKEN_STORAGE_VERSION,
)


@pytest.fixture(autouse=True)
def _clean_storage():
    FakeStore.reset()
    yield
    FakeStore.reset()


async def _stored_token() -> str | None:
    """Read back what the helper persisted via a fresh Store instance."""
    store = FakeStore(None, PWA_TOKEN_STORAGE_VERSION, PWA_TOKEN_STORAGE_KEY)
    data = await store.async_load()
    return data.get("token") if isinstance(data, dict) else None


class TestAsyncLoadPwaToken:
    @pytest.mark.asyncio
    async def test_first_run_generates_and_persists(self):
        token = await _async_load_pwa_token(MagicMock())
        assert isinstance(token, str)
        assert len(token) >= 32  # secrets.token_urlsafe(32) → 43 chars
        assert await _stored_token() == token

    @pytest.mark.asyncio
    async def test_restart_reuses_persisted_token(self):
        first = await _async_load_pwa_token(MagicMock())
        # Simulate HA restart: fresh Store instance over the same .storage
        second = await _async_load_pwa_token(MagicMock())
        assert second == first

    @pytest.mark.asyncio
    async def test_unexpected_storage_shape_regenerates(self):
        FakeStore._disk[PWA_TOKEN_STORAGE_KEY] = {"unexpected": "shape"}
        token = await _async_load_pwa_token(MagicMock())
        assert isinstance(token, str) and token
        assert await _stored_token() == token

    @pytest.mark.asyncio
    async def test_empty_token_string_regenerates(self):
        FakeStore._disk[PWA_TOKEN_STORAGE_KEY] = {"token": ""}
        token = await _async_load_pwa_token(MagicMock())
        assert token
        assert await _stored_token() == token

    @pytest.mark.asyncio
    async def test_non_dict_storage_regenerates(self):
        FakeStore._disk[PWA_TOKEN_STORAGE_KEY] = ["not", "a", "dict"]
        token = await _async_load_pwa_token(MagicMock())
        assert token
        assert await _stored_token() == token
