"""Unit tests for HAClient — mock HA REST API responses."""

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

# src in pythonpath via pyproject.toml [tool.pytest.ini_options]
from ha_client import (
    SUPPORT_REPEAT_SET,
    HAClient,
)


class TestHAClientInit:
    def test_parses_http_url(self):
        client = HAClient("http://192.168.1.1:8123", "token123")
        assert client._base == "http://192.168.1.1:8123/api"
        assert client._token == "token123"

    def test_parses_https_url_with_path(self):
        client = HAClient("https://ha.example.com", "secret")
        assert client._base == "https://ha.example.com/api"

    def test_parses_url_trailing_slash(self):
        client = HAClient("http://10.0.0.1:8123/", "t")
        assert client._base == "http://10.0.0.1:8123/api"


class TestHAClientRequest:
    def test_get_request_returns_json(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": "playing"}'

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            code, result = client._request("GET", "/states/test.entity")

        assert code == 200
        assert result == {"state": "playing"}
        mock_open.assert_called_once()

    def test_post_request_sends_json_body(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            code, _ = client._request("POST", "/services/test", data={"key": "val"})

        assert code == 200

    def test_http_error_returns_code(self):
        client = HAClient("http://ha:8123", "tok")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "msg", {}, None),
        ):
            code, result = client._request("GET", "/states/x")

        assert code == 500
        assert "HTTP 500" in result

    def test_network_error_returns_zero(self):
        client = HAClient("http://ha:8123", "tok")

        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            code, result = client._request("GET", "/states/x")

        assert code == 0
        assert "no route" in result


class TestHAClientState:
    def test_returns_state_string(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": "playing"}'

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.state("media_player.test") == "playing"

    def test_returns_empty_on_http_error(self):
        client = HAClient("http://ha:8123", "tok")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 404, "not found", {}, None),
        ):
            assert client.state("media_player.test") == ""

    def test_returns_empty_without_token(self):
        client = HAClient("http://ha:8123", "")
        assert client.state("anything") == ""

    def test_returns_unavailable_state(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": "unavailable"}'

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.state("media_player.offline") == "unavailable"


class TestHAClientCall:
    def test_call_returns_true_on_success(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.call("media_player/play_media", {"entity_id": "x"}) is True

    def test_call_returns_false_on_failure(self):
        client = HAClient("http://ha:8123", "tok")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "boom", {}, None),
        ):
            assert client.call("media_player/play_media", {"entity_id": "x"}) is False

    def test_call_returns_false_without_token(self):
        client = HAClient("http://ha:8123", "")
        assert client.call("service", {}) is False


