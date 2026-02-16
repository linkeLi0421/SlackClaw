from __future__ import annotations

from dataclasses import dataclass

from .models import SlackMessage
from .slack_api import SlackWebClient


def _ts_as_float(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class PollResult:
    messages: list[SlackMessage]
    newest_ts: str | None


class SlackChannelListener:
    def __init__(
        self,
        client: SlackWebClient,
        *,
        channel_id: str,
        batch_size: int = 100,
        max_pages: int = 3,
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._batch_size = batch_size
        self._max_pages = max_pages

    def poll(self, *, last_ts: str | None) -> PollResult:
        cursor = None
        pages = 0
        fetched: list[dict] = []

        while pages < self._max_pages:
            payload = self._client.conversations_history(
                channel_id=self._channel_id,
                oldest=last_ts,
                limit=self._batch_size,
                cursor=cursor,
            )
            page_messages = payload.get("messages") or []
            if isinstance(page_messages, list):
                fetched.extend(page_messages)

            pages += 1
            if not payload.get("has_more"):
                break
            cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

        normalized: list[SlackMessage] = []
        for raw in fetched:
            if not isinstance(raw, dict):
                continue
            ts = str(raw.get("ts") or "")
            if not ts:
                continue
            normalized.append(
                SlackMessage(
                    channel_id=self._channel_id,
                    ts=ts,
                    user=str(raw.get("user") or raw.get("bot_id") or "unknown"),
                    text=str(raw.get("text") or ""),
                    raw=raw,
                )
            )

        normalized.sort(key=lambda message: _ts_as_float(message.ts))
        newest_ts = normalized[-1].ts if normalized else None
        return PollResult(messages=normalized, newest_ts=newest_ts)
