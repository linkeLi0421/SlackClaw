from __future__ import annotations

import unittest

from slackclaw.models import TaskExecutionResult, TaskSpec, TaskStatus
from slackclaw.reporter import Reporter


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def chat_post_message(self, *, channel_id: str, text: str, thread_ts: str | None = None) -> dict:
        if self.error is not None:
            raise self.error
        self.calls.append((channel_id, text))
        return {"ok": True, "ts": "1.1"}


class ReporterTests(unittest.TestCase):
    def _task(self) -> TaskSpec:
        return TaskSpec(
            task_id="task-1",
            channel_id="C_CMD",
            message_ts="1.23",
            trigger_user="U1",
            trigger_text="!do test",
            command_text="test",
            lock_key="global",
        )

    def test_report_posts_success_message(self) -> None:
        client = FakeClient()
        reporter = Reporter(client=client, report_channel_id="C_REPORT")
        result = TaskExecutionResult(status=TaskStatus.SUCCEEDED, summary="ok", details="done")

        reporter.report(self._task(), result)

        self.assertEqual(len(client.calls), 1)
        channel_id, text = client.calls[0]
        self.assertEqual(channel_id, "C_REPORT")
        self.assertIn("SlackClaw task task-1", text)
        self.assertIn("summary: ok", text)

    def test_report_raises_when_post_fails(self) -> None:
        client = FakeClient()
        client.error = RuntimeError("missing_scope")
        reporter = Reporter(client=client, report_channel_id="C_REPORT")
        result = TaskExecutionResult(status=TaskStatus.FAILED, summary="failed", details="traceback")

        with self.assertRaises(RuntimeError):
            reporter.report(self._task(), result)


if __name__ == "__main__":
    unittest.main()
