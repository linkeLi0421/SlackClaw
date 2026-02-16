from __future__ import annotations

import unittest

from slackclaw.config import AppConfig
from slackclaw.decider import decide_message
from slackclaw.models import SlackMessage


def _config(trigger_mode: str = "prefix", bot_user_id: str = "") -> AppConfig:
    return AppConfig(
        slack_bot_token="xoxb-test",
        command_channel_id="C111",
        report_channel_id="C222",
        poll_interval=3.0,
        poll_batch_size=100,
        trigger_mode=trigger_mode,
        trigger_prefix="!do",
        bot_user_id=bot_user_id,
        state_db_path="./state.db",
        reporter_mode="desktop_skill",
        desktop_report_script="/Users/link/Desktop/slack-web-post/scripts/post_channel.py",
        exec_timeout_seconds=120,
        dry_run=True,
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


if __name__ == "__main__":
    unittest.main()
