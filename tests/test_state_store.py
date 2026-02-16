from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slackclaw.models import TaskStatus
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


if __name__ == "__main__":
    unittest.main()
