from __future__ import annotations

import unittest
from pathlib import Path

from slackclaw.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_env = {
            "SLACK_BOT_TOKEN": "xoxb-test-token",
            "COMMAND_CHANNEL_ID": "C1234567890",
            "REPORT_CHANNEL_ID": "C0987654321",
            "DESKTOP_REPORT_SCRIPT": "/Users/link/Desktop/slack-web-post/scripts/post_channel.py",
        }

    def test_load_config_uses_defaults(self) -> None:
        cfg = load_config(self.base_env)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.poll_batch_size, 100)
        self.assertEqual(cfg.trigger_mode, "prefix")
        self.assertEqual(cfg.trigger_prefix, "!do")
        self.assertEqual(cfg.bot_user_id, "")
        self.assertEqual(cfg.state_db_path, "./state.db")
        self.assertEqual(cfg.reporter_mode, "desktop_skill")
        self.assertEqual(cfg.exec_timeout_seconds, 120)
        self.assertTrue(cfg.dry_run)

    def test_missing_required_env_raises(self) -> None:
        env = dict(self.base_env)
        del env["COMMAND_CHANNEL_ID"]
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_invalid_poll_interval_raises(self) -> None:
        env = dict(self.base_env)
        env["POLL_INTERVAL"] = "0"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_invalid_poll_batch_size_raises(self) -> None:
        env = dict(self.base_env)
        env["POLL_BATCH_SIZE"] = "999"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_invalid_bool_raises(self) -> None:
        env = dict(self.base_env)
        env["DRY_RUN"] = "sometimes"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_missing_desktop_script_raises(self) -> None:
        env = dict(self.base_env)
        env["DESKTOP_REPORT_SCRIPT"] = str(Path.cwd() / "does-not-exist.py")
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_mention_mode_requires_bot_user_id(self) -> None:
        env = dict(self.base_env)
        env["TRIGGER_MODE"] = "mention"
        with self.assertRaises(ConfigError):
            load_config(env)


if __name__ == "__main__":
    unittest.main()
