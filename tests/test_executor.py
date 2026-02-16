from __future__ import annotations

import json
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch
from pathlib import Path

from slackclaw.executor import TaskExecutor
from slackclaw.models import TaskSpec, TaskStatus
from slackclaw.state_store import StateStore


def _task(command_text: str) -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        channel_id="C111",
        message_ts="1.1",
        thread_ts="1.1",
        trigger_user="U1",
        trigger_text="!do x",
        command_text=command_text,
        lock_key="global",
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
                args=["kimi", "--quiet", "-p", "who are you"],
                returncode=0,
                stdout="I am kimi\n",
                stderr="",
            )
            result = executor.execute(_task("kimi:who are you"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "kimi command completed")
        self.assertIn("I am kimi", result.details)

    def test_codex_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["codex", "exec", "--skip-git-repo-check", "-C", "/tmp", "fix tests"],
                returncode=0,
                stdout="codex done\n",
                stderr="",
            )
            result = executor.execute(_task("codex:fix tests"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "codex command completed")
        self.assertIn("codex done", result.details)

    def test_claude_command_success(self) -> None:
        executor = TaskExecutor(dry_run=False, timeout_seconds=30)
        with patch("slackclaw.executor.subprocess.run") as mock_run:
            mock_run.return_value = CompletedProcess(
                args=["claude", "code", "review this repo"],
                returncode=0,
                stdout="claude done\n",
                stderr="",
            )
            result = executor.execute(_task("claude:review this repo"))
        self.assertEqual(result.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.summary, "claude command completed")
        self.assertIn("claude done", result.details)

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
            self.assertEqual(first_cmd[:2], ["codex", "exec"])
            self.assertEqual(second_cmd[:3], ["codex", "exec", "resume"])
            self.assertIn("thread-1", second_cmd)
            self.assertIn("agent=codex", store.get_thread_context("C111", "1.1"))
            store.close()


if __name__ == "__main__":
    unittest.main()
