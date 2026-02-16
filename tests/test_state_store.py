from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slackclaw.models import ApprovalStatus, TaskStatus
from slackclaw.state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def test_checkpoint_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            store = StateStore(str(db_path))
            store.init_schema()

            self.assertIsNone(store.get_checkpoint("last_ts"))
            store.set_checkpoint("last_ts", "123.45")
            self.assertEqual(store.get_checkpoint("last_ts"), "123.45")

            store.close()

    def test_mark_message_processed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            self.assertTrue(store.mark_message_processed("C123", "1.1"))
            self.assertTrue(store.is_message_processed("C123", "1.1"))
            self.assertFalse(store.mark_message_processed("C123", "1.1"))

            store.close()

    def test_task_upsert_and_status_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            store.upsert_task("task-1", TaskStatus.PENDING, payload={"text": "build"})
            row = store.get_task("task-1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, TaskStatus.PENDING)
            self.assertEqual(row.payload, {"text": "build"})

            store.update_task_status("task-1", TaskStatus.RUNNING)
            row = store.get_task("task-1")
            assert row is not None
            self.assertEqual(row.status, TaskStatus.RUNNING)

            store.close()

    def test_transition_task_status_is_compare_and_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_task("task-1", TaskStatus.PENDING, payload={})

            self.assertTrue(store.transition_task_status("task-1", TaskStatus.PENDING, TaskStatus.RUNNING))
            self.assertFalse(store.transition_task_status("task-1", TaskStatus.PENDING, TaskStatus.SUCCEEDED))
            row = store.get_task("task-1")
            assert row is not None
            self.assertEqual(row.status, TaskStatus.RUNNING)
            store.close()

    def test_running_tasks_marked_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_task("task-1", TaskStatus.RUNNING, payload={})
            store.upsert_task("task-2", TaskStatus.SUCCEEDED, payload={})

            changed = store.mark_running_tasks_aborted()
            self.assertEqual(changed, 1)

            row1 = store.get_task("task-1")
            row2 = store.get_task("task-2")
            assert row1 is not None
            assert row2 is not None
            self.assertEqual(row1.status, TaskStatus.ABORTED_ON_RESTART)
            self.assertEqual(row2.status, TaskStatus.SUCCEEDED)
            store.close()

    def test_execution_lock_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            self.assertTrue(store.acquire_execution_lock("global", "task-1"))
            self.assertFalse(store.acquire_execution_lock("global", "task-2"))

            store.release_execution_lock("global", "task-1")
            self.assertTrue(store.acquire_execution_lock("global", "task-2"))
            store.close()

    def test_task_approval_roundtrip_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            store.upsert_task_approval(
                task_id="task-1",
                channel_id="C123",
                source_message_ts="1.1",
                approval_message_ts="1.2",
                approve_reaction="white_check_mark",
                reject_reaction="x",
            )

            pending = store.get_pending_approval_for_message("C123", "1.1")
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.status, ApprovalStatus.PENDING)
            self.assertEqual(pending.task_id, "task-1")

            pending_by_plan = store.get_pending_approval_for_message("C123", "1.2")
            self.assertIsNotNone(pending_by_plan)
            assert pending_by_plan is not None
            self.assertEqual(pending_by_plan.task_id, "task-1")

            resolved = store.resolve_task_approval(
                task_id="task-1",
                status=ApprovalStatus.APPROVED,
                decided_by="U1",
                decision_reaction="white_check_mark",
            )
            self.assertTrue(resolved)

            row = store.get_task_approval("task-1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, ApprovalStatus.APPROVED)
            self.assertEqual(row.decided_by, "U1")
            self.assertEqual(row.decision_reaction, "white_check_mark")

            unresolved = store.resolve_task_approval(
                task_id="task-1",
                status=ApprovalStatus.REJECTED,
                decided_by="U2",
                decision_reaction="x",
            )
            self.assertFalse(unresolved)
            store.close()

    def test_agent_session_and_thread_context_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            self.assertIsNone(store.get_agent_session("C123", "1.1", "codex"))
            store.upsert_agent_session("C123", "1.1", "codex", "session-1")
            self.assertEqual(store.get_agent_session("C123", "1.1", "codex"), "session-1")

            store.upsert_agent_session("C123", "1.1", "codex", "session-2")
            self.assertEqual(store.get_agent_session("C123", "1.1", "codex"), "session-2")

            self.assertEqual(store.get_thread_context("C123", "1.1"), "")
            store.upsert_thread_context("C123", "1.1", "ctx-a")
            self.assertEqual(store.get_thread_context("C123", "1.1"), "ctx-a")
            store.upsert_thread_context("C123", "1.1", "ctx-b")
            self.assertEqual(store.get_thread_context("C123", "1.1"), "ctx-b")
            store.close()


if __name__ == "__main__":
    unittest.main()
