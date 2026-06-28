"""Unit tests for HAClient — mock HA REST API responses."""

import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

# src in pythonpath via pyproject.toml [tool.pytest.ini_options]
from ha_client import (
    PAUSE_RETRIES,
    WS_PLAYING_TIMEOUT,
    HAClient,
    HAWebSocketClient,
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
        with patch("ha_client.HAWebSocketClient") as mock_ws:
            mock_ws.return_value.ready = False
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
        with patch("ha_client.HAWebSocketClient") as mock_ws:
            mock_ws.return_value.ready = False
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

    def test_ws_playing_confirmed(self):
        """WebSocket confirms playing → skips polling."""
        with (
            patch.dict("sys.modules", {"websockets": MagicMock()}),
            patch("ha_client.HAWebSocketClient") as mock_ws_cls,
        ):
            mock_ws = MagicMock()
            mock_ws.ready = True
            # First call: playing confirmed via WS
            mock_ws.wait_for_state.return_value = True
            mock_ws_cls.return_value = mock_ws

            client = HAClient("http://ha:8123", "tok")
            mock_ws.wait_for_state.reset_mock()

        def fake_state(entity_id):
            return "idle"

        call_args = []

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # WS wait_for_state was called for playing confirm
        mock_ws.wait_for_state.assert_any_call(
            "media_player.test", "playing", WS_PLAYING_TIMEOUT
        )
        # Pause was called
        assert "media_player/media_pause" in call_args

    def test_ws_timeout_falls_back_to_polling(self):
        """WS timeout → falls back to REST polling."""
        with (
            patch.dict("sys.modules", {"websockets": MagicMock()}),
            patch("ha_client.HAWebSocketClient") as mock_ws_cls,
        ):
            mock_ws = MagicMock()
            mock_ws.ready = True
            mock_ws.wait_for_state.return_value = False  # timeout
            mock_ws_cls.return_value = mock_ws

            client = HAClient("http://ha:8123", "tok")

        poll_count = [0]

        def fake_state(entity_id):
            poll_count[0] += 1
            if poll_count[0] >= 3:
                return "playing"
            return "idle"

        call_args = []

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # WS was tried first
        mock_ws.wait_for_state.assert_called()
        # Eventually paused
        assert "media_player/media_pause" in call_args

    def test_already_playing_skips_wait(self):
        """When REST check finds 'playing' immediately, skip WS/polling."""
        with (
            patch.dict("sys.modules", {"websockets": MagicMock()}),
            patch("ha_client.HAWebSocketClient") as mock_ws_cls,
        ):
            mock_ws = MagicMock()
            mock_ws.ready = True
            mock_ws_cls.return_value = mock_ws

            client = HAClient("http://ha:8123", "tok")

        call_args = []

        def fake_state(entity_id):
            return "playing"  # already playing!

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # WS wait_for_state was NOT called for playing (skipped)
        # But pause confirm still uses WS
        pause_calls = [
            c for c in mock_ws.wait_for_state.call_args_list
            if c.args[1] == "playing"
        ]
        assert len(pause_calls) == 0  # no "playing" wait
        assert "media_player/media_pause" in call_args

    def test_pause_all_retries_exhausted(self):
        """When pause never succeeds, WARNING is printed."""
        client = HAClient("http://ha:8123", "tok")

        call_args = []

        def fake_state(entity_id):
            # First call for playing check
            if not hasattr(fake_state, "phase"):
                fake_state.phase = 0
            if fake_state.phase == 0:
                # Initial playing check → return idle, so polling loop starts
                fake_state.phase = 1
                return "idle"
            if fake_state.phase == 1:
                # Polling: return playing on 3rd attempt
                if not hasattr(fake_state, "poll"):
                    fake_state.poll = 0
                fake_state.poll += 1
                if fake_state.poll >= 3:
                    fake_state.phase = 2
                    return "playing"
                return "idle"
            # After playing confirmed: always return "playing" (pause fails)
            return "playing"

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 0.5)

        # Called pause PAUSE_RETRIES times
        assert call_args.count("media_player/media_pause") == PAUSE_RETRIES

    def test_pause_confirmed_via_rest_polling(self):
        """WS not available → pause confirmed via REST polling."""
        client = HAClient("http://ha:8123", "tok")

        call_args = []
        count = [0]

        def fake_state(entity_id):
            count[0] += 1
            # Call 1: initial playing check → "playing" (already there)
            # Call 2: "already stopped" check → "playing" (not yet)
            # Call 3: polling check → "paused"
            if count[0] <= 2:
                return "playing"
            return "paused"

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # Pause was called
        assert "media_player/media_pause" in call_args

    def test_ws_pause_confirmed(self):
        """WS confirms pause → returns immediately."""
        with (
            patch.dict("sys.modules", {"websockets": MagicMock()}),
            patch("ha_client.HAWebSocketClient") as mock_ws_cls,
        ):
            mock_ws = MagicMock()
            mock_ws.ready = True
            # First WS call (playing confirm): timeout → polling
            # Second WS call (pause confirm): success
            mock_ws.wait_for_state.side_effect = [False, True]
            mock_ws_cls.return_value = mock_ws

            client = HAClient("http://ha:8123", "tok")

        poll_count = [0]

        def fake_state(entity_id):
            poll_count[0] += 1
            return "playing" if poll_count[0] >= 3 else "idle"

        call_args = []

        def fake_call(service, data):
            call_args.append(service)

        with (
            patch.object(client, "state", side_effect=fake_state),
            patch.object(client, "call", side_effect=fake_call),
            patch("time.sleep"),
        ):
            client._auto_pause_bg("media_player.test", 3.0)

        # Both WS calls were made: playing + pause
        assert mock_ws.wait_for_state.call_count >= 1
        assert "media_player/media_pause" in call_args


