import json
import unittest
from unittest.mock import patch

from home_assistant import (
    BED_ENTITY_ID,
    CHAIR_ENTITY_ID,
    ENTITY_IDS,
    HomeAssistantStateMonitor,
    websocket_url,
)


class FakeSocket:
    def __init__(self, responses, events=()):
        self.responses = iter(responses)
        self.events = iter(events)
        self.sent = []

    async def recv(self):
        return json.dumps(next(self.responses))

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration


class FakeConnection:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_args):
        return None


class HomeAssistantStateMonitorTest(unittest.IsolatedAsyncioTestCase):
    def test_websocket_url_accepts_http_host_or_complete_websocket_url(self):
        self.assertEqual(
            websocket_url("http://homeassistant.local:8123"),
            "ws://homeassistant.local:8123/api/websocket",
        )
        self.assertEqual(
            websocket_url("https://ha.example.com/base/"),
            "wss://ha.example.com/base/api/websocket",
        )
        self.assertEqual(
            websocket_url("ws://10.0.0.5:8123/api/websocket"),
            "ws://10.0.0.5:8123/api/websocket",
        )

    async def test_connects_loads_initial_states_and_subscribes_to_only_two_entities(self):
        socket = FakeSocket(
            responses=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": [
                        {"entity_id": CHAIR_ENTITY_ID, "state": "on", "last_updated": "chair-1"},
                        {"entity_id": BED_ENTITY_ID, "state": "off", "last_updated": "bed-1"},
                        {"entity_id": "light.unrelated", "state": "on"},
                    ],
                },
                {"id": 2, "type": "result", "success": True, "result": None},
            ],
            events=[{
                "id": 2,
                "type": "event",
                "event": {
                    "variables": {
                        "trigger": {
                            "to_state": {
                                "entity_id": CHAIR_ENTITY_ID,
                                "state": "off",
                                "last_updated": "chair-2",
                            }
                        }
                    }
                },
            }],
        )
        monitor = HomeAssistantStateMonitor("http://ha:8123", "secret")

        with patch("home_assistant.websockets.connect", return_value=FakeConnection(socket)):
            with self.assertRaisesRegex(ConnectionError, "closed"):
                await monitor._connect_once()

        self.assertEqual(socket.sent[0], {"type": "auth", "access_token": "secret"})
        self.assertEqual(socket.sent[1], {"id": 1, "type": "get_states"})
        self.assertEqual(socket.sent[2]["type"], "subscribe_trigger")
        self.assertEqual(socket.sent[2]["trigger"]["entity_id"], list(ENTITY_IDS))
        snapshot = monitor.snapshot()
        self.assertFalse(snapshot["zones"]["chair"]["occupied"])
        self.assertFalse(snapshot["zones"]["bed"]["occupied"])
        self.assertEqual(snapshot["zones"]["chair"]["last_updated"], "chair-2")

    async def test_authentication_failure_does_not_expose_token_in_error(self):
        socket = FakeSocket([
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "Invalid access token"},
        ])
        monitor = HomeAssistantStateMonitor("http://ha:8123", "very-secret")

        with self.assertRaisesRegex(PermissionError, "Invalid access token") as error:
            await monitor._authenticate(socket)

        self.assertNotIn("very-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
