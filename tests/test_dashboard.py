from __future__ import annotations

import json
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path

from slackclaw.config import AppConfig
from slackclaw.dashboard import DashboardContext, start_dashboard
from slackclaw.models import TaskStatus
from slackclaw.queue import TaskQueue
from slackclaw.state_store import StateStore


def _test_config(db_path: str) -> AppConfig:
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
        worker_processes=1,
        dashboard_port=0,
    )


class DashboardTests(unittest.TestCase):
    def test_dashboard_serves_html_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
            store.upsert_task(
                "task-1",
                TaskStatus.PENDING,
                payload={"command_text": "sh:ls", "trigger_user": "U1"},
            )
            store.close()

            queue = TaskQueue()
            config = _test_config(db_path)
            ctx = DashboardContext(
                config=config,
                state_db_path=db_path,
                queue_snapshot=queue.snapshot,
                in_flight_snapshot=lambda: [],
                queue_len=queue.__len__,
            )

            thread, server = start_dashboard(ctx)
            try:
                port = server.server_port
                conn = HTTPConnection("127.0.0.1", port, timeout=5)

                # HTML page
                conn.request("GET", "/")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                body = resp.read().decode()
                self.assertIn("SlackClaw Dashboard", body)

                # /api/tasks
                conn.request("GET", "/api/tasks")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                tasks = json.loads(resp.read())
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["task_id"], "task-1")

                # /api/stats
                conn.request("GET", "/api/stats")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                stats = json.loads(resp.read())
                self.assertEqual(stats["total_tasks"], 1)
                self.assertIn("tasks_by_status", stats)

                # /api/config - tokens must be redacted
                conn.request("GET", "/api/config")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                cfg = json.loads(resp.read())
                self.assertNotIn("slack_bot_token", cfg)
                self.assertNotIn("slack_app_token", cfg)
                self.assertEqual(cfg["dry_run"], True)

                # /api/sessions
                conn.request("GET", "/api/sessions")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertIsInstance(json.loads(resp.read()), list)

                # /api/approvals
                conn.request("GET", "/api/approvals")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertIsInstance(json.loads(resp.read()), list)

                # /api/locks
                conn.request("GET", "/api/locks")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertIsInstance(json.loads(resp.read()), list)

                # 404
                conn.request("GET", "/nonexistent")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 404)

                conn.close()
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
