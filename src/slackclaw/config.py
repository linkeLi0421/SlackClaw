from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_DESKTOP_REPORT_SCRIPT = "/Users/link/Desktop/slack-web-post/scripts/post_channel.py"
ALLOWED_TRIGGER_MODES = {"prefix", "mention"}
ALLOWED_REPORTER_MODES = {"desktop_skill"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    slack_bot_token: str
    command_channel_id: str
    report_channel_id: str
    poll_interval: float
    poll_batch_size: int
    trigger_mode: str
    trigger_prefix: str
    bot_user_id: str
    state_db_path: str
    reporter_mode: str
    desktop_report_script: str
    exec_timeout_seconds: int
    dry_run: bool


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

    reporter_mode = _validate_mode(
        "REPORTER_MODE",
        source.get("REPORTER_MODE", "desktop_skill"),
        ALLOWED_REPORTER_MODES,
    )
    desktop_report_script = (source.get("DESKTOP_REPORT_SCRIPT") or DEFAULT_DESKTOP_REPORT_SCRIPT).strip()
    if not desktop_report_script:
        raise ConfigError("DESKTOP_REPORT_SCRIPT cannot be empty")

    script_path = Path(desktop_report_script)
    if reporter_mode == "desktop_skill" and not script_path.exists():
        raise ConfigError(f"Desktop reporter script not found: {desktop_report_script}")

    exec_timeout_seconds = _parse_positive_int(
        "EXEC_TIMEOUT_SECONDS",
        source.get("EXEC_TIMEOUT_SECONDS", ""),
        120,
    )
    dry_run = _parse_bool("DRY_RUN", source.get("DRY_RUN", ""), True)

    return AppConfig(
        slack_bot_token=slack_bot_token,
        command_channel_id=command_channel_id,
        report_channel_id=report_channel_id,
        poll_interval=poll_interval,
        poll_batch_size=poll_batch_size,
        trigger_mode=trigger_mode,
        trigger_prefix=trigger_prefix,
        bot_user_id=bot_user_id,
        state_db_path=state_db_path,
        reporter_mode=reporter_mode,
        desktop_report_script=desktop_report_script,
        exec_timeout_seconds=exec_timeout_seconds,
        dry_run=dry_run,
    )
