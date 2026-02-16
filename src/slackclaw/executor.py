from __future__ import annotations

import subprocess

from .models import TaskExecutionResult, TaskSpec, TaskStatus


class TaskExecutor:
    def __init__(self, *, dry_run: bool, timeout_seconds: int) -> None:
        self._dry_run = dry_run
        self._timeout_seconds = timeout_seconds

    def execute(self, task: TaskSpec) -> TaskExecutionResult:
        if self._dry_run:
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary=f"dry-run only, no command executed for {task.task_id}",
                details=f"planned command: {task.command_text}",
            )

        command = task.command_text
        if command.startswith("sh:"):
            shell_cmd = command[3:].strip()
            if not shell_cmd:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid shell command: empty payload",
                    details="use format: sh:<command>",
                )
            return self._run_shell(shell_cmd)

        return TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary=f"no-op executor completed for {task.task_id}",
            details=f"received command text: {task.command_text}",
        )

    def _run_shell(self, command: str) -> TaskExecutionResult:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"shell command timed out after {self._timeout_seconds}s",
                details=command,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"shell execution failed: {exc}",
                details=command,
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        details = "\n".join(part for part in [stdout, stderr] if part)
        if completed.returncode == 0:
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="shell command completed",
                details=details or "<no output>",
            )
        return TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"shell command exited with code {completed.returncode}",
            details=details or "<no output>",
        )
