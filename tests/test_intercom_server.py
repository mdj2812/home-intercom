"""Flask route tests — using app.test_client() to simulate requests."""

import io
import os
import tempfile
from unittest.mock import patch

import pytest

# src in pythonpath via pyproject.toml
from intercom_server import WAV_HEADER_SIZE, app


@pytest.fixture
def client():
    """Flask test client."""
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
        data = resp.json
        assert data is not None
        assert "living" in data
        assert data["living"]["name"] == "\u5ba2\u5385"

    def test_static_icon_192(self, client):
        """icon-192.png exists in src/static/."""
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
        data = resp.json
        assert "version" in data
        assert "pcm_rate" in data


class TestRoomsStatus:
    def test_returns_500_without_token(self, client, monkeypatch):
        monkeypatch.setattr("intercom_server.HA_TOKEN", "")
        resp = client.get("/rooms/status")
        assert resp.status_code == 500
        data = resp.json
        assert "error" in data


class TestRecordValidation:
    def test_no_target(self, client):
        resp = client.post("/record")
        assert resp.status_code == 400
        data = resp.json
        assert "missing target" in data["error"]

    def test_no_audio_data(self, client):
        resp = client.post("/record?target=living", data=b"")
        assert resp.status_code == 400
        data = resp.json
        assert "no audio" in data["error"].lower()

    def test_unknown_target(self, client):
        resp = client.post("/record?target=mars", data=b"x" * WAV_HEADER_SIZE)
        assert resp.status_code == 400
        data = resp.json
        assert "unknown" in data["error"].lower()

    def test_target_all_with_no_rooms(self, client, monkeypatch):
        import intercom_server

        monkeypatch.setattr(intercom_server, "ROOM_MAP", {})
        resp = client.post("/record?target=all", data=b"x" * WAV_HEADER_SIZE)
        assert resp.status_code == 500
        data = resp.json
        assert "no rooms" in data["error"].lower()


class TestHandleWavPassthrough:
    def test_valid_pcm_wav(self):
        import wave

        from intercom_server import _handle_wav_passthrough

        # Create a valid minimal WAV in memory
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)  # 0.5s of silence

        wav_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            rate, duration = _handle_wav_passthrough(wav_bytes, tmp_path)
            assert rate == 16000
            assert duration == pytest.approx(0.5, rel=0.01)
            assert os.path.exists(tmp_path)
            assert os.path.getsize(tmp_path) == len(wav_bytes)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestHandlePcmToWav:
    def test_creates_valid_wav(self):
        import wave

        from intercom_server import _handle_pcm_to_wav

        # 0.1s of 16-bit mono 16000 Hz PCM
        pcm = b"\x00\x00" * 1600

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            duration = _handle_pcm_to_wav(pcm, 16000, tmp_path)
            assert duration == pytest.approx(0.1, rel=0.01)
            assert os.path.exists(tmp_path)

            with wave.open(tmp_path, "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2
                assert wf.getframerate() == 16000
                assert wf.getnframes() == 1600
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestRecordPcmBranch:
    """Test /record with raw PCM (PWA path)."""

    def test_pcm_triggers_wav_writing(self, client, monkeypatch, tmp_path):
        import intercom_server

        # 0.1s of 16-bit mono 16000 Hz PCM
        pcm = b"\x00\x00" * 1600

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))

        with patch.object(intercom_server.haclient, "play_and_auto_pause",
                          return_value=True):
            resp = client.post("/record?target=living&rate=16000", data=pcm)

        assert resp.status_code == 200
        data = resp.json
        assert data["ok"] is True
        assert data["rooms_sent"] == 1

        # Check WAV file was written
        wav_path = os.path.join(str(tmp_path), "intercom_living.wav")
        assert os.path.exists(wav_path)

        with __import__("wave").open(wav_path, "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getnchannels() == 1


class TestRecordWavPassthroughBranch:
    """Test /record with complete WAV (ESP32 path)."""

    def test_wav_passthrough(self, client, monkeypatch, tmp_path):
        import wave

        import intercom_server

        # Create a valid WAV
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 8000)
        wav_data = buf.getvalue()

        monkeypatch.setattr(intercom_server, "AUDIO_DIR", str(tmp_path))

        with patch.object(intercom_server.haclient, "play_and_auto_pause",
                          return_value=True):
            resp = client.post("/record?target=living", data=wav_data)

        assert resp.status_code == 200
        data = resp.json
        assert data["ok"] is True

        wav_path = os.path.join(str(tmp_path), "intercom_living.wav")
        assert os.path.exists(wav_path)
        assert os.path.getsize(wav_path) == len(wav_data)
