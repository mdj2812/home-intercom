"""Unit tests for custom chime shared helpers (issue #66)."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.home_intercom.const import CUSTOM_CHIME_FILENAME, DEFAULT_CHIME_STATIC_URL
from custom_components.home_intercom.shared import (
    chime_public_url,
    chime_status_payload,
    delete_custom_chime,
    has_custom_chime,
    resolve_chime_wav,
    write_custom_chime_wav,
)

WAV_DATA = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00" + b"\x00" * 64
)


class TestResolveChime:
    def test_default_when_no_custom(self, tmp_path: Path) -> None:
        integration_dir = tmp_path / "integration"
        (integration_dir / "static").mkdir(parents=True)
        default = integration_dir / "static" / "pre_announce.wav"
        default.write_bytes(WAV_DATA)
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()

        resolved = resolve_chime_wav(integration_dir=integration_dir, audio_dir=str(audio_dir))
        assert resolved == default

    def test_custom_when_uploaded(self, tmp_path: Path) -> None:
        integration_dir = tmp_path / "integration"
        (integration_dir / "static").mkdir(parents=True)
        (integration_dir / "static" / "pre_announce.wav").write_bytes(WAV_DATA)
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        custom = audio_dir / CUSTOM_CHIME_FILENAME
        custom.write_bytes(WAV_DATA)

        resolved = resolve_chime_wav(integration_dir=integration_dir, audio_dir=str(audio_dir))
        assert resolved == custom


class TestChimePublicUrl:
    def test_none_without_custom(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        assert chime_public_url("http://host", audio_dir=str(audio_dir), deployment="docker") is None

    def test_docker_url(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / CUSTOM_CHIME_FILENAME).write_bytes(WAV_DATA)
        url = chime_public_url("http://host/", audio_dir=str(audio_dir), deployment="docker")
        assert url == f"http://host/audio/{CUSTOM_CHIME_FILENAME}"

    def test_ha_url(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / CUSTOM_CHIME_FILENAME).write_bytes(WAV_DATA)
        url = chime_public_url("http://ha:8123", audio_dir=str(audio_dir), deployment="ha")
        assert url == f"http://ha:8123/local/home_intercom_audio/{CUSTOM_CHIME_FILENAME}"


class TestChimeUpload:
    def test_write_and_delete(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        write_custom_chime_wav(WAV_DATA, str(audio_dir))
        assert has_custom_chime(str(audio_dir))
        assert delete_custom_chime(str(audio_dir)) is True
        assert not has_custom_chime(str(audio_dir))
        assert delete_custom_chime(str(audio_dir)) is False

    def test_rejects_non_wav(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a wav"):
            write_custom_chime_wav(b"not wav", str(tmp_path / "audio"))

    def test_status_payload_default(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        payload = chime_status_payload(
            base_url="http://host", audio_dir=str(audio_dir), deployment="docker"
        )
        assert payload["custom"] is False
        assert payload["url"] == DEFAULT_CHIME_STATIC_URL

    def test_status_payload_custom(self, tmp_path: Path) -> None:
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        (audio_dir / CUSTOM_CHIME_FILENAME).write_bytes(WAV_DATA)
        payload = chime_status_payload(
            base_url="http://host", audio_dir=str(audio_dir), deployment="docker"
        )
        assert payload["custom"] is True
        assert payload["url"] == f"http://host/audio/{CUSTOM_CHIME_FILENAME}"
