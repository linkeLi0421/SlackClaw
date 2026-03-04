from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slackclaw.models import ApprovalStatus, MemoryCategory, MemoryRecord, MemoryScope, TaskStatus
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


    def test_list_tasks_and_count_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            store.upsert_task("t1", TaskStatus.PENDING, payload={"cmd": "a"})
            store.upsert_task("t2", TaskStatus.RUNNING, payload={"cmd": "b"})
            store.upsert_task("t3", TaskStatus.SUCCEEDED, payload={"cmd": "c"})

            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 3)
            task_ids = {t.task_id for t in tasks}
            self.assertEqual(task_ids, {"t1", "t2", "t3"})

            counts = store.count_tasks_by_status()
            self.assertEqual(counts.get("pending"), 1)
            self.assertEqual(counts.get("running"), 1)
            self.assertEqual(counts.get("succeeded"), 1)

            self.assertEqual(store.list_execution_locks(), [])
            store.acquire_execution_lock("global", "t2")
            locks = store.list_execution_locks()
            self.assertEqual(len(locks), 1)
            self.assertEqual(locks[0]["lock_key"], "global")

            self.assertEqual(store.list_agent_sessions(), [])
            store.upsert_agent_session("C1", "1.1", "codex", "sess-1")
            sessions = store.list_agent_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["agent"], "codex")

            self.assertEqual(store.list_checkpoints(), [])
            store.set_checkpoint("k1", "v1")
            checkpoints = store.list_checkpoints()
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["key"], "k1")

            store.close()


    def test_store_task_result_merges_into_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            store.upsert_task("t1", TaskStatus.SUCCEEDED, payload={"command_text": "sh:ls"})
            store.store_task_result("t1", "listed files", "file1.txt\nfile2.txt")

            row = store.get_task("t1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.payload["result_summary"], "listed files")
            self.assertEqual(row.payload["result_details"], "file1.txt\nfile2.txt")
            # original payload key preserved
            self.assertEqual(row.payload["command_text"], "sh:ls")
            store.close()

    def test_store_task_result_nonexistent_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            # Should not raise for a nonexistent task
            store.store_task_result("no-such-task", "summary", "details")
            self.assertIsNone(store.get_task("no-such-task"))
            store.close()


    # --- Memory tests ---

    def test_memory_upsert_and_retrieve(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            record = MemoryRecord(
                memory_id="mem-1",
                scope=MemoryScope.USER,
                scope_key="U1",
                category=MemoryCategory.FACT,
                content="Python 3.11 is used",
                file_path="/tmp/mem-1.md",
                source_task_id="t1",
                source_agent="claude",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            )
            store.upsert_memory(record)
            retrieved = store.get_memory("mem-1")
            self.assertIsNotNone(retrieved)
            assert retrieved is not None
            self.assertEqual(retrieved.memory_id, "mem-1")
            self.assertEqual(retrieved.scope, MemoryScope.USER)
            self.assertEqual(retrieved.content, "Python 3.11 is used")
            store.close()

    def test_memory_fts5_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            for i, content in enumerate(["Python 3.11 async await", "Docker container deploy", "Redis cache layer"]):
                store.upsert_memory(MemoryRecord(
                    memory_id=f"mem-{i}",
                    scope=MemoryScope.USER,
                    scope_key="U1",
                    category=MemoryCategory.FACT,
                    content=content,
                    file_path=f"/tmp/mem-{i}.md",
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                    last_accessed_at="2026-01-01T00:00:00+00:00",
                ))
            results = store.search_memories(scope_keys=["U1"], query="Python", limit=10)
            self.assertTrue(len(results) >= 1)
            self.assertTrue(any("Python" in r.content for r in results))
            store.close()

    def test_memory_search_across_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-u1",
                scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="user note about deployment",
                file_path="/tmp/u1.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            store.upsert_memory(MemoryRecord(
                memory_id="mem-ws",
                scope=MemoryScope.WORKSPACE, scope_key="workspace",
                category=MemoryCategory.PROCEDURE, content="workspace deployment procedure",
                file_path="/tmp/ws.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            results = store.search_memories(scope_keys=["U1", "workspace"], query="deployment", limit=10)
            self.assertEqual(len(results), 2)
            store.close()

    def test_memory_touch_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-1", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="test", file_path="/tmp/m.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            store.touch_memory_access("mem-1")
            record = store.get_memory("mem-1")
            assert record is not None
            self.assertEqual(record.access_count, 1)
            store.touch_memory_access("mem-1")
            record = store.get_memory("mem-1")
            assert record is not None
            self.assertEqual(record.access_count, 2)
            store.close()

    def test_memory_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-del", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.NOTE, content="delete me", file_path="/tmp/d.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            self.assertTrue(store.delete_memory("mem-del"))
            self.assertIsNone(store.get_memory("mem-del"))
            self.assertFalse(store.delete_memory("mem-del"))
            store.close()

    def test_memory_purge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-old", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="old fact", file_path="/tmp/old.md",
                created_at="2020-01-01T00:00:00+00:00",
                updated_at="2020-01-01T00:00:00+00:00",
                last_accessed_at="2020-01-01T00:00:00+00:00",
            ))
            store.upsert_memory(MemoryRecord(
                memory_id="mem-new", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="new fact", file_path="/tmp/new.md",
                created_at="2026-03-01T00:00:00+00:00",
                updated_at="2026-03-01T00:00:00+00:00",
                last_accessed_at="2026-03-01T00:00:00+00:00",
            ))
            purged = store.purge_old_memories(90)
            self.assertEqual(len(purged), 1)
            self.assertIn("/tmp/old.md", purged)
            self.assertIsNone(store.get_memory("mem-old"))
            self.assertIsNotNone(store.get_memory("mem-new"))
            store.close()

    def test_memory_count_by_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            for i in range(3):
                store.upsert_memory(MemoryRecord(
                    memory_id=f"mem-u-{i}", scope=MemoryScope.USER, scope_key="U1",
                    category=MemoryCategory.FACT, content=f"fact {i}", file_path=f"/tmp/u{i}.md",
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                    last_accessed_at="2026-01-01T00:00:00+00:00",
                ))
            store.upsert_memory(MemoryRecord(
                memory_id="mem-ws", scope=MemoryScope.WORKSPACE, scope_key="workspace",
                category=MemoryCategory.NOTE, content="ws note", file_path="/tmp/ws.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            counts = store.count_memories_by_scope()
            self.assertEqual(counts.get("U1"), 3)
            self.assertEqual(counts.get("workspace"), 1)
            self.assertEqual(store.count_memories_total(), 4)
            store.close()

    def test_memory_list_by_scope_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_memory(MemoryRecord(
                memory_id="mem-a", scope=MemoryScope.USER, scope_key="U1",
                category=MemoryCategory.FACT, content="A", file_path="/tmp/a.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            store.upsert_memory(MemoryRecord(
                memory_id="mem-b", scope=MemoryScope.USER, scope_key="U2",
                category=MemoryCategory.FACT, content="B", file_path="/tmp/b.md",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                last_accessed_at="2026-01-01T00:00:00+00:00",
            ))
            u1_mems = store.list_memories("U1")
            self.assertEqual(len(u1_mems), 1)
            self.assertEqual(u1_mems[0].memory_id, "mem-a")
            store.close()


if __name__ == "__main__":
    unittest.main()
