from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slackclaw.app import _process_command_message, _process_reaction_event
from slackclaw.config import AppConfig
from slackclaw.decider import decide_message
from slackclaw.models import ApprovalStatus, SlackMessage, SlackReaction, TaskStatus
from slackclaw.queue import TaskQueue
from slackclaw.state_store import StateStore


def _config(approval_mode: str) -> AppConfig:
    return AppConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        command_channel_id="C111",
        report_channel_id="C222",
        listener_mode="socket",
        socket_read_timeout_seconds=1.0,
        poll_interval=3.0,
        poll_batch_size=100,
        trigger_mode="prefix",
        trigger_prefix="!do",
        bot_user_id="",
        state_db_path="./state.db",
        exec_timeout_seconds=120,
        dry_run=True,
        approval_mode=approval_mode,
        approve_reaction="white_check_mark",
        reject_reaction="x",
    )


class FakeClient:
    def __init__(self, ts: str = "1.2") -> None:
        self.ts = ts
        self.calls: list[tuple[str, str, str | None]] = []

    def chat_post_message(self, *, channel_id: str, text: str, thread_ts: str | None = None) -> dict:
        self.calls.append((channel_id, text, thread_ts))
        return {"ok": True, "ts": self.ts}


class FakeReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskStatus, str]] = []

    def report(self, task, result) -> None:
        self.calls.append((task.task_id, result.status, result.summary))


class ApprovalFlowTests(unittest.TestCase):
    def test_reaction_mode_waits_for_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(approval_mode="reaction")
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            queue = TaskQueue()
            client = FakeClient(ts="1.2")
            reporter = FakeReporter()
            message = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="!do ship", raw={})
            decision = decide_message(cfg, message)
            assert decision.task is not None
            task = decision.task

            enqueued = _process_command_message(
                cfg,
                message,
                store=store,
                queue=queue,
                client=client,  # type: ignore[arg-type]
                reporter=reporter,  # type: ignore[arg-type]
            )

            self.assertEqual(enqueued, 0)
            self.assertEqual(len(queue), 0)
            self.assertEqual(len(client.calls), 1)
            row = store.get_task(task.task_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, TaskStatus.WAITING_APPROVAL)

            approval = store.get_task_approval(task.task_id)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertEqual(approval.status, ApprovalStatus.PENDING)
            self.assertEqual(approval.approval_message_ts, "1.2")
            store.close()

    def test_approve_reaction_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(approval_mode="reaction")
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            queue = TaskQueue()
            reporter = FakeReporter()
            client = FakeClient(ts="1.2")
            message = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="!do ship", raw={})
            _process_command_message(
                cfg,
                message,
                store=store,
                queue=queue,
                client=client,  # type: ignore[arg-type]
                reporter=reporter,  # type: ignore[arg-type]
            )

            approved = _process_reaction_event(
                SlackReaction(
                    channel_id="C111",
                    message_ts="1.2",
                    reaction="white_check_mark",
                    user="U2",
                    raw={},
                ),
                store=store,
                queue=queue,
                reporter=reporter,  # type: ignore[arg-type]
            )

            self.assertEqual(approved, 1)
            self.assertEqual(len(queue), 1)
            decision = decide_message(cfg, message)
            assert decision.task is not None
            task_id = decision.task.task_id
            row = store.get_task(task_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, TaskStatus.PENDING)
            approval = store.get_task_approval(task_id)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertEqual(approval.status, ApprovalStatus.APPROVED)
            self.assertEqual(len(reporter.calls), 0)
            store.close()

    def test_reject_reaction_cancels_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(approval_mode="reaction")
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            queue = TaskQueue()
            reporter = FakeReporter()
            client = FakeClient(ts="1.2")
            message = SlackMessage(channel_id="C111", ts="1.1", user="U1", text="!do ship", raw={})
            _process_command_message(
                cfg,
                message,
                store=store,
                queue=queue,
                client=client,  # type: ignore[arg-type]
                reporter=reporter,  # type: ignore[arg-type]
            )

            approved = _process_reaction_event(
                SlackReaction(
                    channel_id="C111",
                    message_ts="1.2",
                    reaction="x",
                    user="U2",
                    raw={},
                ),
                store=store,
                queue=queue,
                reporter=reporter,  # type: ignore[arg-type]
            )

            self.assertEqual(approved, 0)
            self.assertEqual(len(queue), 0)
            decision = decide_message(cfg, message)
            assert decision.task is not None
            task_id = decision.task.task_id
            row = store.get_task(task_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, TaskStatus.CANCELED)
            approval = store.get_task_approval(task_id)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertEqual(approval.status, ApprovalStatus.REJECTED)
            self.assertEqual(len(reporter.calls), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
