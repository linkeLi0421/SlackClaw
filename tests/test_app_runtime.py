from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slackclaw.app import _drain_queue, run
from slackclaw.config import AppConfig, ConfigError
from slackclaw.executor import TaskExecutor
from slackclaw.models import TaskSpec, TaskStatus
from slackclaw.queue import TaskQueue
from slackclaw.state_store import StateStore


def _config(db_path: str) -> AppConfig:
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
        state_db_path=db_path,
        exec_timeout_seconds=120,
        dry_run=True,
        report_input_max_chars=500,
        report_summary_max_chars=1200,
        report_details_max_chars=4000,
        run_mode="run",
        approval_mode="none",
        approve_reaction="white_check_mark",
        reject_reaction="x",
        worker_processes=2,
    )


class _SubmitFailingPool:
    def submit(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("broken process pool")


class _FakeReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskStatus, str]] = []

    def report(self, task: TaskSpec, result) -> None:  # noqa: ANN001
        self.calls.append((task.task_id, result.status, result.summary))


class AppRuntimeTests(unittest.TestCase):
    def test_drain_queue_falls_back_to_inline_when_submit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
            queue = TaskQueue()
            task = TaskSpec(
                task_id="task-1",
                channel_id="C111",
                message_ts="1.1",
                thread_ts="1.1",
                trigger_user="U1",
                trigger_text="!do sh:echo hi",
                command_text="sh:echo hi",
                lock_key="thread:1.1",
            )
            store.upsert_task(task.task_id, TaskStatus.PENDING, payload={})
            queue.enqueue(task)

            reporter = _FakeReporter()
            executor = TaskExecutor(dry_run=True, timeout_seconds=30)
            handled = _drain_queue(
                queue,
                config=_config(db_path),
                store=store,
                executor=executor,
                reporter=reporter,  # type: ignore[arg-type]
                process_pool=_SubmitFailingPool(),  # type: ignore[arg-type]
            )

            self.assertEqual(handled, 1)
            row = store.get_task(task.task_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, TaskStatus.SUCCEEDED)
            self.assertEqual(len(reporter.calls), 1)
            store.close()

    def test_run_with_empty_argv_does_not_fall_back_to_sys_argv(self) -> None:
        with patch("slackclaw.app.parse_args") as parse_args:
            parse_args.return_value = argparse.Namespace(once=False)
            with patch("slackclaw.app.load_config", side_effect=ConfigError("boom")):
                exit_code = run([])
        parse_args.assert_called_once_with([])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
