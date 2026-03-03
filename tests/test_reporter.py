from __future__ import annotations

import os
import tempfile
import unittest

from slackclaw.models import TaskExecutionResult, TaskSpec, TaskStatus
from slackclaw.reporter import Reporter


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict] | None]] = []
        self.upload_calls: list[dict] = []
        self.error: Exception | None = None
        self.upload_error: Exception | None = None

    def chat_post_message(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict] | None = None,
    ) -> dict:
        if self.error is not None:
            raise self.error
        self.calls.append((channel_id, text, blocks))
        return {"ok": True, "ts": "1.1"}

    def upload_file(
        self,
        *,
        channel_id: str,
        filepath: str,
        filename: str = "",
        title: str = "",
        thread_ts: str | None = None,
        initial_comment: str = "",
    ) -> dict:
        if self.upload_error is not None:
            raise self.upload_error
        self.upload_calls.append(
            {
                "channel_id": channel_id,
                "filepath": filepath,
                "filename": filename,
                "title": title,
                "thread_ts": thread_ts,
                "initial_comment": initial_comment,
            }
        )
        return {"ok": True}


class ReporterTests(unittest.TestCase):
    def _task(self) -> TaskSpec:
        return TaskSpec(
            task_id="task-1",
            channel_id="C_CMD",
            message_ts="1.23",
            thread_ts="1.23",
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
        channel_id, text, blocks = client.calls[0]
        self.assertEqual(channel_id, "C_REPORT")
        self.assertIn("SlackClaw task task-1", text)
        self.assertIn("summary: ok", text)
        self.assertIsNotNone(blocks)
        assert blocks is not None
        self.assertEqual(blocks[0]["type"], "section")
        self.assertIn("*SlackClaw task*", str(blocks[0]))

    def test_report_raises_when_post_fails(self) -> None:
        client = FakeClient()
        client.error = RuntimeError("missing_scope")
        reporter = Reporter(client=client, report_channel_id="C_REPORT")
        result = TaskExecutionResult(status=TaskStatus.FAILED, summary="failed", details="traceback")

        with self.assertRaises(RuntimeError):
            reporter.report(self._task(), result)

    def test_report_truncation_uses_configurable_limits(self) -> None:
        client = FakeClient()
        reporter = Reporter(
            client=client,
            report_channel_id="C_REPORT",
            input_max_chars=8,
            summary_max_chars=10,
            details_max_chars=12,
        )
        task = self._task()
        task = TaskSpec(
            task_id=task.task_id,
            channel_id=task.channel_id,
            message_ts=task.message_ts,
            thread_ts=task.thread_ts,
            trigger_user=task.trigger_user,
            trigger_text=task.trigger_text,
            command_text="abcdefghijklmno",
            lock_key=task.lock_key,
        )
        result = TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary="0123456789ABCDEF",
            details="zzzzzzzzzzzzzzzzzz",
        )

        reporter.report(task, result)

        _channel, text, blocks = client.calls[0]
        self.assertIn("input: abcde...", text)
        self.assertIn("summary: 0123456...", text)
        self.assertIn("details: zzzzzzzzz...", text)
        self.assertIsNotNone(blocks)

    def test_auto_file_uploads_when_details_exceed_threshold(self) -> None:
        client = FakeClient()
        reporter = Reporter(
            client=client,
            report_channel_id="C_REPORT",
            file_output_threshold=50,
        )
        long_details = "x" * 200
        result = TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary="ok",
            details=long_details,
        )

        reporter.report(self._task(), result)

        self.assertEqual(len(client.calls), 1)
        _channel, text, _blocks = client.calls[0]
        self.assertIn("full output uploaded as file", text)

        self.assertEqual(len(client.upload_calls), 1)
        upload = client.upload_calls[0]
        self.assertEqual(upload["channel_id"], "C_REPORT")
        self.assertIn("task-1", upload["filename"])
        # temp file should have been cleaned up
        self.assertFalse(os.path.exists(upload["filepath"]))

    def test_no_auto_file_when_details_under_threshold(self) -> None:
        client = FakeClient()
        reporter = Reporter(
            client=client,
            report_channel_id="C_REPORT",
            file_output_threshold=4000,
        )
        result = TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary="ok",
            details="short output",
        )

        reporter.report(self._task(), result)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.upload_calls), 0)

    def test_upload_file_path_triggers_file_upload(self) -> None:
        client = FakeClient()
        reporter = Reporter(client=client, report_channel_id="C_REPORT")
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            filepath = f.name
        try:
            result = TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="file ready",
                details=f"uploading {filepath}",
                upload_file_path=filepath,
            )
            reporter.report(self._task(), result)

            self.assertEqual(len(client.calls), 1)
            self.assertEqual(len(client.upload_calls), 1)
            upload = client.upload_calls[0]
            self.assertEqual(upload["filepath"], filepath)
            self.assertEqual(upload["channel_id"], "C_REPORT")
        finally:
            os.unlink(filepath)


    def test_upload_failure_posts_warning_instead_of_crashing(self) -> None:
        client = FakeClient()
        client.upload_error = RuntimeError("missing_scope: files:write")
        reporter = Reporter(client=client, report_channel_id="C_REPORT")
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            filepath = f.name
        try:
            result = TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="file ready",
                details=f"uploading {filepath}",
                upload_file_path=filepath,
            )
            # Should NOT raise — upload failure is handled gracefully
            reporter.report(self._task(), result)

            # Text report posted + warning message about upload failure
            self.assertEqual(len(client.calls), 2)
            warning_text = client.calls[1][1]
            self.assertIn("Failed to upload file", warning_text)
            self.assertIn("missing_scope", warning_text)
            self.assertEqual(len(client.upload_calls), 0)
        finally:
            os.unlink(filepath)


if __name__ == "__main__":
    unittest.main()
