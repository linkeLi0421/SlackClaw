from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch
from pathlib import Path

from slackclaw.executor import TaskExecutor
from slackclaw.models import TaskSpec, TaskStatus
from slackclaw.state_store import StateStore


def _task(command_text: str, *, attachment_paths: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        channel_id="C111",
        message_ts="1.1",
        thread_ts="1.1",
        trigger_user="U1",
        trigger_text="!do x",
        command_text=command_text,
        lock_key="global",
        attachment_paths=attachment_paths,
    )


class ExecutorTests(unittest.TestCase):
    def test_dry_run_does_not_execute_shell(self) -> None:
        executor = TaskExecutor(dry_run=True, timeout_seconds=30)
        result = executor.execute(_task("sh:echo hello"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertIn("dry-run", result.summary)

    def test_shell_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("sh:printf ok"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "shell command completed")
        self.assertIn("ok", result.details)

    def test_shell_command_receives_attachment_env_vars(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["sh"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )
            result = executor.execute(_task("sh:echo hi", attachment_paths=("/tmp/a.png", "/tmp/b.jpg")))

        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        kwargs = mock_run.call_args.kwargs
        env = kwargs.get("env") or {}
        self.assertEqual(env.get("SLACKCLAW_ATTACHMENT_COUNT"), "2")
        self.assertIn("/tmp/a.png", str(env.get("SLACKCLAW_ATTACHMENT_PATHS", "")))
        self.assertIn("/tmp/b.jpg", str(env.get("SLACKCLAW_ATTACHMENT_PATHS", "")))
        # backward compat
        self.assertEqual(env.get("SLACKCLAW_IMAGE_COUNT"), "2")
        self.assertIn("/tmp/a.png", str(env.get("SLACKCLAW_IMAGE_PATHS", "")))

    def test_shell_prefix_with_empty_payload_fails(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("sh:   "))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("invalid shell command", result.summary)

    def test_shell_timeout_is_reported(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=1)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            from subprocess import TimeoutExpired

            mock_run.side_effect = TimeoutExpired(cmd="sleep 10", timeout=1)
            result = executor.execute(_task("sh:sleep 10"))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("timed out", result.summary)

    def test_kimi_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["kimi", "--quiet", "--yolo", "-S", "session", "-p", "who are you"],
                returncode=0,
                stdout="I am kimi\n",
                stderr="",
            )
            result = executor.execute(_task("kimi:who are you"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "kimi command completed")
        self.assertIn("I am kimi", result.details)
        cmd = mock_run.call_args.args[0]
        self.assertIn("--yolo", cmd)
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
        env = kwargs.get("env") or {}
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")
        self.assertEqual(env.get("PYTHONUTF8"), "1")

    def test_kimi_prompt_includes_attached_file_paths(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["kimi"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )
            _ = executor.execute(_task("kimi:describe image", attachment_paths=("/tmp/screen.png",)))

        prompt_arg = mock_run.call_args.args[0][-1]
        self.assertIn("Attached file paths on local disk", prompt_arg)
        self.assertIn("/tmp/screen.png", prompt_arg)

    def test_codex_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["codex", "exec", "--full-auto", "--sandbox", "workspace-write", "--json", "fix tests"],
                returncode=0,
                stdout="codex done\n",
                stderr="",
            )
            result = executor.execute(_task("codex:fix tests"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "codex command completed")
        self.assertIn("codex done", result.details)
        cmd = mock_run.call_args.args[0]
        self.assertIn("--full-auto", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("workspace-write", cmd)

    def test_claude_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["claude", "--permission-mode", "acceptEdits", "-p", "review this repo"],
                returncode=0,
                stdout="claude done\n",
                stderr="",
            )
            result = executor.execute(_task("claude:review this repo"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "claude command completed")
        self.assertIn("claude done", result.details)
        cmd = mock_run.call_args.args[0]
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[-2], "-p")
        self.assertIn("review this repo", cmd[-1])

    def test_agent_workdir_applies_to_all_agents_and_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "AGENT_WORKDIR": tmpdir,
                    "KIMI_PERMISSION_MODE": "yolo",
                    "CODEX_PERMISSION_MODE": "full-auto",
                    "CODEX_SANDBOX_MODE": "workspace-write",
                    "CLAUDE_PERMISSION_MODE": "acceptEdits",
                },
                clear=False,
            ):
                executor = TaskExecutor(dry_run=False, timeout_seconds=30)
                with patch("slackclaw.executor.subprocess.run") as mock_run:
                    mock_run.return_value = CompletedProcess(
                        args=["agent"],
                        returncode=0,
                        stdout="ok\n",
                        stderr="",
                    )
                    _ = executor.execute(_task("kimi:touch README"))
                    _ = executor.execute(_task("codex:touch README"))
                    _ = executor.execute(_task("claude:touch README"))
                    _ = executor.execute(_task("sh:pwd"))

                kimi_cmd = mock_run.call_args_list[0].args[0]
                codex_cmd = mock_run.call_args_list[1].args[0]
                claude_cmd = mock_run.call_args_list[2].args[0]
                kimi_kwargs = mock_run.call_args_list[0].kwargs
                codex_kwargs = mock_run.call_args_list[1].kwargs
                claude_kwargs = mock_run.call_args_list[2].kwargs
                shell_kwargs = mock_run.call_args_list[3].kwargs
                self.assertIn("-w", kimi_cmd)
                self.assertIn(tmpdir, kimi_cmd)
                self.assertIn("--yolo", kimi_cmd)
                self.assertIn("-C", codex_cmd)
                self.assertIn(tmpdir, codex_cmd)
                self.assertIn("--full-auto", codex_cmd)
                self.assertIn("--add-dir", claude_cmd)
                self.assertIn(tmpdir, claude_cmd)
                self.assertIn("-p", claude_cmd)
                self.assertEqual(kimi_kwargs.get("cwd"), tmpdir)
                self.assertEqual(codex_kwargs.get("cwd"), tmpdir)
                self.assertEqual(claude_kwargs.get("cwd"), tmpdir)
                self.assertEqual(shell_kwargs.get("cwd"), tmpdir)

    def test_codex_uses_json_output_and_resumes_session_per_thread(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()

            first_events = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "first answer"},
                        }
                    ),
                ]
            )
            second_events = "\n".join(
                [
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "second answer"},
                        }
                    ),
                ]
            )

            with patch("slackclaw.executor.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=first_events,
                        stderr="ERROR state db missing rollout path for thread x",
                    ),
                    CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=second_events,
                        stderr="",
                    ),
                ]

                result1 = executor.execute(_task("codex:one"), store=store)
                result2 = executor.execute(_task("codex:two"), store=store)

            self.assertEqual(result1.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result1.details, "first answer")
            self.assertEqual(result2.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result2.details, "second answer")
            self.assertEqual(store.get_agent_session("C111", "1.1", "codex"), "thread-1")

            first_cmd = mock_run.call_args_list[0][0][0]
            second_cmd = mock_run.call_args_list[1][0][0]
            self.assertIn("--json", first_cmd)
            self.assertTrue(os.path.basename(first_cmd[0]).lower().startswith("codex"))
            self.assertEqual(first_cmd[1], "exec")
            self.assertTrue(os.path.basename(second_cmd[0]).lower().startswith("codex"))
            self.assertEqual(second_cmd[1:3], ["exec", "resume"])
            self.assertIn("thread-1", second_cmd)
            self.assertIn("agent=codex", store.get_thread_context("C111", "1.1"))
            store.close()

    def test_file_command_valid_path(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"hello world")
            filepath = f.name
        try:
            result = executor.execute(_task(f"file:{filepath}"))
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("file ready for upload", result.summary)
            self.assertEqual(result.upload_file_path, filepath)
        finally:
            os.unlink(filepath)

    def test_file_command_missing_path(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("file:/nonexistent/path/foo.txt"))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("file not found", result.summary)
        self.assertEqual(result.upload_file_path, "")

    def test_file_command_empty_path(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        result = executor.execute(_task("file:   "))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("invalid file command", result.summary)


    def test_memory_command_disabled(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30, memory_enabled=False)
        result = executor.execute(_task("memory:store hello world"))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("disabled", result.summary)

    def test_memory_command_enabled_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            mem_dir = str(Path(tmpdir) / "mem")
            executor = TaskExecutor(
                dry_run=False, timeout_seconds=30,
                memory_enabled=True, memory_dir=mem_dir,
            )
            result = executor.execute(_task("memory:store project uses Python"), store=store)
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertIn("memory stored", result.summary)
            store.close()

    def test_memory_empty_args(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30, memory_enabled=True)
        result = executor.execute(_task("memory:   "))
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("invalid memory command", result.summary)

    def test_prompt_includes_memory_context_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            mem_dir = str(Path(tmpdir) / "mem")

            # Store a memory first
            from slackclaw.memory import handle_memory_command
            handle_memory_command(
                "store", "deployment uses Docker containers",
                trigger_user="U1", channel_id="C111", thread_ts="1.1",
                store=store, memory_dir_path=mem_dir,
            )

            executor = TaskExecutor(
                dry_run=False, timeout_seconds=30,
                memory_enabled=True, memory_dir=mem_dir,
                agent_workdir=tmpdir,
            )
            claude_md = Path(tmpdir) / "CLAUDE.md"
            claude_md.write_text("# Test\n", encoding="utf-8")
            injected_content = None

            original_run = subprocess.run
            def capture_run(*args, **kwargs):
                nonlocal injected_content
                injected_content = claude_md.read_text(encoding="utf-8")
                return CompletedProcess(args=["claude"], returncode=0, stdout="done\n", stderr="")

            with patch("slackclaw.executor.subprocess.run", side_effect=capture_run):
                executor.execute(_task("claude:explain the deployment process"), store=store)

            # Memory context was injected into CLAUDE.md during execution
            self.assertIsNotNone(injected_content)
            self.assertIn("Relevant memories:", injected_content)
            self.assertIn("Docker", injected_content)
            # CLAUDE.md is restored after execution
            self.assertEqual(claude_md.read_text(encoding="utf-8"), "# Test\n")
            store.close()

    def test_auto_extract_memories_from_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            mem_dir = str(Path(tmpdir) / "mem")
            executor = TaskExecutor(
                dry_run=False, timeout_seconds=30,
                memory_enabled=True, memory_dir=mem_dir, memory_auto_extract=True,
            )
            with patch("slackclaw.executor.subprocess.run") as mock_run:
                mock_run.return_value = CompletedProcess(
                    args=["claude"], returncode=0,
                    stdout="Here is the answer.\n[MEMORY]: Always run tests before deploying\n", stderr="",
                )
                result = executor.execute(_task("claude:help me"), store=store)

            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            memories = store.list_memories("C111:1.1")
            self.assertTrue(len(memories) >= 1)
            contents = [m.content for m in memories]
            self.assertTrue(any("Always run tests" in c for c in contents))
            store.close()

    def test_identity_query_includes_profile_hints_from_thread_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_thread_context(
                "C111",
                "1.1",
                (
                    "agent=kimi\n"
                    "user=your name is xiaoli, you always answer with encouraging words.\n"
                    "assistant=My name is xiaoli and I always answer with encouraging words."
                ),
            )
            executor = TaskExecutor(
                dry_run=False,
                timeout_seconds=30,
                memory_enabled=True,
                agent_workdir=tmpdir,
            )
            claude_md = Path(tmpdir) / "CLAUDE.md"
            claude_md.write_text("# Test\n", encoding="utf-8")
            injected_content = None

            def capture_run(*args, **kwargs):
                nonlocal injected_content
                injected_content = claude_md.read_text(encoding="utf-8")
                return CompletedProcess(args=["claude"], returncode=0, stdout="ok\n", stderr="")

            with patch("slackclaw.executor.subprocess.run", side_effect=capture_run):
                executor.execute(_task("claude:who are you"), store=store)

            # Profile hints are injected into CLAUDE.md during execution
            self.assertIsNotNone(injected_content)
            self.assertIn("Assistant profile:", injected_content)
            self.assertIn("Preferred assistant name: xiaoli", injected_content)
            self.assertIn("Preferred tone: encouraging and positive", injected_content)
            self.assertIn("Identity response rule:", injected_content)
            store.close()

    def test_identity_query_uses_user_only_thread_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(str(Path(tmpdir) / "state.db"))
            store.init_schema()
            store.upsert_thread_context(
                "C111",
                "1.1",
                (
                    "agent=codex\n"
                    "user=who are you\n"
                    "assistant=Please paste the hints after Conversation profile hints\n"
                    "agent=kimi\n"
                    "user=your name is xiaoli, you always answer with encouraging words\n"
                    "assistant=My name is xiaoli\n"
                ),
            )
            executor = TaskExecutor(
                dry_run=False,
                timeout_seconds=30,
                memory_enabled=True,
                agent_workdir=tmpdir,
            )
            agents_md = Path(tmpdir) / "AGENTS.md"
            agents_md.write_text("# Test\n", encoding="utf-8")
            injected_content = None

            def capture_run(*args, **kwargs):
                nonlocal injected_content
                injected_content = agents_md.read_text(encoding="utf-8")
                return CompletedProcess(args=["codex"], returncode=0, stdout="ok\n", stderr="")

            with patch("slackclaw.executor.subprocess.run", side_effect=capture_run):
                executor.execute(_task("codex:who are you"), store=store)

            # Thread context is injected into AGENTS.md during execution
            self.assertIsNotNone(injected_content)
            self.assertIn("Shared thread context from previous user messages:", injected_content)
            self.assertIn("- who are you", injected_content)
            self.assertIn("- your name is xiaoli, you always answer with encouraging words", injected_content)
            self.assertNotIn("Please paste the hints after Conversation profile hints", injected_content)
            # AGENTS.md is restored after execution
            self.assertEqual(agents_md.read_text(encoding="utf-8"), "# Test\n")
            store.close()


if __name__ == "__main__":
    unittest.main()
