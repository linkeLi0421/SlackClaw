from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import SlackMessage, SlackReaction
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


@dataclass(frozen=True)
class SocketEventBatch:
    messages: list[SlackMessage]
    reactions: list[SlackReaction]


class SocketConnection(Protocol):
    def recv(self) -> str | bytes:
        raise NotImplementedError

    def send(self, payload: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


SocketFactory = Callable[[str, float], SocketConnection]


def _default_socket_factory(url: str, timeout_seconds: float) -> SocketConnection:
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Socket mode requires 'websocket-client' package") from exc
    return websocket.create_connection(url, timeout=timeout_seconds, enable_multithread=True)


def _is_socket_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return exc.__class__.__name__ == "WebSocketTimeoutException"


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


class SlackSocketModeListener:
    def __init__(
        self,
        client: SlackWebClient,
        *,
        app_token: str,
        command_channel_id: str,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self._client = client
        self._app_token = app_token
        self._command_channel_id = command_channel_id
        if socket_factory is None:
            try:
                import websocket  # type: ignore  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("Socket mode requires 'websocket-client' package") from exc
            self._socket_factory = _default_socket_factory
        else:
            self._socket_factory = socket_factory
        self._socket: SocketConnection | None = None

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            return

    def receive(self, *, timeout_seconds: float) -> SocketEventBatch:
        self._ensure_socket(timeout_seconds=timeout_seconds)

        sock = self._socket
        if sock is None:
            raise RuntimeError("socket connection is not initialized")

        try:
            raw = sock.recv()
        except Exception as exc:
            if _is_socket_timeout_error(exc):
                return SocketEventBatch(messages=[], reactions=[])
            self.close()
            raise RuntimeError(f"socket recv failed: {exc}") from exc

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str) or not raw.strip():
            return SocketEventBatch(messages=[], reactions=[])

        try:
            envelope = json.loads(raw)
        except Exception:
            return SocketEventBatch(messages=[], reactions=[])
        if not isinstance(envelope, dict):
            return SocketEventBatch(messages=[], reactions=[])

        envelope_id = str(envelope.get("envelope_id") or "")
        if envelope_id:
            ack = json.dumps({"envelope_id": envelope_id}, separators=(",", ":"))
            try:
                sock.send(ack)
            except Exception as exc:
                self.close()
                raise RuntimeError(f"socket ack failed: {exc}") from exc

        if str(envelope.get("type") or "") == "disconnect":
            self.close()
            return SocketEventBatch(messages=[], reactions=[])

        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            return SocketEventBatch(messages=[], reactions=[])
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return SocketEventBatch(messages=[], reactions=[])

        event_type = str(event.get("type") or "")
        if event_type == "message":
            msg = self._parse_message_event(event)
            if msg is None:
                return SocketEventBatch(messages=[], reactions=[])
            return SocketEventBatch(messages=[msg], reactions=[])

        if event_type == "reaction_added":
            reaction = self._parse_reaction_event(event)
            if reaction is None:
                return SocketEventBatch(messages=[], reactions=[])
            return SocketEventBatch(messages=[], reactions=[reaction])

        return SocketEventBatch(messages=[], reactions=[])

    def _ensure_socket(self, *, timeout_seconds: float) -> None:
        if self._socket is not None:
            return
        payload = self._client.apps_connections_open(app_token=self._app_token)
        ws_url = str(payload.get("url") or "").strip()
        if not ws_url:
            raise RuntimeError("apps.connections.open did not return a websocket URL")
        self._socket = self._socket_factory(ws_url, timeout_seconds)

    def _parse_message_event(self, event: dict) -> SlackMessage | None:
        channel_id = str(event.get("channel") or "")
        if channel_id != self._command_channel_id:
            return None
        ts = str(event.get("ts") or "")
        if not ts:
            return None
        return SlackMessage(
            channel_id=channel_id,
            ts=ts,
            user=str(event.get("user") or event.get("bot_id") or "unknown"),
            text=str(event.get("text") or ""),
            raw=event,
        )

    def _parse_reaction_event(self, event: dict) -> SlackReaction | None:
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return None
        if str(item.get("type") or "") != "message":
            return None

        channel_id = str(item.get("channel") or "")
        if channel_id != self._command_channel_id:
            return None
        ts = str(item.get("ts") or "")
        reaction = str(event.get("reaction") or "").strip().strip(":")
        if not ts or not reaction:
            return None
        return SlackReaction(
            channel_id=channel_id,
            message_ts=ts,
            reaction=reaction,
            user=str(event.get("user") or "unknown"),
            raw=event,
        )
