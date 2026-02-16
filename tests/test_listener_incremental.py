from __future__ import annotations

import unittest

from slackclaw.listener import SlackChannelListener


class FakeSlackClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def conversations_history(
        self,
        *,
        channel_id: str,
        oldest: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        self.calls.append(
            {
                "channel_id": channel_id,
                "oldest": oldest,
                "limit": limit,
                "cursor": cursor,
            }
        )
        if not self.pages:
            return {"ok": True, "messages": [], "has_more": False}
        return self.pages.pop(0)


class ListenerTests(unittest.TestCase):
    def test_poll_sorts_messages_old_to_new(self) -> None:
        fake = FakeSlackClient(
            pages=[
                {
                    "ok": True,
                    "messages": [
                        {"ts": "2.0", "user": "U2", "text": "newer"},
                        {"ts": "1.0", "user": "U1", "text": "older"},
                    ],
                    "has_more": False,
                }
            ]
        )
        listener = SlackChannelListener(fake, channel_id="C111", batch_size=50)
        result = listener.poll(last_ts="0.9")
        self.assertEqual([m.ts for m in result.messages], ["1.0", "2.0"])
        self.assertEqual(result.newest_ts, "2.0")
        self.assertEqual(fake.calls[0]["oldest"], "0.9")
        self.assertEqual(fake.calls[0]["limit"], 50)

    def test_poll_handles_multiple_pages(self) -> None:
        fake = FakeSlackClient(
            pages=[
                {
                    "ok": True,
                    "messages": [{"ts": "3.0", "user": "U3", "text": "page1"}],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "c1"},
                },
                {
                    "ok": True,
                    "messages": [{"ts": "4.0", "user": "U4", "text": "page2"}],
                    "has_more": False,
                },
            ]
        )
        listener = SlackChannelListener(fake, channel_id="C111", batch_size=100, max_pages=3)
        result = listener.poll(last_ts="2.5")
        self.assertEqual([m.ts for m in result.messages], ["3.0", "4.0"])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["cursor"], "c1")


if __name__ == "__main__":
    unittest.main()
