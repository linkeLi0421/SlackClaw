from __future__ import annotations

import unittest
from unittest.mock import patch

from slackclaw.models import TaskExecutionResult, TaskSpec, TaskStatus
from slackclaw.reporter import Reporter


class ReporterTests(unittest.TestCase):
    def test_report_posts_success_message(self) -> None:
        reporter = Reporter(
            report_channel_id="C_REPORT",
            desktop_report_script="/Users/link/Desktop/slack-web-post/scripts/post_channel.py",
        )
        task = TaskSpec(
            task_id="task-1",
            channel_id="C_CMD",
            message_ts="1.23",
            trigger_user="U1",
            trigger_text="!do test",
            command_text="test",
            lock_key="global",
        )
        result = TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary="ok",
            details="done",
        )

        with patch("slackclaw.reporter.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "123.456"
            mock_run.return_value.stderr = ""
            reporter.report(task, result)

            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "python3")
            self.assertEqual(args[1], "/Users/link/Desktop/slack-web-post/scripts/post_channel.py")
            self.assertIn("C_REPORT", args)

    def test_report_raises_when_post_fails(self) -> None:
        reporter = Reporter(
            report_channel_id="C_REPORT",
            desktop_report_script="/Users/link/Desktop/slack-web-post/scripts/post_channel.py",
        )
        task = TaskSpec(
            task_id="task-1",
            channel_id="C_CMD",
            message_ts="1.23",
            trigger_user="U1",
            trigger_text="!do test",
            command_text="test",
            lock_key="global",
        )
        result = TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary="failed",
            details="traceback",
        )
        with patch("slackclaw.reporter.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "missing_scope"
            with self.assertRaises(RuntimeError):
                reporter.report(task, result)


if __name__ == "__main__":
    unittest.main()
