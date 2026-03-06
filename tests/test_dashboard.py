from __future__ import annotations

import json
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path

from slackclaw.config import AppConfig
from slackclaw.dashboard import DashboardContext, start_dashboard
from slackclaw.models import MemoryCategory, MemoryRecord, MemoryScope, TaskStatus
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
    @staticmethod
    def _stop_dashboard(thread, server) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

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
                self._stop_dashboard(thread, server)

    def test_api_tasks_includes_result_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
            store.upsert_task(
                "task-r1",
                TaskStatus.SUCCEEDED,
                payload={
                    "command_text": "sh:echo hi",
                    "trigger_user": "U1",
                    "trigger_text": "!do echo hi",
                },
            )
            store.store_task_result("task-r1", "echoed hi", "hi\n")
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

                conn.request("GET", "/api/tasks")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                tasks = json.loads(resp.read())
                self.assertEqual(len(tasks), 1)
                t = tasks[0]
                self.assertEqual(t["result_summary"], "echoed hi")
                self.assertEqual(t["result_details"], "hi\n")
                self.assertEqual(t["trigger_text"], "!do echo hi")

                conn.close()
            finally:
                self._stop_dashboard(thread, server)

    def test_dashboard_html_contains_modal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
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

                conn.request("GET", "/")
                resp = conn.getresponse()
                body = resp.read().decode()
                self.assertIn("task-modal", body)
                self.assertIn("modal-overlay", body)
                self.assertIn("openTaskModal", body)
                self.assertIn("localTime", body)
                self.assertIn("fadeSlideIn", body)

                conn.close()
            finally:
                self._stop_dashboard(thread, server)


    def test_api_memories_returns_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-1", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="test fact", file_path="/tmp/m.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
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

                conn.request("GET", "/api/memories")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
                self.assertEqual(data["total_memories"], 1)
                self.assertIn("U1", data["memories_by_scope"])

                conn.close()
            finally:
                self._stop_dashboard(thread, server)

    def test_api_stats_includes_memory_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")
            store = StateStore(db_path)
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-1", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="test", file_path="/tmp/m.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
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

                conn.request("GET", "/api/stats")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                stats = json.loads(resp.read())
                self.assertEqual(stats["memory_count"], 1)

                conn.close()
            finally:
                self._stop_dashboard(thread, server)


if __name__ == "__main__":
    unittest.main()