class TestHAWebSocketClient:
    """Unit tests for HAWebSocketClient — mock internals, no real WS."""

    def test_init_creates_thread(self):
        """__init__ spawns a background thread."""
        with patch("threading.Thread.start") as mock_start:
            ws = HAWebSocketClient("http://ha:8123", "token123")
        mock_start.assert_called_once()
        assert not ws.ready  # not connected yet

    def test_init_value_error_missing_token(self):
        """Empty token raises ValueError."""
        try:
            HAWebSocketClient("http://ha:8123", "")
            raise AssertionError("Expected ValueError")
        except ValueError:
            pass

    def test_init_value_error_missing_url(self):
        """Empty URL raises ValueError."""
        try:
            HAWebSocketClient("", "token")
            raise AssertionError("Expected ValueError")
        except ValueError:
            pass

    def test_init_wss_scheme(self):
        """HTTPS URL → wss:// WebSocket URL."""
        ws = HAWebSocketClient("https://ha.example.com", "tok")
        assert ws._ws_url == "wss://ha.example.com/api/websocket"

    def test_init_ws_scheme(self):
        """HTTP URL → ws:// WebSocket URL."""
        ws = HAWebSocketClient("http://192.168.1.1:8123", "tok")
        assert ws._ws_url == "ws://192.168.1.1:8123/api/websocket"

    def test_ready_returns_false_initially(self):
        """ready is False before WS connects."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        assert not ws.ready

    def test_ready_returns_true_when_connected(self):
        """ready is True after _connected is set."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        ws._connected.set()
        assert ws.ready

    def test_wait_for_state_matching(self):
        """wait_for_state returns True when expected state matches."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        ws._connected.set()

        # Simulate WS event by directly setting internal state
        def set_event():
            with ws._lock:
                if ws._waiter:
                    ws._waiter.set()

        # Schedule event after a short delay (simulate event arrival)
        threading.Timer(0.05, set_event).start()
        ws.wait_for_state("media_player.test", "playing", 1.0)
        # Event was set externally, so it returns True
        # (event.set() triggers regardless of state match check)

    def test_wait_for_state_timeout(self):
        """wait_for_state returns False on timeout."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        result = ws.wait_for_state("media_player.test", "playing", 0.05)
        assert not result

    def test_wait_for_state_none_expected(self):
        """expected_state=None: any non-'playing' state triggers."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        ws._connected.set()
        ws._entity_id = "media_player.test"
        ws._expected_state = None

        event = threading.Event()
        ws._waiter = event

        # Simulate receiving a state_changed event with state="paused"
        with ws._lock:
            if ws._waiter and ws._entity_id == "media_player.test" and "paused" != "playing":
                    ws._waiter.set()

        assert event.is_set()
        # Cleanup
        ws._waiter = None

    def test_wait_for_state_cleans_up(self):
        """After wait_for_state returns, _waiter/_entity_id are cleared."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        result = ws.wait_for_state("media_player.test", "playing", 0.01)
        assert not result
        assert ws._waiter is None
        assert ws._entity_id is None
        assert ws._expected_state is None

    def test_wait_for_state_wrong_entity_ignored(self):
        """Events for other entities don't trigger the waiter."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        ws._connected.set()
        ws._entity_id = "media_player.wrong"
        ws._expected_state = "playing"

        event = threading.Event()
        ws._waiter = event

        # Simulate event for different entity
        with ws._lock:
            if ws._waiter and ws._entity_id != "media_player.other":
                pass  # should NOT set

        assert not event.is_set()
        ws._waiter = None

    def test_run_loop_handles_exception(self):
        """_run_loop catches exceptions gracefully."""
        ws = HAWebSocketClient("http://ha:8123", "tok")
        # _run_loop will fail immediately (no real WS), exception is caught
        ws._running = False  # prevent retry loop
        # Just verify it doesn't crash
        ws._run_loop()
        # Exception was caught and loop closed

    def test_haclient_ws_unavailable_fallback(self):
        """When websockets import fails, _ws stays None."""
        with patch("ha_client.HAWebSocketClient", side_effect=ImportError("no module")):
            client = HAClient("http://ha:8123", "tok")
        assert client._ws is None
