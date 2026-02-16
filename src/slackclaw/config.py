from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


ALLOWED_TRIGGER_MODES = {"prefix", "mention"}
ALLOWED_LISTENER_MODES = {"poll", "socket"}
ALLOWED_APPROVAL_MODES = {"none", "reaction"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    slack_bot_token: str
    slack_app_token: str
    command_channel_id: str
    report_channel_id: str
    listener_mode: str
    socket_read_timeout_seconds: float
    poll_interval: float
    poll_batch_size: int
    trigger_mode: str
    trigger_prefix: str
    bot_user_id: str
    state_db_path: str
    exec_timeout_seconds: int
    dry_run: bool
    approval_mode: str
    approve_reaction: str
    reject_reaction: str


def _required(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {key}")
    return value


def _parse_positive_float(name: str, raw_value: str, default: float) -> float:
    value = (raw_value or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw_value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be > 0, got {parsed}")
    return parsed


def _parse_positive_int(name: str, raw_value: str, default: int) -> int:
    value = (raw_value or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw_value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be > 0, got {parsed}")
    return parsed


def _parse_bool(name: str, raw_value: str, default: bool) -> bool:
    value = (raw_value or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value, got {raw_value!r}")


def _validate_mode(name: str, value: str, allowed: set[str]) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ConfigError(f"{name} cannot be empty")
    if normalized not in allowed:
        raise ConfigError(f"{name} must be one of {sorted(allowed)}, got {normalized!r}")
    return normalized


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    source = env if env is not None else os.environ
    slack_bot_token = (source.get("SLACK_BOT_TOKEN") or source.get("SLACK_MCP_XOXB_TOKEN") or "").strip()
    if not slack_bot_token:
        raise ConfigError("Missing required environment variable: SLACK_BOT_TOKEN (or SLACK_MCP_XOXB_TOKEN)")

    listener_mode = _validate_mode(
        "LISTENER_MODE",
        source.get("LISTENER_MODE", "socket"),
        ALLOWED_LISTENER_MODES,
    )
    slack_app_token = (source.get("SLACK_APP_TOKEN") or source.get("SLACK_MCP_XAPP_TOKEN") or "").strip()
    if listener_mode == "socket" and not slack_app_token:
        raise ConfigError("SLACK_APP_TOKEN (or SLACK_MCP_XAPP_TOKEN) is required when LISTENER_MODE=socket")
    socket_read_timeout_seconds = _parse_positive_float(
        "SOCKET_READ_TIMEOUT_SECONDS",
        source.get("SOCKET_READ_TIMEOUT_SECONDS", ""),
        1.0,
    )

    command_channel_id = _required(source, "COMMAND_CHANNEL_ID")
    report_channel_id = _required(source, "REPORT_CHANNEL_ID")
    poll_interval = _parse_positive_float("POLL_INTERVAL", source.get("POLL_INTERVAL", ""), 3.0)
    poll_batch_size = _parse_positive_int("POLL_BATCH_SIZE", source.get("POLL_BATCH_SIZE", ""), 100)
    if poll_batch_size > 200:
        raise ConfigError("POLL_BATCH_SIZE must be <= 200 (Slack API max)")
    trigger_mode = _validate_mode("TRIGGER_MODE", source.get("TRIGGER_MODE", "prefix"), ALLOWED_TRIGGER_MODES)
    trigger_prefix = (source.get("TRIGGER_PREFIX") or "!do").strip()
    if not trigger_prefix:
        raise ConfigError("TRIGGER_PREFIX cannot be empty")
    bot_user_id = (source.get("BOT_USER_ID") or "").strip()
    if trigger_mode == "mention" and not bot_user_id:
        raise ConfigError("BOT_USER_ID is required when TRIGGER_MODE=mention")

    state_db_path = (source.get("STATE_DB_PATH") or "./state.db").strip()
    if not state_db_path:
        raise ConfigError("STATE_DB_PATH cannot be empty")

    exec_timeout_seconds = _parse_positive_int(
        "EXEC_TIMEOUT_SECONDS",
        source.get("EXEC_TIMEOUT_SECONDS", ""),
        120,
    )
    dry_run = _parse_bool("DRY_RUN", source.get("DRY_RUN", ""), True)
    approval_mode = _validate_mode(
        "APPROVAL_MODE",
        source.get("APPROVAL_MODE", "reaction"),
        ALLOWED_APPROVAL_MODES,
    )
    if approval_mode == "reaction" and listener_mode != "socket":
        raise ConfigError("APPROVAL_MODE=reaction requires LISTENER_MODE=socket")

    approve_reaction = (source.get("APPROVE_REACTION") or "white_check_mark").strip().strip(":")
    reject_reaction = (source.get("REJECT_REACTION") or "x").strip().strip(":")
    if not approve_reaction:
        raise ConfigError("APPROVE_REACTION cannot be empty")
    if not reject_reaction:
        raise ConfigError("REJECT_REACTION cannot be empty")
    if approve_reaction == reject_reaction:
        raise ConfigError("APPROVE_REACTION and REJECT_REACTION must be different")

    return AppConfig(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        command_channel_id=command_channel_id,
        report_channel_id=report_channel_id,
        listener_mode=listener_mode,
        socket_read_timeout_seconds=socket_read_timeout_seconds,
        poll_interval=poll_interval,
        poll_batch_size=poll_batch_size,
        trigger_mode=trigger_mode,
        trigger_prefix=trigger_prefix,
        bot_user_id=bot_user_id,
        state_db_path=state_db_path,
        exec_timeout_seconds=exec_timeout_seconds,
        dry_run=dry_run,
        approval_mode=approval_mode,
        approve_reaction=approve_reaction,
        reject_reaction=reject_reaction,
    )
