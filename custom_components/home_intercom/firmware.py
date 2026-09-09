"""Fetch and cache intercom-button firmware from GitHub releases.

The ESP32 OTA client is HTTP-only on the LAN. Home Intercom downloads the
latest public GitHub ``.bin`` over HTTPS, stores it, and serves
``GET /api/home_intercom/firmware`` with ``X-Checksum-SHA256``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .const import (
        FIRMWARE_ASSET_BIN_RE,
        FIRMWARE_CACHE_BIN,
        FIRMWARE_CACHE_META,
        FIRMWARE_CACHE_SIG,
        FIRMWARE_GITHUB_DOWNLOAD_URL,
        FIRMWARE_GITHUB_LATEST_PAGE,
        FIRMWARE_GITHUB_LATEST_URL,
        FIRMWARE_POLL_INTERVAL_SECS,
    )
    from .shared import normalize_firmware_version
except ImportError:
    from const import (  # Docker standalone (absolute)
        FIRMWARE_ASSET_BIN_RE,
        FIRMWARE_CACHE_BIN,
        FIRMWARE_CACHE_META,
        FIRMWARE_CACHE_SIG,
        FIRMWARE_GITHUB_DOWNLOAD_URL,
        FIRMWARE_GITHUB_LATEST_PAGE,
        FIRMWARE_GITHUB_LATEST_URL,
        FIRMWARE_POLL_INTERVAL_SECS,
    )
    from shared import normalize_firmware_version

_LOGGER = logging.getLogger(__name__)
_BIN_RE = re.compile(FIRMWARE_ASSET_BIN_RE)
_USER_AGENT = "home-intercom-ota"
_HTTP_TIMEOUT_SECS = 60


class FirmwareError(Exception):
    """GitHub fetch or cache failure."""


class FirmwareFetchError(FirmwareError):
    """Transport failure talking to GitHub (rate limit, network, timeout)."""


@dataclass(frozen=True)
class CachedFirmware:
    """On-disk firmware image plus checksum used for OTA headers."""

    version: str
    sha256: str
    bin_path: str
    sig_path: str | None = None


@dataclass(frozen=True)
class _ReleaseAsset:
    version: str
    tag: str
    asset_name: str
    download_url: str
    sha256: str
    sig_url: str | None


def firmware_checksum_headers(sha256: str) -> dict[str, str]:
    """HTTP headers the ESP32 OTA client already understands."""
    return {"X-Checksum-SHA256": sha256}


def load_cached_firmware(cache_dir: str) -> CachedFirmware | None:
    """Return the cached image when bin + meta are present, else None."""
    if not cache_dir:
        return None
    cache = Path(cache_dir)
    meta_path = cache / FIRMWARE_CACHE_META
    bin_path = cache / FIRMWARE_CACHE_BIN
    if not meta_path.is_file() or not bin_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _LOGGER.warning("firmware meta unreadable: %s", exc)
        return None
    if not isinstance(meta, dict):
        return None
    version = normalize_firmware_version(str(meta.get("version") or ""))
    sha256 = str(meta.get("sha256") or "").strip().lower()
    if not version or not sha256:
        return None
    sig = cache / FIRMWARE_CACHE_SIG
    return CachedFirmware(
        version=version,
        sha256=sha256,
        bin_path=str(bin_path),
        sig_path=str(sig) if sig.is_file() else None,
    )


def ensure_latest_firmware(cache_dir: str) -> CachedFirmware:
    """Download the latest GitHub ``.bin`` if the cache is missing or stale.

    Falls back to an existing cache when GitHub is unreachable so a previous
    successful fetch can still trigger OTA. Raises FirmwareError when neither
    GitHub nor cache can provide an image.
    """
    try:
        return _fetch_and_cache(cache_dir)
    except FirmwareError as exc:
        cached = load_cached_firmware(cache_dir)
        if cached is not None:
            _LOGGER.warning("firmware fetch failed (%s); using cache %s", exc, cached.version)
            return cached
        raise


def refresh_cached_firmware(cache_dir: str) -> CachedFirmware | None:
    """Best-effort GitHub poll for the background cache refresher.

    Never raises — a failed poll must not take down Flask or the HA loop.
    """
    try:
        return ensure_latest_firmware(cache_dir)
    except FirmwareError as exc:
        _LOGGER.warning("firmware cache refresh failed: %s", exc)
        return None


def start_firmware_poller(
    cache_dir: str,
    interval_secs: int = FIRMWARE_POLL_INTERVAL_SECS,
    stop: threading.Event | None = None,
    should_run: Callable[[], bool] | None = None,
) -> threading.Event:
    """Daemon thread: refresh immediately, then every ``interval_secs``.

    ``should_run`` gates GitHub traffic — Docker passes “has registered
    devices”. Returns the stop event (caller may pass one in). Home
    Assistant uses ``async_track_time_interval`` instead.
    """
    halt = stop if stop is not None else threading.Event()

    def _loop() -> None:
        while not halt.is_set():
            if should_run is None or should_run():
                refresh_cached_firmware(cache_dir)
            halt.wait(interval_secs)

    threading.Thread(target=_loop, name="home-intercom-firmware-poll", daemon=True).start()
    _LOGGER.info("firmware cache poller every %ss (%s)", interval_secs, cache_dir)
    return halt


def schedule_firmware_refresh(cache_dir: str) -> None:
    """Background GitHub fetch when the first device registers."""
    if not cache_dir:
        return
    threading.Thread(
        target=refresh_cached_firmware,
        args=(cache_dir,),
        name="home-intercom-firmware-refresh",
        daemon=True,
    ).start()


def _fetch_and_cache(cache_dir: str) -> CachedFirmware:
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    release = _fetch_latest_release()
    cached = load_cached_firmware(str(cache))
    if (
        cached is not None
        and cached.version == release.version
        and Path(cached.bin_path).is_file()
        and (not release.sha256 or cached.sha256 == release.sha256)
    ):
        _remove_cache_temps(cache)
        return cached

    data = _http_get(release.download_url)
    sha256 = hashlib.sha256(data).hexdigest()
    if release.sha256 and sha256 != release.sha256:
        raise FirmwareError("downloaded firmware checksum mismatch")

    bin_path = cache / FIRMWARE_CACHE_BIN
    _atomic_write(bin_path, data)

    sig_path: str | None = None
    if release.sig_url:
        try:
            sig = _http_get(release.sig_url)
            sig_file = cache / FIRMWARE_CACHE_SIG
            _atomic_write(sig_file, sig)
            sig_path = str(sig_file)
        except FirmwareError as exc:
            _LOGGER.info("firmware signature not cached: %s", exc)
    if sig_path is None:
        _unlink_quiet(cache / FIRMWARE_CACHE_SIG)

    meta = {
        "version": release.version,
        "sha256": sha256,
        "tag": release.tag,
        "asset": release.asset_name,
    }
    _atomic_write(cache / FIRMWARE_CACHE_META, json.dumps(meta, indent=2).encode("utf-8"))
    _remove_cache_temps(cache)
    _LOGGER.info("cached firmware %s (%dB, sha256=%s)", release.version, len(data), sha256)
    return CachedFirmware(
        version=release.version,
        sha256=sha256,
        bin_path=str(bin_path),
        sig_path=sig_path,
    )


def _fetch_latest_release() -> _ReleaseAsset:
    try:
        return _fetch_latest_release_api()
    except FirmwareFetchError as exc:
        _LOGGER.warning("GitHub API unavailable (%s); falling back to release page", exc)
        return _fetch_latest_release_page()


def _fetch_latest_release_api() -> _ReleaseAsset:
    raw = _http_get(FIRMWARE_GITHUB_LATEST_URL, json_api=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirmwareError("invalid GitHub release JSON") from exc
    if not isinstance(payload, dict):
        raise FirmwareError("invalid GitHub release JSON")
    tag = str(payload.get("tag_name") or "").strip()
    version = normalize_firmware_version(tag)
    if not version:
        raise FirmwareError("latest GitHub release has no tag_name")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    bin_asset = _pick_bin_asset(assets, tag)
    name = str(bin_asset.get("name") or "")
    url = str(bin_asset.get("browser_download_url") or "")
    if not url:
        raise FirmwareError(f"firmware asset {name!r} has no download URL")
    sha256 = _digest_sha256(bin_asset.get("digest"))
    sig_url = _pick_sig_url(assets, name)
    return _ReleaseAsset(
        version=version,
        tag=tag,
        asset_name=name,
        download_url=url,
        sha256=sha256,
        sig_url=sig_url,
    )


def _fetch_latest_release_page() -> _ReleaseAsset:
    """Resolve latest tag via github.com redirect when the API is rate-limited."""
    tag = _tag_from_release_url(_http_final_url(FIRMWARE_GITHUB_LATEST_PAGE))
    name = f"intercom-button-{tag}.bin"
    url = FIRMWARE_GITHUB_DOWNLOAD_URL.format(tag=tag, name=name)
    return _ReleaseAsset(
        version=normalize_firmware_version(tag),
        tag=tag,
        asset_name=name,
        download_url=url,
        sha256="",
        sig_url=url + ".sig",
    )


def _tag_from_release_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    marker = "/releases/tag/"
    if marker not in path:
        raise FirmwareError(f"unexpected GitHub latest URL {url}")
    tag = path.split(marker, 1)[1].split("/")[0]
    if not tag:
        raise FirmwareError(f"unexpected GitHub latest URL {url}")
    return tag


def _pick_bin_asset(assets: list[Any], tag: str) -> dict[str, Any]:
    bins = [a for a in assets if isinstance(a, dict) and _BIN_RE.match(str(a.get("name") or ""))]
    if not bins:
        raise FirmwareError("no intercom-button-*.bin on latest GitHub release")
    for asset in bins:
        name = str(asset.get("name") or "")
        if tag and tag in name:
            return asset
        if tag.lstrip("vV") and tag.lstrip("vV") in name:
            return asset
    return bins[0]


def _pick_sig_url(assets: list[Any], bin_name: str) -> str | None:
    want = f"{bin_name}.sig"
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "") != want:
            continue
        url = str(asset.get("browser_download_url") or "")
        return url or None
    return None


def _digest_sha256(digest: Any) -> str:
    text = str(digest or "").strip().lower()
    if text.startswith("sha256:"):
        return text[7:]
    return ""


def _http_get(url: str, *, json_api: bool = False) -> bytes:
    with _http_open(url, json_api=json_api) as resp:
        return resp.read()


def _http_final_url(url: str) -> str:
    with _http_open(url) as resp:
        return resp.geturl()


def _http_open(url: str, *, json_api: bool = False):
    headers = {"User-Agent": _USER_AGENT}
    if json_api:
        headers["Accept"] = "application/vnd.github+json"
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECS)
    except urllib.error.HTTPError as exc:
        raise FirmwareFetchError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise FirmwareFetchError(f"network error fetching {url}") from exc
    except TimeoutError as exc:
        raise FirmwareFetchError(f"timeout fetching {url}") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:
        _unlink_quiet(tmp)
        raise


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _LOGGER.debug("could not remove %s: %s", path, exc)


def _remove_cache_temps(cache: Path) -> None:
    """Drop leftover atomic-write temps after a cache update."""
    if not cache.is_dir():
        return
    for path in cache.glob("*.tmp"):
        _unlink_quiet(path)
