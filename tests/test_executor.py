from __future__ import annotations

import unittest
from unittest.mock import patch

from slackclaw.executor import TaskExecutor
from slackclaw.models import TaskSpec, TaskStatus


def _task(command_text: str) -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        channel_id="C111",
        message_ts="1.1",
        trigger_user="U1",
        trigger_text="!do x",
        command_text=command_text,
        lock_key="global",
    )


class ExecutorTests(unittest.TestCase):
    def test_dry_run_does_not_execute_shell(self) -> None:
        executor = TaskExecutor(dry_run=True, timeout_seconds=30)
        result = executor.execute(_task("sh:echo hello"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertIn("dry-run", result.summary)

    def test_shell_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("sh:printf ok"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "shell command completed")
        self.assertIn("ok", result.details)

    def test_shell_prefix_with_empty_payload_fails(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("sh:   "))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("invalid shell command", result.summary)

    def test_shell_timeout_is_reported(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=1)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            from subprocess import TimeoutExpired

            mock_run.side_effect = TimeoutExpired(cmd="sleep 10", timeout=1)
            result = executor.execute(_task("sh:sleep 10"))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("timed out", result.summary)


if __name__ == "__main__":
    unittest.main()
