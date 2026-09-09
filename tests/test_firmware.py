"""GitHub firmware fetch + on-disk cache."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest
from firmware import (
    CachedFirmware,
    FirmwareError,
    ensure_latest_firmware,
    load_cached_firmware,
)
from shared import devices_payload, firmware_update_available, normalize_firmware_version

API_URL = "https://api.github.com/repos/mdj2812/intercom-button/releases/latest"
BIN_URL = "https://github.example/intercom-button-v0.2.0.bin"
BLOB = b"esp32-firmware-image"
SHA = hashlib.sha256(BLOB).hexdigest()


class _Resp:
    def __init__(self, data: bytes, url: str = ""):
        self._data = data
        self.url = url

    def read(self) -> bytes:
        return self._data

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _latest_json(*, digest: str | None = SHA, sig: bool = False) -> bytes:
    assets = [
        {
            "name": "intercom-button-v0.2.0.bin",
            "browser_download_url": BIN_URL,
        }
    ]
    if digest:
        assets[0]["digest"] = f"sha256:{digest}"
    if sig:
        assets.append(
            {
                "name": "intercom-button-v0.2.0.bin.sig",
                "browser_download_url": BIN_URL + ".sig",
            }
        )
    return json.dumps({"tag_name": "v0.2.0", "assets": assets}).encode()


def _urlopen_map(mapping: dict[str, bytes]):
    def _open(req, timeout=None):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if url not in mapping:
            raise urllib.error.HTTPError(url, 404, "not found", hdrs=None, fp=None)
        return _Resp(mapping[url], url=url)

    return _open


def test_normalize_firmware_version():
    assert normalize_firmware_version("v0.2.0") == "0.2.0"
    assert normalize_firmware_version("0.2.0") == "0.2.0"
    assert normalize_firmware_version("V1.0.0") == "1.0.0"
    assert normalize_firmware_version("") == ""


def test_firmware_update_available():
    assert firmware_update_available("0.2.0", "0.2.1") is True
    assert firmware_update_available("0.2.1", "v0.2.1") is False
    assert firmware_update_available("0.2.0", "") is False
    assert firmware_update_available("", "0.2.1") is True
    assert firmware_update_available("0.2.1-local", "0.2.1") is True


class _Store:
    def __init__(self, devices):
        self.devices = devices


def test_devices_payload_omits_update_fields_without_latest():
    store = _Store({"AA:BB:CC:DD:EE:FF": {"firmware_version": "0.2.0", "name": "Btn"}})
    out = devices_payload(store, "")
    assert "firmware_latest" not in out["AA:BB:CC:DD:EE:FF"]
    assert "firmware_update_available" not in out["AA:BB:CC:DD:EE:FF"]


def test_devices_payload_marks_outdated_and_current():
    store = _Store(
        {
            "AA:BB:CC:DD:EE:01": {"firmware_version": "0.2.0", "name": "Old"},
            "AA:BB:CC:DD:EE:02": {"firmware_version": "0.2.1", "name": "New"},
        }
    )
    out = devices_payload(store, "v0.2.1")
    assert out["AA:BB:CC:DD:EE:01"]["firmware_latest"] == "0.2.1"
    assert out["AA:BB:CC:DD:EE:01"]["firmware_update_available"] is True
    assert out["AA:BB:CC:DD:EE:02"]["firmware_update_available"] is False


def test_load_cached_firmware_empty(tmp_path):
    assert load_cached_firmware(str(tmp_path)) is None
    assert load_cached_firmware("") is None


def test_ensure_latest_downloads_and_caches(tmp_path):
    cache = tmp_path / "firmware"
    mapping = {API_URL: _latest_json(), BIN_URL: BLOB}
    with patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map(mapping)):
        cached = ensure_latest_firmware(str(cache))
    assert cached.version == "0.2.0"
    assert cached.sha256 == SHA
    assert (cache / "firmware.bin").read_bytes() == BLOB
    loaded = load_cached_firmware(str(cache))
    assert loaded == cached


def test_ensure_latest_skips_download_when_cache_fresh(tmp_path):
    cache = tmp_path / "firmware"
    mapping = {API_URL: _latest_json(), BIN_URL: BLOB}
    with patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map(mapping)) as mock_open:
        ensure_latest_firmware(str(cache))
        first_calls = mock_open.call_count
        ensure_latest_firmware(str(cache))
        # Latest JSON is checked again; the .bin is not re-downloaded.
        assert mock_open.call_count == first_calls + 1


def test_ensure_latest_falls_back_to_cache(tmp_path):
    cache = tmp_path / "firmware"
    mapping = {API_URL: _latest_json(), BIN_URL: BLOB}
    with patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map(mapping)):
        first = ensure_latest_firmware(str(cache))

    def _fail(req, timeout=None):
        raise urllib.error.URLError("github down")

    with patch("firmware.urllib.request.urlopen", side_effect=_fail):
        cached = ensure_latest_firmware(str(cache))
    assert cached.version == first.version
    assert cached.sha256 == first.sha256


def test_ensure_latest_raises_without_cache(tmp_path):
    def _fail(req, timeout=None):
        raise urllib.error.URLError("github down")

    with (
        patch("firmware.urllib.request.urlopen", side_effect=_fail),
        pytest.raises(FirmwareError),
    ):
        ensure_latest_firmware(str(tmp_path / "firmware"))


def test_ensure_latest_checksum_mismatch(tmp_path):
    mapping = {
        API_URL: _latest_json(digest="0" * 64),
        BIN_URL: BLOB,
    }
    with (
        patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map(mapping)),
        pytest.raises(FirmwareError, match="checksum"),
    ):
        ensure_latest_firmware(str(tmp_path / "firmware"))


def test_ensure_latest_no_bin_asset(tmp_path):
    payload = json.dumps({"tag_name": "v0.2.0", "assets": []}).encode()
    with (
        patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map({API_URL: payload})),
        pytest.raises(FirmwareError, match="no intercom-button"),
    ):
        ensure_latest_firmware(str(tmp_path / "firmware"))


def test_ensure_latest_caches_optional_sig(tmp_path):
    sig = b"s" * 64
    mapping = {
        API_URL: _latest_json(sig=True),
        BIN_URL: BLOB,
        BIN_URL + ".sig": sig,
    }
    with patch("firmware.urllib.request.urlopen", side_effect=_urlopen_map(mapping)):
        cached = ensure_latest_firmware(str(tmp_path / "firmware"))
    assert cached.sig_path is not None
    assert (tmp_path / "firmware" / "firmware.sig").read_bytes() == sig


def test_api_403_falls_back_to_release_page(tmp_path):
    from const import FIRMWARE_GITHUB_DOWNLOAD_URL, FIRMWARE_GITHUB_LATEST_PAGE

    page_tag = "https://github.com/mdj2812/intercom-button/releases/tag/v0.2.0"
    gh_bin = FIRMWARE_GITHUB_DOWNLOAD_URL.format(tag="v0.2.0", name="intercom-button-v0.2.0.bin")

    def _open(req, timeout=None):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if url == API_URL:
            raise urllib.error.HTTPError(url, 403, "rate limit", hdrs=None, fp=None)
        if url == FIRMWARE_GITHUB_LATEST_PAGE:
            return _Resp(b"<html>latest</html>", url=page_tag)
        if url == gh_bin:
            return _Resp(BLOB, url=url)
        if url.endswith(".sig"):
            raise urllib.error.HTTPError(url, 404, "not found", hdrs=None, fp=None)
        raise urllib.error.HTTPError(url, 404, "not found", hdrs=None, fp=None)

    with patch("firmware.urllib.request.urlopen", side_effect=_open):
        cached = ensure_latest_firmware(str(tmp_path / "firmware"))
    assert cached.version == "0.2.0"
    assert cached.sha256 == SHA
    assert (tmp_path / "firmware" / "firmware.bin").read_bytes() == BLOB


def test_cached_firmware_is_frozen():
    item = CachedFirmware(version="0.2.0", sha256=SHA, bin_path="/tmp/x")
    with pytest.raises(FrozenInstanceError):
        item.version = "9.9.9"  # type: ignore[misc]
