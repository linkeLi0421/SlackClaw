from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from slackclaw.memory import (
    _build_memory_id,
    _delete_memory_file,
    _is_similar,
    _read_memory_file,
    _scope_subdir,
    _write_memory_file,
    build_memory_context,
    extract_and_store_memories,
    extract_prompt_keywords,
    extract_user_input_memories,
    handle_memory_command,
)
from slackclaw.models import MemoryCategory, MemoryRecord, MemoryScope
from slackclaw.state_store import StateStore


def _make_store(tmpdir: str) -> StateStore:
    store = StateStore(str(Path(tmpdir) / "state.db"))
    store.init_schema()
    return store


class TestBuildMemoryId(unittest.TestCase):
    def test_deterministic(self) -> None:
        id1 = _build_memory_id("user", "U1", "hello world")
        id2 = _build_memory_id("user", "U1", "hello world")
        self.assertEqual(id1, id2)
        self.assertEqual(len(id1), 16)

    def test_different_inputs_different_ids(self) -> None:
        id1 = _build_memory_id("user", "U1", "hello")
        id2 = _build_memory_id("user", "U1", "goodbye")
        self.assertNotEqual(id1, id2)


class TestIsSimilar(unittest.TestCase):
    def test_identical_strings(self) -> None:
        self.assertTrue(_is_similar("hello world", "hello world"))

    def test_very_different_strings(self) -> None:
        self.assertFalse(_is_similar("hello world", "the quick brown fox"))

    def test_similar_strings(self) -> None:
        self.assertTrue(_is_similar("project uses Python 3.11", "project uses Python 3.11"))

    def test_empty_strings(self) -> None:
        self.assertTrue(_is_similar("", ""))

    def test_one_empty(self) -> None:
        self.assertFalse(_is_similar("hello", ""))


class TestMemoryFileOps(unittest.TestCase):
    def test_scope_subdir_sanitizes_thread_key_for_windows_paths(self) -> None:
        subdir = _scope_subdir(MemoryScope.THREAD, "C0A7VR4J96X:1772664384.792709")
        self.assertEqual(subdir, "thread/C0A7VR4J96X_1772664384.792709")

    def test_write_and_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.md"
            record = MemoryRecord(
                memory_id="abc123",
                scope=MemoryScope.USER,
                scope_key="U1",
                category=MemoryCategory.FACT,
                content="Project uses Python 3.11",
                file_path=str(filepath),
                source_agent="claude",
                source_task_id="t1",
                created_at="2026-01-01T00:00:00+00:00",
            )
            _write_memory_file(filepath, record)
            self.assertTrue(filepath.exists())

            data = _read_memory_file(filepath)
            self.assertEqual(data["id"], "abc123")
            self.assertEqual(data["scope"], "user")
            self.assertEqual(data["content"], "Project uses Python 3.11")

    def test_delete_memory_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.md"
            filepath.write_text("test", encoding="utf-8")
            self.assertTrue(filepath.exists())
            _delete_memory_file(filepath)
            self.assertFalse(filepath.exists())

    def test_delete_nonexistent_file(self) -> None:
        _delete_memory_file(Path("/nonexistent/path/test.md"))