class TestHAClientQueryStatuses:
    def test_all_available(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": "playing"}'

        room_map = {
            "living": {"entity": "media_player.living"},
            "bedroom": {"entity": "media_player.bedroom"},
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.query_statuses(room_map)

        assert result == {"living": True, "bedroom": True}

    def test_unavailable(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": "unavailable"}'

        room_map = {"living": {"entity": "media_player.living"}}

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.query_statuses(room_map)

        assert result == {"living": False}

    def test_no_entity_defaults_available(self):
        client = HAClient("http://ha:8123", "tok")
        room_map = {"broadcast": {"name": "All"}}

        with patch("urllib.request.urlopen") as mock_open:
            result = client.query_statuses(room_map)

        assert result == {"broadcast": True}
        mock_open.assert_not_called()

    def test_empty_state_treated_as_false(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"state": ""}'

        room_map = {"living": {"entity": "media_player.living"}}

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = client.query_statuses(room_map)

        assert result == {"living": False}


class TestHAClientPlayAndAutoPause:
    def test_calls_play_service_and_spawns_thread(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("threading.Thread.start") as mock_start,
        ):
            client.play_and_auto_pause("media_player.test", "http://ha/audio/test.wav", 2.0)

        mock_start.assert_called_once()

    def test_play_failure_does_not_spawn_thread(self):
        client = HAClient("http://ha:8123", "tok")

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("url", 500, "err", {}, None),
            ),
            patch("threading.Thread.start") as mock_start,
        ):
            client.play_and_auto_pause("media_player.test", "http://ha/audio/test.wav", 2.0)

        mock_start.assert_not_called()


class TestAutoPauseBg:
    def test_confirms_playing_then_pauses(self):
        """Simulate full cycle: playing → wait → pause → confirmed stopped."""
        client = HAClient("http://ha:8123", "tok")

        call_args = []

        def fake_state(entity_id):
            # Return states: idle → playing (detected on 2nd poll) → idle after pause
            if not hasattr(fake_state, "count"):
                fake_state.count = 0
            fake_state.count += 1
            if fake_state.count <= 2:
                return "idle"  # not yet playing
            elif fake_state.count == 3:
                return "playing"  # detected!
            else:
                return "idle"  # after pause

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # Should have called pause at least once
        assert "media_player/media_pause" in call_args

    def test_short_audio_no_playing_detected(self):
        """Short audio: polling never catches 'playing' — still pauses."""
        client = HAClient("http://ha:8123", "tok")

        call_args = []

        def fake_state(entity_id):
            return "idle"

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 0.5)

        # Should still try to pause even if playing was never detected
        assert "media_player/media_pause" in call_args

    def test_pause_buffer_added_to_sleep(self):
        """pause_buffer=1.5 adds 1.5s to the remaining sleep time."""
        client = HAClient("http://ha:8123", "tok", pause_buffer=1.5)

        def fake_state(entity_id):
            return "playing"  # immediately confirmed

        def fake_call(service, data):
            pass

        sleep_calls = []

        def fake_sleep(sec):
            sleep_calls.append(sec)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep", side_effect=fake_sleep),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # elapsed ≈ 0, so remaining = 3.0 + 1.5 = 4.5
        assert sleep_calls, "sleep should be called"
        assert sleep_calls[0] >= 4.0, f"expected >=4.0, got {sleep_calls[0]}"

    def test_play_and_auto_pause_returns_true_on_success(self):
        """play_and_auto_pause should return True when play_media succeeds."""
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("threading.Thread.start"),
        ):
            result = client.play_and_auto_pause(
                "media_player.test", "http://ha/audio/test.wav", 2.0
            )

        assert result is True

    def test_play_and_auto_pause_returns_false_on_failure(self):
        """play_and_auto_pause should return False when play_media fails."""
        client = HAClient("http://ha:8123", "tok")

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("url", 500, "err", {}, None),
            ),
            patch("threading.Thread.start") as mock_start,
        ):
            result = client.play_and_auto_pause(
                "media_player.test", "http://ha/audio/test.wav", 2.0
            )

        assert result is False
        mock_start.assert_not_called()

    def test_repeat_set_path_skips_auto_pause(self):
        """When repeat_set is supported, no background thread is spawned."""
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"

        with (
            patch.object(client, "supports_repeat_set", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("threading.Thread.start") as mock_start,
        ):
            result = client.play_and_auto_pause(
                "media_player.test", "http://ha/audio/test.wav", 2.0
            )

        assert result is True
        mock_start.assert_not_called()


class TestSupportsRepeatSet:
    def test_returns_true_when_bit_set(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(
            {
                "state": "idle",
                "attributes": {"supported_features": SUPPORT_REPEAT_SET},
            }
        ).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.supports_repeat_set("media_player.test") is True

    def test_returns_false_when_bit_not_set(self):
        client = HAClient("http://ha:8123", "tok")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(
            {
                "state": "idle",
                "attributes": {"supported_features": 0},
            }
        ).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert client.supports_repeat_set("media_player.test") is False

    def test_returns_false_without_token(self):
        client = HAClient("http://ha:8123", "")
        assert client.supports_repeat_set("media_player.test") is False

    def test_returns_false_on_api_error(self):
        client = HAClient("http://ha:8123", "tok")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "err", {}, None),
        ):
            assert client.supports_repeat_set("media_player.test") is False
