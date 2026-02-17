from __future__ import annotations

import json
import os
import subprocess
from uuid import uuid4

from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .state_store import StateStore


_THREAD_CONTEXT_MAX_CHARS = 12000
_DEFAULT_AGENT_RESPONSE_INSTRUCTION = (
    "Format the final answer for Slack Markdown.\n"
    "- Start with a one-line summary.\n"
    "- Use short sections with bullets.\n"
    "- Put commands/code in fenced code blocks.\n"
    "- Skip CLI metadata/log headers."
)


class TaskExecutor:
    def __init__(
        self,
        *,
        dry_run: bool,
        timeout_seconds: int,
        response_format_instruction: str = _DEFAULT_AGENT_RESPONSE_INSTRUCTION,
    ) -> None:
        self._dry_run = dry_run
        self._timeout_seconds = timeout_seconds
        self._response_format_instruction = response_format_instruction.strip()
        self._agent_workdir = (os.environ.get("AGENT_WORKDIR") or "").strip()
        self._kimi_permission_mode = (os.environ.get("KIMI_PERMISSION_MODE") or "yolo").strip().lower()
        self._codex_permission_mode = (os.environ.get("CODEX_PERMISSION_MODE") or "full-auto").strip().lower()
        self._codex_sandbox_mode = (os.environ.get("CODEX_SANDBOX_MODE") or "workspace-write").strip().lower()
        self._claude_permission_mode = (os.environ.get("CLAUDE_PERMISSION_MODE") or "acceptEdits").strip()

    def execute(self, task: TaskSpec, *, store: StateStore | None = None) -> TaskExecutionResult:
        if self._dry_run:
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary=f"dry-run only, no command executed for {task.task_id}",
                details=f"planned command: {task.command_text}",
            )

        command = task.command_text
        if command.startswith("codex:"):
            prompt = command[6:].strip()
            if not prompt:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid codex command: empty prompt",
                    details="use format: codex:<prompt> or Slack message `CODEX <prompt>`",
                )
            return self._run_codex(prompt, task=task, store=store)

        if command.startswith("claude:"):
            prompt = command[7:].strip()
            if not prompt:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid claude command: empty prompt",
                    details="use format: claude:<prompt> or Slack message `CLAUDE <prompt>`",
                )
            return self._run_claude(prompt, task=task, store=store)

        if command.startswith("kimi:"):
            prompt = command[5:].strip()
            if not prompt:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid kimi command: empty prompt",
                    details="use format: kimi:<prompt> or Slack message `KIMI <prompt>`",
                )
            return self._run_kimi(prompt, task=task, store=store)

        if command.startswith("sh:"):
            shell_cmd = command[3:].strip()
            if not shell_cmd:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid shell command: empty payload",
                    details="use format: sh:<command>",
                )
            return self._run_shell(shell_cmd, task=task)

        return TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary=f"no-op executor completed for {task.task_id}",
            details=f"received command text: {task.command_text}",
        )

    def _run_shell(self, command: str, *, task: TaskSpec) -> TaskExecutionResult:
        env = os.environ.copy()
        if task.image_paths:
            joined = "\n".join(task.image_paths)
            env["SLACKCLAW_IMAGE_PATHS"] = joined
            env["SLACKCLAW_IMAGE_COUNT"] = str(len(task.image_paths))
        run_cwd = self._run_cwd()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                env=env,
                cwd=run_cwd,
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

    def _run_kimi(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> TaskExecutionResult:
        session_id = self._get_or_create_session(store, task, agent="kimi")
        prompt_with_context = self._prompt_with_context(prompt, task=task, store=store)
        run_cwd = self._run_cwd()
        cmd = ["kimi", "--quiet"]
        if run_cwd:
            cmd.extend(["-w", run_cwd])
        if self._kimi_permission_mode in {"yolo", "auto", "yes"}:
            cmd.append("--yolo")
        cmd.extend(["-S", session_id, "-p", prompt_with_context])
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"kimi command timed out after {self._timeout_seconds}s",
                details=prompt,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"kimi execution failed: {exc}",
                details=prompt,
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        details = "\n".join(part for part in [stdout, stderr] if part)
        if completed.returncode == 0:
            self._persist_session(store, task, agent="kimi", session_id=session_id)
            self._append_thread_context(store, task=task, prompt=prompt, response=stdout or details, agent="kimi")
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="kimi command completed",
                details=details or "<no output>",
            )
        return TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"kimi command exited with code {completed.returncode}",
            details=details or "<no output>",
        )

    def _run_codex(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> TaskExecutionResult:
        existing_session_id = store.get_agent_session(task.channel_id, task.thread_ts, "codex") if store else None
        prompt_with_context = self._prompt_with_context(prompt, task=task, store=store)
        run_cwd = self._run_cwd()
        codex_cwd = run_cwd or os.getcwd()
        if existing_session_id:
            cmd = [
                "codex",
                "exec",
                "resume",
            ]
            cmd.extend(self._codex_permission_flags(include_sandbox=False, codex_cwd=codex_cwd))
            cmd.extend(
                [
                    "--skip-git-repo-check",
                    "--json",
                    existing_session_id,
                    prompt_with_context,
                ]
            )
        else:
            cmd = [
                "codex",
                "exec",
            ]
            cmd.extend(self._codex_permission_flags(include_sandbox=True, codex_cwd=codex_cwd))
            cmd.extend(
                [
                    "--skip-git-repo-check",
                    "--json",
                    prompt_with_context,
                ]
            )
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"codex command timed out after {self._timeout_seconds}s",
                details=prompt_with_context,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"codex execution failed: {exc}",
                details=prompt_with_context,
            )

        events = self._parse_json_events(completed.stdout or "")
        session_id = self._extract_codex_session_id(events) or existing_session_id
        response = self._extract_codex_response(events)
        stderr = self._strip_codex_noise(completed.stderr or "")
        if not response:
            response = self._fallback_output(completed.stdout or "", stderr)

        if completed.returncode == 0:
            if session_id:
                self._persist_session(store, task, agent="codex", session_id=session_id)
            self._append_thread_context(store, task=task, prompt=prompt, response=response, agent="codex")
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="codex command completed",
                details=response or "<no output>",
            )
        return TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"codex command exited with code {completed.returncode}",
            details=response or "<no output>",
        )

    def _run_claude(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> TaskExecutionResult:
        prompt_with_context = self._prompt_with_context(prompt, task=task, store=store)
        run_cwd = self._run_cwd()
        cmd = ["claude", "-p"]
        if self._claude_permission_mode:
            cmd.extend(["--permission-mode", self._claude_permission_mode])
        if run_cwd:
            cmd.extend(["--add-dir", run_cwd])
        cmd.extend(["--", prompt_with_context])
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"claude command timed out after {self._timeout_seconds}s",
                details=prompt_with_context,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"claude execution failed: {exc}",
                details=prompt_with_context,
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        details = "\n".join(part for part in [stdout, stderr] if part)
        if completed.returncode == 0:
            self._append_thread_context(store, task=task, prompt=prompt, response=stdout or details, agent="claude")
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary="claude command completed",
                details=details or "<no output>",
            )
        return TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"claude command exited with code {completed.returncode}",
            details=details or "<no output>",
        )

    @staticmethod
    def _parse_json_events(text: str) -> list[dict]:
        events: list[dict] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _extract_codex_session_id(events: list[dict]) -> str:
        for event in events:
            if str(event.get("type") or "") == "thread.started":
                thread_id = str(event.get("thread_id") or "").strip()
                if thread_id:
                    return thread_id
        return ""

    @staticmethod
    def _extract_codex_response(events: list[dict]) -> str:
        messages: list[str] = []
        for event in events:
            if str(event.get("type") or "") != "item.completed":
                continue
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "agent_message":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                messages.append(text)
        if not messages:
            return ""
        return messages[-1]

    @staticmethod
    def _strip_codex_noise(text: str) -> str:
        if not text:
            return ""
        kept: list[str] = []
        for line in text.splitlines():
            if "state db missing rollout path for thread" in line:
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    @staticmethod
    def _fallback_output(stdout: str, stderr: str) -> str:
        non_json_stdout = "\n".join(
            line for line in stdout.splitlines() if not line.strip().startswith("{")
        ).strip()
        if non_json_stdout:
            return non_json_stdout
        return (stderr or "").strip()

    @staticmethod
    def _get_or_create_session(store: StateStore | None, task: TaskSpec, *, agent: str) -> str:
        if store is None:
            return str(uuid4())
        existing = store.get_agent_session(task.channel_id, task.thread_ts, agent)
        if existing:
            return existing
        return str(uuid4())

    @staticmethod
    def _persist_session(store: StateStore | None, task: TaskSpec, *, agent: str, session_id: str) -> None:
        if store is None or not session_id:
            return
        store.upsert_agent_session(task.channel_id, task.thread_ts, agent, session_id)

    def _prompt_with_context(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> str:
        if store is None:
            base_prompt = prompt
        else:
            context = store.get_thread_context(task.channel_id, task.thread_ts).strip()
            if not context:
                base_prompt = prompt
            else:
                base_prompt = (
                    "Shared thread context from previous agent runs:\n"
                    f"{context}\n\n"
                    f"Current request:\n{prompt}"
                )

        if task.image_paths:
            image_list = "\n".join(f"- {path}" for path in task.image_paths)
            base_prompt = (
                f"{base_prompt}\n\n"
                "Attached image file paths available on local disk:\n"
                f"{image_list}"
            )

        if not self._response_format_instruction:
            return base_prompt
        return (
            f"{base_prompt}\n\n"
            "Response format requirements:\n"
            f"{self._response_format_instruction}"
        )

    def _run_cwd(self) -> str | None:
        configured = self._agent_workdir
        if not configured:
            return None
        if os.path.isdir(configured):
            return configured
        return None

    def _codex_permission_flags(self, *, include_sandbox: bool, codex_cwd: str) -> list[str]:
        flags: list[str] = []
        mode = self._codex_permission_mode
        if mode in {"dangerous", "bypass", "dangerously-bypass-approvals-and-sandbox"}:
            flags.append("--dangerously-bypass-approvals-and-sandbox")
        elif mode == "full-auto":
            flags.append("--full-auto")

        if include_sandbox and mode not in {"dangerous", "bypass", "dangerously-bypass-approvals-and-sandbox"}:
            if self._codex_sandbox_mode in {"read-only", "workspace-write", "danger-full-access"}:
                flags.extend(["--sandbox", self._codex_sandbox_mode])
            flags.extend(["-C", codex_cwd])
        return flags

    @staticmethod
    def _append_thread_context(
        store: StateStore | None,
        *,
        task: TaskSpec,
        prompt: str,
        response: str,
        agent: str,
    ) -> None:
        if store is None:
            return
        clean_response = (response or "").strip()
        if not clean_response:
            return
        existing = store.get_thread_context(task.channel_id, task.thread_ts)
        entry = (
            f"agent={agent}\n"
            f"user={prompt.strip()}\n"
            f"assistant={clean_response}"
        )
        merged = entry if not existing else f"{existing}\n\n{entry}"
        if len(merged) > _THREAD_CONTEXT_MAX_CHARS:
            merged = merged[-_THREAD_CONTEXT_MAX_CHARS :]
        store.upsert_thread_context(task.channel_id, task.thread_ts, merged)