class TestHandleMemoryCommand(unittest.TestCase):
    def test_store_and_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            summary, details = handle_memory_command(
                "store", "project uses Python 3.11",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            self.assertEqual(summary, "memory stored")
            self.assertIn("Python 3.11", details)

            summary, details = handle_memory_command(
                "recall", "Python",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            self.assertIn("found", summary)
            self.assertIn("Python 3.11", details)
            store.close()

    def test_store_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            handle_memory_command(
                "store", "project uses Python 3.11",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            summary, _ = handle_memory_command(
                "store", "project uses Python 3.11",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            self.assertIn("duplicate", summary)
            store.close()

    def test_store_workspace_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            summary, details = handle_memory_command(
                "store", "workspace:deploy process uses Docker",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            self.assertEqual(summary, "memory stored")
            self.assertIn("scope=workspace", details)
            store.close()

    def test_forget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            handle_memory_command(
                "store", "delete me please",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            memories = store.list_memories("workspace")
            self.assertEqual(len(memories), 1)
            mem_id = memories[0].memory_id

            summary, _ = handle_memory_command(
                "forget", mem_id,
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            self.assertEqual(summary, "memory deleted")
            self.assertIsNone(store.get_memory(mem_id))
            store.close()

    def test_forget_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            summary, _ = handle_memory_command(
                "forget", "nonexistent_id",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("not found", summary)
            store.close()

    def test_list_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            summary, _ = handle_memory_command(
                "list", "",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("no memories", summary)
            store.close()

    def test_list_with_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            handle_memory_command(
                "store", "fact one",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            handle_memory_command(
                "store", "fact two completely different words here",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            summary, details = handle_memory_command(
                "list", "",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("2 memories", summary)
            store.close()

    def test_store_empty_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            summary, _ = handle_memory_command(
                "store", "",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("failed", summary)
            store.close()

    def test_recall_empty_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            summary, _ = handle_memory_command(
                "recall", "",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("failed", summary)
            store.close()

    def test_unknown_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            summary, _ = handle_memory_command(
                "foobar", "",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store,
            )
            self.assertIn("unknown", summary)
            store.close()


class TestExtractPromptKeywords(unittest.TestCase):
    def test_basic_extraction(self) -> None:
        keywords = extract_prompt_keywords("explain the deployment process for Python")
        self.assertIn("explain", keywords)
        self.assertIn("deployment", keywords)
        self.assertIn("process", keywords)
        self.assertIn("python", keywords)
        # Stopwords excluded
        self.assertNotIn("the", keywords)
        self.assertNotIn("for", keywords)

    def test_max_keywords(self) -> None:
        text = "one two three four five six seven eight nine ten"
        keywords = extract_prompt_keywords(text, max_keywords=3)
        self.assertEqual(len(keywords), 3)

    def test_empty_input(self) -> None:
        self.assertEqual(extract_prompt_keywords(""), [])

    def test_dedup(self) -> None:
        keywords = extract_prompt_keywords("test test test")
        self.assertEqual(keywords, ["test"])


class TestBuildMemoryContext(unittest.TestCase):
    def test_returns_relevant_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            handle_memory_command(
                "store", "deployment uses Docker containers on AWS",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            ctx = build_memory_context(
                store, trigger_user="U1", channel_id="C1", thread_ts="1.1",
                prompt_text="explain the deployment process",
            )
            self.assertIn("Relevant memories:", ctx)
            self.assertIn("Docker", ctx)
            store.close()

    def test_empty_when_no_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            ctx = build_memory_context(
                store, trigger_user="U1", channel_id="C1", thread_ts="1.1",
                prompt_text="explain something",
            )
            self.assertEqual(ctx, "")
            store.close()

    def test_respects_max_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            for i in range(20):
                handle_memory_command(
                    "store", f"unique_fact_{i} about topic alpha bravo",
                    trigger_user="U1", channel_id="C1", thread_ts="1.1",
                    store=store, memory_dir_path=mem_dir,
                )
            ctx = build_memory_context(
                store, trigger_user="U1", channel_id="C1", thread_ts="1.1",
                prompt_text="unique_fact alpha bravo topic",
                max_chars=100,
            )
            self.assertTrue(len(ctx) <= 200)  # header + budget
            store.close()

    def test_empty_prompt_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            ctx = build_memory_context(
                store, trigger_user="U1", channel_id="C1", thread_ts="1.1",
                prompt_text="",
            )
            self.assertEqual(ctx, "")
            store.close()

    def test_fallback_to_recent_memories_when_keywords_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            handle_memory_command(
                "store", "workspace:My name is xiaoli and I answer with encouragement",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )
            ctx = build_memory_context(
                store, trigger_user="U1", channel_id="C1", thread_ts="1.1",
                prompt_text="who are you?",
            )
            self.assertIn("Relevant memories:", ctx)
            self.assertIn("xiaoli", ctx)
            store.close()


class TestExtractAndStoreMemories(unittest.TestCase):
    def test_extracts_memory_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            text = (
                "Here is my answer.\n"
                "[MEMORY]: Project uses Python 3.11\n"
                "More text here.\n"
                "[REMEMBER]: Always run tests before deploying\n"
            )
            ids = extract_and_store_memories(
                store, text, agent="claude", trigger_user="U1",
                task_id="t1", channel_id="C1", thread_ts="1.1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids), 2)
            for mid in ids:
                record = store.get_memory(mid)
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.scope, MemoryScope.THREAD)
                self.assertEqual(record.scope_key, "C1:1.1")
            store.close()

    def test_dedup_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            text = "[MEMORY]: same fact\n"
            ids1 = extract_and_store_memories(
                store, text, agent="claude", trigger_user="U1",
                task_id="t1", channel_id="C1", thread_ts="1.1", memory_dir_path=mem_dir,
            )
            ids2 = extract_and_store_memories(
                store, text, agent="claude", trigger_user="U1",
                task_id="t2", channel_id="C1", thread_ts="1.1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids1), 1)
            self.assertEqual(len(ids2), 0)
            store.close()

    def test_no_tags_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            ids = extract_and_store_memories(
                store, "just a normal response",
                agent="claude", trigger_user="U1", task_id="t1",
            )
            self.assertEqual(ids, [])
            store.close()


class TestExtractUserInputMemories(unittest.TestCase):
    def test_stores_name_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            ids = extract_user_input_memories(
                store,
                "your name is xiaoli, you always answer with encouraging words",
                trigger_user="U1", channel_id="C1", thread_ts="1.1",
                task_id="t1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids), 1)
            record = store.get_memory(ids[0])
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.scope, MemoryScope.USER)
            self.assertEqual(record.category, MemoryCategory.PREFERENCE)
            self.assertIn("xiaoli", record.content)
            store.close()

    def test_stores_always_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            ids = extract_user_input_memories(
                store,
                "always use tabs instead of spaces",
                trigger_user="U1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids), 1)
            store.close()

    def test_stores_remember_that_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            ids = extract_user_input_memories(
                store,
                "remember that we deploy on Fridays",
                trigger_user="U1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids), 1)
            store.close()

    def test_ignores_normal_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            ids = extract_user_input_memories(
                store,
                "explain how the memory system works",
                trigger_user="U1",
            )
            self.assertEqual(ids, [])
            store.close()

    def test_dedup_identical_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            mem_dir = str(Path(tmpdir) / "mem")
            ids1 = extract_user_input_memories(
                store,
                "your name is xiaoli",
                trigger_user="U1", memory_dir_path=mem_dir,
            )
            ids2 = extract_user_input_memories(
                store,
                "your name is xiaoli",
                trigger_user="U1", memory_dir_path=mem_dir,
            )
            self.assertEqual(len(ids1), 1)
            self.assertEqual(len(ids2), 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
