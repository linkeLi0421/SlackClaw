from __future__ import annotations

import unittest

from slackclaw.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_env = {
            "SLACK_BOT_TOKEN": "xoxb-test-token",
            "SLACK_APP_TOKEN": "xapp-test-token",
            "COMMAND_CHANNEL_ID": "C1234567890",
            "REPORT_CHANNEL_ID": "C0987654321",
        }

    def test_load_config_uses_defaults(self) -> None:
        cfg = load_config(self.base_env)
        self.assertEqual(cfg.listener_mode, "socket")
        self.assertEqual(cfg.socket_read_timeout_seconds, 1.0)
        self.assertEqual(cfg.poll_interval, 3.0)
        self.assertEqual(cfg.poll_batch_size, 100)
        self.assertEqual(cfg.trigger_mode, "prefix")
        self.assertEqual(cfg.trigger_prefix, "!do")
        self.assertEqual(cfg.bot_user_id, "")
        self.assertEqual(cfg.state_db_path, "./state.db")
        self.assertEqual(cfg.exec_timeout_seconds, 120)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.report_input_max_chars, 500)
        self.assertEqual(cfg.report_summary_max_chars, 1200)
        self.assertEqual(cfg.report_details_max_chars, 4000)
        self.assertEqual(cfg.run_mode, "approve")
        self.assertEqual(cfg.approval_mode, "reaction")
        self.assertEqual(cfg.approve_reaction, "white_check_mark")
        self.assertEqual(cfg.reject_reaction, "x")
        self.assertTrue(cfg.agent_response_instruction.startswith("Format the final answer"))
        self.assertEqual(cfg.worker_processes, 1)
        self.assertIn("echo", cfg.shell_allowlist)
        self.assertNotIn("rm", cfg.shell_allowlist)

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

    def test_mention_mode_requires_bot_user_id(self) -> None:
        env = dict(self.base_env)
        env["TRIGGER_MODE"] = "mention"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_socket_mode_requires_app_token(self) -> None:
        env = dict(self.base_env)
        del env["SLACK_APP_TOKEN"]
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_poll_mode_does_not_require_app_token(self) -> None:
        env = dict(self.base_env)
        del env["SLACK_APP_TOKEN"]
        env["LISTENER_MODE"] = "poll"
        env["APPROVAL_MODE"] = "none"
        cfg = load_config(env)
        self.assertEqual(cfg.listener_mode, "poll")
        self.assertEqual(cfg.slack_app_token, "")

    def test_reaction_approval_requires_socket_mode(self) -> None:
        env = dict(self.base_env)
        env["LISTENER_MODE"] = "poll"
        env["APPROVAL_MODE"] = "reaction"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_run_mode_disables_approval(self) -> None:
        env = dict(self.base_env)
        env["RUN_MODE"] = "run"
        env["APPROVAL_MODE"] = "reaction"
        cfg = load_config(env)
        self.assertEqual(cfg.run_mode, "run")
        self.assertEqual(cfg.approval_mode, "none")

    def test_invalid_report_limit_raises(self) -> None:
        env = dict(self.base_env)
        env["REPORT_DETAILS_MAX_CHARS"] = "0"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_agent_response_instruction_allows_empty_override(self) -> None:
        env = dict(self.base_env)
        env["AGENT_RESPONSE_INSTRUCTION"] = ""
        cfg = load_config(env)
        self.assertEqual(cfg.agent_response_instruction, "")

    def test_invalid_worker_processes_raises(self) -> None:
        env = dict(self.base_env)
        env["WORKER_PROCESSES"] = "0"
        with self.assertRaises(ConfigError):
            load_config(env)

    def test_shell_allowlist_parses_comma_or_space(self) -> None:
        env = dict(self.base_env)
        env["SHELL_ALLOWLIST"] = "echo, ls  ,pytest"
        cfg = load_config(env)
        self.assertEqual(cfg.shell_allowlist, ("echo", "ls", "pytest"))


if __name__ == "__main__":
    unittest.main()
