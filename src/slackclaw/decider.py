from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .config import AppConfig
from .models import SlackMessage, TaskSpec

_SHELL_CD_RE = re.compile(r"^\s*sh:\s*cd\s+([^\s;&]+)")
_LOCK_PREFIX_RE = re.compile(r"^lock:([^\s]+)\s+(.*)$")
_SIMPLE_SHELL_RE = re.compile(r"^shell\s+(.+)$", re.IGNORECASE)
_SIMPLE_KIMI_RE = re.compile(r"^kimi\s+(.+)$", re.IGNORECASE)
_SIMPLE_CODEX_RE = re.compile(r"^codex\s+(.+)$", re.IGNORECASE)
_SIMPLE_CLAUDE_RE = re.compile(r"^claude\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Decision:
    should_run: bool
    reason: str
    task: TaskSpec | None


def _build_task_id(channel_id: str, message_ts: str, text: str) -> str:
    raw = f"{channel_id}:{message_ts}:{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _starts_with_mention(text: str, bot_user_id: str) -> tuple[bool, str]:
    mention = f"<@{bot_user_id}>"
    stripped = text.strip()
    if not stripped.startswith(mention):
        return False, ""
    remainder = stripped[len(mention) :].strip()
    return True, remainder


def _extract_lock_key(command_text: str) -> tuple[str, str]:
    prefixed = _LOCK_PREFIX_RE.match(command_text)
    if prefixed:
        lock_name = prefixed.group(1).strip()
        remainder = prefixed.group(2).strip()
        if lock_name:
            return f"lock:{lock_name}", remainder

    shell_cd = _SHELL_CD_RE.match(command_text)
    if shell_cd:
        path = shell_cd.group(1).strip()
        if path:
            return f"path:{path}", command_text

    return "global", command_text


def _parse_simple_command(text: str) -> str | None:
    shell_match = _SIMPLE_SHELL_RE.match(text)
    if shell_match:
        command = shell_match.group(1).strip()
        if command:
            return f"sh:{command}"

    kimi_match = _SIMPLE_KIMI_RE.match(text)
    if kimi_match:
        prompt = kimi_match.group(1).strip()
        if prompt:
            return f"kimi:{prompt}"

    codex_match = _SIMPLE_CODEX_RE.match(text)
    if codex_match:
        prompt = codex_match.group(1).strip()
        if prompt:
            return f"codex:{prompt}"

    claude_match = _SIMPLE_CLAUDE_RE.match(text)
    if claude_match:
        prompt = claude_match.group(1).strip()
        if prompt:
            return f"claude:{prompt}"

    return None


def decide_message(config: AppConfig, message: SlackMessage) -> Decision:
    subtype = str(message.raw.get("subtype") or "")
    if subtype:
        return Decision(should_run=False, reason=f"ignored subtype={subtype}", task=None)

    text = message.text.strip()
    if not text:
        return Decision(should_run=False, reason="ignored empty text", task=None)

    command_text = _parse_simple_command(text)
    if not command_text:
        if config.trigger_mode == "prefix":
            if not text.startswith(config.trigger_prefix):
                return Decision(should_run=False, reason="no prefix trigger", task=None)
            command_text = text[len(config.trigger_prefix) :].strip()
        elif config.trigger_mode == "mention":
            matched, remainder = _starts_with_mention(text, config.bot_user_id)
            if not matched:
                return Decision(should_run=False, reason="no mention trigger", task=None)
            command_text = remainder
        else:
            return Decision(should_run=False, reason="unsupported trigger mode", task=None)

    if not command_text:
        return Decision(should_run=False, reason="empty command after trigger", task=None)

    lock_key, command_text = _extract_lock_key(command_text)
    if not command_text:
        return Decision(should_run=False, reason="empty command after lock prefix", task=None)

    task_id = _build_task_id(message.channel_id, message.ts, message.text)
    task = TaskSpec(
        task_id=task_id,
        channel_id=message.channel_id,
        message_ts=message.ts,
        trigger_user=message.user,
        trigger_text=message.text,
        command_text=command_text,
        lock_key=lock_key,
    )
    return Decision(should_run=True, reason="trigger matched", task=task)
