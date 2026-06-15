"""Flask 路由测试 — 用 app.test_client() 模拟请求"""

import io
import json
import os
import subprocess
import tempfile
from unittest.mock import patch

import pytest

# src in pythonpath via pyproject.toml
from intercom_server import TMP_PREFIX, app


@pytest.fixture
def client():
    """Flask test client"""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestStaticRoutes:
    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<html" in resp.data

    def test_rooms_json(self, client):
        resp = client.get("/rooms.json")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "living" in data
        assert data["living"]["name"] == "客厅"

    def test_static_icon_192(self, client):
        """icon-192.png exists in src/static/ — manifest is copied there at build time"""
        resp = client.get("/static/icon-192.png")
        assert resp.status_code == 200
        assert resp.content_length > 0

    def test_audio_file_not_found(self, client):
        resp = client.get("/audio/nonexistent.wav")
        assert resp.status_code == 404


class TestVersionRoute:
    def test_returns_version_json(self, client):
        resp = client.get("/version")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "version" in data


class TestRoomsStatus:
    def test_returns_500_without_token(self, client, monkeypatch):
        """Patch module-level HA_TOKEN to empty so endpoint returns 500"""
        monkeypatch.setattr("intercom_server.HA_TOKEN", "")
        resp = client.get("/rooms/status")
        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert "error" in data


class TestConvertValidation:
    def test_no_audio_data(self, client):
        resp = client.post("/convert?target=living")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "no audio" in data["error"].lower()

    def test_unknown_target(self, client):
        resp = client.post("/convert?target=mars", data=b"some audio")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "unknown" in data["error"].lower()

    def test_target_all_with_no_rooms(self, client, monkeypatch):
        """When ROOM_MAP has no entities, broadcast should fail"""
        import intercom_server

        monkeypatch.setattr(intercom_server, "ROOM_MAP", {})
        resp = client.post("/convert?target=all", data=b"test")
        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert "no rooms" in data["error"].lower()


class TestHandleWavPassthrough:
    def test_valid_pcm_wav(self):
        import wave

        from intercom_server import _handle_wav_passthrough

        # Create a valid minimal WAV in memory
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)  # 0.5s of silence

        wav_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            duration = _handle_wav_passthrough(wav_bytes, tmp_path)
            assert duration == pytest.approx(0.5, rel=0.01)
            assert os.path.exists(tmp_path)
            assert os.path.getsize(tmp_path) == len(wav_bytes)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestHandleWebmConvert:
    def test_ffmpeg_success(self):
        from intercom_server import _handle_webm_convert

        fake_webm = b"fake_webm_content"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_tmp:
            wav_path = wav_tmp.name

        try:
            with patch("subprocess.run") as mock_run:
                # Simulate ffmpeg producing a WAV file
                def side_effect(*args, **kwargs):
                    # Create a fake output WAV
                    import wave

                    with wave.open(wav_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(16000)
                        wf.writeframes(b"\x00\x00" * 16000)  # exactly 1.0s
                    return subprocess.CompletedProcess(args, 0)

                mock_run.side_effect = side_effect

                duration = _handle_webm_convert(fake_webm, "test", wav_path)

                assert duration == pytest.approx(1.0, rel=0.01)
                # tmp webm should be cleaned up
                webm_tmp = f"{TMP_PREFIX}test.webm"
                assert not os.path.exists(webm_tmp)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)

    def test_ffmpeg_failure_raises(self):
        from intercom_server import _handle_webm_convert

        fake_webm = b"corrupt_data"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_tmp:
            wav_path = wav_tmp.name

        try:
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.CalledProcessError(
                    1, "ffmpeg", stderr=b"Invalid data"
                )

                with pytest.raises(subprocess.CalledProcessError):
                    _handle_webm_convert(fake_webm, "test", wav_path)

                # tmp webm should still be cleaned on failure
                webm_tmp = f"{TMP_PREFIX}test.webm"
                assert not os.path.exists(webm_tmp)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)


class TestWavMagicDetection:
    """Test the WAV magic byte detection in convert()"""

    def test_wav_bytes_routed_to_passthrough(self, client, monkeypatch, tmp_path):
        import intercom_server

        # Create a minimal WAV
        buf = io.BytesIO()
        import wave

        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)
        wav_data = buf.getvalue()

        called = []

        def fake_passthrough(raw, tmp_wav):
            called.append("passthrough")
            # Must actually create the file so shutil.move doesn't fail
            import wave

            buf = io.BytesIO(raw)
            with wave.open(buf, "rb") as wf_read:
                params = wf_read.getparams()
            with wave.open(tmp_wav, "wb") as wf:
                wf.setparams(params)
                wf.writeframes(b"\x00\x00" * wf_read.getnframes())
            return 0.5

        monkeypatch.setattr(intercom_server, "_handle_wav_passthrough", fake_passthrough)
        # Use tmp_path so shutil.move doesn't fail on cross-device /data/audio
        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))

        # Patch HAClient to avoid real HA calls
        with patch.object(intercom_server.haclient, "play_and_auto_pause"):
            resp = client.post("/convert?target=living", data=wav_data)

        assert resp.status_code == 200
        assert "passthrough" in called

    def test_non_wav_bytes_routed_to_ffmpeg(self, client, monkeypatch):
        import intercom_server

        called = []

        def fake_convert(raw, target, tmp_wav):
            called.append("ffmpeg")
            # Create a minimal output so duration calc works
            import wave

            with wave.open(tmp_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 16000)
            # Move the file to AUDIO_DIR (mocked downstream)
            return 1.0

        monkeypatch.setattr(intercom_server, "_handle_webm_convert", fake_convert)

        with patch.object(intercom_server.haclient, "play_and_auto_pause"):
            resp = client.post("/convert?target=living", data=b"not_a_wav_file")

        assert resp.status_code == 200
        assert "ffmpeg" in called
