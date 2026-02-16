from __future__ import annotations

import unittest

from slackclaw.config import AppConfig
from slackclaw.decider import decide_message
from slackclaw.models import SlackMessage


def _config(trigger_mode: str = "prefix", bot_user_id: str = "") -> AppConfig:
    return AppConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="",
        command_channel_id="C111",
        report_channel_id="C222",
        listener_mode="poll",
        socket_read_timeout_seconds=1.0,
        poll_interval=3.0,
        poll_batch_size=100,
        trigger_mode=trigger_mode,
        trigger_prefix="!do",
        bot_user_id=bot_user_id,
        state_db_path="./state.db",
        exec_timeout_seconds=120,
        dry_run=True,
        report_input_max_chars=500,
        report_summary_max_chars=1200,
        report_details_max_chars=4000,
        run_mode="approve",
        approval_mode="none",
        approve_reaction="white_check_mark",
        reject_reaction="x",
    )


class DeciderTests(unittest.TestCase):
    def test_prefix_trigger_creates_task(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="!do run tests", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        self.assertIsNotNone(decision.task)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "run tests")
        self.assertEqual(decision.task.lock_key, "global")

    def test_prefix_trigger_ignored_without_prefix(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="run tests", raw={})
        decision = decide_message(cfg, msg)
        self.assertFalse(decision.should_run)
        self.assertIsNone(decision.task)

    def test_mention_trigger_creates_task(self) -> None:
        cfg = _config(trigger_mode="mention", bot_user_id="U_BOT")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="<@U_BOT> ship it", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        self.assertIsNotNone(decision.task)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "ship it")

    def test_subtype_message_ignored(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(
            channel_id="C111",
            ts="1.1",
            user="U1",
            text="!do run tests",
            raw={"subtype": "channel_join"},
        )
        decision = decide_message(cfg, msg)
        self.assertFalse(decision.should_run)
        self.assertIsNone(decision.task)

    def test_lock_prefix_is_extracted(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(
            channel_id="C111",
            ts="1.1",
            user="U1",
            text="!do lock:repo-a sh:echo hi",
            raw={},
        )
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.lock_key, "lock:repo-a")
        self.assertEqual(decision.task.command_text, "sh:echo hi")

    def test_simple_shell_command_without_prefix(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="SHELL echo hi", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "sh:echo hi")

    def test_simple_kimi_command_without_prefix(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="KIMI improve repo", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "kimi:improve repo")

    def test_simple_codex_command_without_prefix(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="CODEX fix tests", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "codex:fix tests")

    def test_simple_claude_command_without_prefix(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="CLAUDE review this", raw={})
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.command_text, "claude:review this")

    def test_task_uses_thread_root_ts_when_present(self) -> None:
        cfg = _config(trigger_mode="prefix")
        msg = SlackMessage(
            channel_id="C111",
            ts="2.2",
            user="U1",
            text="!do run tests",
            raw={"thread_ts": "1.1"},
        )
        decision = decide_message(cfg, msg)
        self.assertTrue(decision.should_run)
        assert decision.task is not None
        self.assertEqual(decision.task.message_ts, "2.2")
        self.assertEqual(decision.task.thread_ts, "1.1")


if __name__ == "__main__":
    unittest.main()
