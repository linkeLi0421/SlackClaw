from __future__ import annotations

import json
import unittest

from slackclaw.listener import SlackSocketModeListener


class FakeSlackClient:
    def __init__(self) -> None:
        self.calls = 0
        self.app_token = ""

    def apps_connections_open(self, *, app_token: str) -> dict:
        self.calls += 1
        self.app_token = app_token
        return {"ok": True, "url": "wss://example.test/socket"}


class FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[str] = []
        self.closed = False

    def recv(self) -> str:
        if not self._frames:
            raise RuntimeError("no frame")
        return self._frames.pop(0)

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


class WebSocketTimeoutException(Exception):
    pass


class TimeoutSocket:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def recv(self) -> str:
        self.calls += 1
        raise WebSocketTimeoutException("timed out")

    def send(self, payload: str) -> None:
        return

    def close(self) -> None:
        self.closed = True


class SocketListenerTests(unittest.TestCase):
    def test_receive_message_event_acknowledges_envelope(self) -> None:
        frame = json.dumps(
            {
                "envelope_id": "env-1",
                "payload": {
                    "event": {
                        "type": "message",
                        "channel": "C111",
                        "user": "U1",
                        "ts": "1.1",
                        "text": "!do build",
                    }
                },
            }
        )
        fake_socket = FakeSocket([frame])
        fake_client = FakeSlackClient()
        listener = SlackSocketModeListener(
            fake_client,  # type: ignore[arg-type]
            app_token="xapp-test",
            command_channel_id="C111",
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        batch = listener.receive(timeout_seconds=1.0)

        self.assertEqual(fake_client.calls, 1)
        self.assertEqual(fake_client.app_token, "xapp-test")
        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.messages[0].ts, "1.1")
        self.assertEqual(len(batch.reactions), 0)
        self.assertEqual(fake_socket.sent, ['{"envelope_id":"env-1"}'])

    def test_receive_reaction_event(self) -> None:
        frame = json.dumps(
            {
                "envelope_id": "env-2",
                "payload": {
                    "event": {
                        "type": "reaction_added",
                        "user": "U2",
                        "reaction": "white_check_mark",
                        "item": {
                            "type": "message",
                            "channel": "C111",
                            "ts": "2.2",
                        },
                    }
                },
            }
        )
        fake_socket = FakeSocket([frame])
        listener = SlackSocketModeListener(
            FakeSlackClient(),  # type: ignore[arg-type]
            app_token="xapp-test",
            command_channel_id="C111",
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        batch = listener.receive(timeout_seconds=1.0)

        self.assertEqual(len(batch.messages), 0)
        self.assertEqual(len(batch.reactions), 1)
        self.assertEqual(batch.reactions[0].channel_id, "C111")
        self.assertEqual(batch.reactions[0].message_ts, "2.2")
        self.assertEqual(batch.reactions[0].reaction, "white_check_mark")

    def test_disconnect_event_closes_socket(self) -> None:
        frame = json.dumps({"type": "disconnect", "envelope_id": "env-3"})
        fake_socket = FakeSocket([frame])
        listener = SlackSocketModeListener(
            FakeSlackClient(),  # type: ignore[arg-type]
            app_token="xapp-test",
            command_channel_id="C111",
            socket_factory=lambda _url, _timeout: fake_socket,
        )

        batch = listener.receive(timeout_seconds=1.0)

        self.assertEqual(len(batch.messages), 0)
        self.assertEqual(len(batch.reactions), 0)
        self.assertTrue(fake_socket.closed)

    def test_timeout_does_not_drop_socket(self) -> None:
        timeout_socket = TimeoutSocket()
        fake_client = FakeSlackClient()
        listener = SlackSocketModeListener(
            fake_client,  # type: ignore[arg-type]
            app_token="xapp-test",
            command_channel_id="C111",
            socket_factory=lambda _url, _timeout: timeout_socket,
        )

        batch_one = listener.receive(timeout_seconds=1.0)
        batch_two = listener.receive(timeout_seconds=1.0)

        self.assertEqual(len(batch_one.messages), 0)
        self.assertEqual(len(batch_one.reactions), 0)
        self.assertEqual(len(batch_two.messages), 0)
        self.assertEqual(len(batch_two.reactions), 0)
        self.assertEqual(fake_client.calls, 1)
        self.assertEqual(timeout_socket.calls, 2)
        self.assertFalse(timeout_socket.closed)


if __name__ == "__main__":
    unittest.main()
