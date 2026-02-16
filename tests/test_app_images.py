from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slackclaw.app import _process_command_message
from slackclaw.config import AppConfig
from slackclaw.decider import decide_message
from slackclaw.models import SlackMessage, TaskStatus
from slackclaw.queue import TaskQueue
from slackclaw.state_store import StateStore


def _config() -> AppConfig:
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
        report_input_max_chars=500,
        report_summary_max_chars=1200,
        report_details_max_chars=4000,
        run_mode="run",
        approval_mode="none",
        approve_reaction="white_check_mark",
        reject_reaction="x",
    )


class FakeClient:
    def __init__(self) -> None:
        self.download_calls: list[str] = []
        self.chat_calls: list[tuple[str, str, str | None]] = []
        self.download_error: Exception | None = None

    def chat_post_message(self, *, channel_id: str, text: str, thread_ts: str | None = None) -> dict:
        self.chat_calls.append((channel_id, text, thread_ts))
        return {"ok": True, "ts": "1.2"}

    def download_private_file(self, url: str) -> bytes:
        self.download_calls.append(url)
        if self.download_error is not None:
            raise self.download_error
        return b"fake-image-bytes"


class FakeReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskStatus, str]] = []

    def report(self, task, result) -> None:
        self.calls.append((task.task_id, result.status, result.summary))


class AppImageTests(unittest.TestCase):
    def test_process_command_downloads_and_attaches_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config()
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            queue = TaskQueue()
            client = FakeClient()
            reporter = FakeReporter()
            message = SlackMessage(
                channel_id="C111",
                ts="1.1",
                user="U1",
                text="KIMI describe this image",
                raw={
                    "subtype": "file_share",
                    "files": [
                        {
                            "id": "F111",
                            "name": "screenshot.png",
                            "mimetype": "image/png",
                            "size": 1024,
                            "url_private_download": "https://files.slack.test/F111",
                        }
                    ],
                },
            )

            with patch("slackclaw.app.ATTACHMENTS_BASE_DIR", str(Path(tmpdir) / "attachments")):
                enqueued = _process_command_message(
                    cfg,
                    message,
                    store=store,
                    queue=queue,
                    client=client,  # type: ignore[arg-type]
                    reporter=reporter,  # type: ignore[arg-type]
                )

            self.assertEqual(enqueued, 1)
            self.assertEqual(len(queue), 1)
            task = queue.dequeue()
            assert task is not None
            self.assertEqual(task.command_text, "kimi:describe this image")
            self.assertEqual(len(task.image_paths), 1)
            self.assertTrue(Path(task.image_paths[0]).exists())
            self.assertEqual(len(client.download_calls), 1)
            self.assertEqual(len(reporter.calls), 0)
            store.close()

    def test_process_command_reports_failure_when_image_download_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config()
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            queue = TaskQueue()
            client = FakeClient()
            client.download_error = RuntimeError("forbidden")
            reporter = FakeReporter()
            message = SlackMessage(
                channel_id="C111",
                ts="1.1",
                user="U1",
                text="KIMI describe this image",
                raw={
                    "subtype": "file_share",
                    "files": [
                        {
                            "id": "F111",
                            "name": "screenshot.png",
                            "mimetype": "image/png",
                            "size": 1024,
                            "url_private_download": "https://files.slack.test/F111",
                        }
                    ],
                },
            )
            decision = decide_message(cfg, message)
            assert decision.task is not None

            with patch("slackclaw.app.ATTACHMENTS_BASE_DIR", str(Path(tmpdir) / "attachments")):
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
            task_row = store.get_task(decision.task.task_id)
            self.assertIsNotNone(task_row)
            assert task_row is not None
            self.assertEqual(task_row.status, TaskStatus.FAILED)
            self.assertEqual(len(reporter.calls), 1)
            self.assertIn("failed to prepare image attachment", reporter.calls[0][2])
            store.close()


if __name__ == "__main__":
    unittest.main()
