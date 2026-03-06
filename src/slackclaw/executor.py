from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from uuid import uuid4

from .memory import (
    build_memory_context,
    extract_and_store_memories,
    handle_memory_command,
)
from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .state_store import StateStore


_THREAD_CONTEXT_MAX_CHARS = 12000
_IDENTITY_QUERY_RE = re.compile(
    r"\b(who are you|what(?:'s| is) your name|do you know)\b",
    re.IGNORECASE,
)
_THREAD_USER_LINE_RE = re.compile(r"^user=(.*)$", re.MULTILINE)


def _resolve_cli(name: str) -> str:
    """Resolve a CLI name to its full path.

    On Windows, subprocess.run with a list arg does not find .cmd/.bat
    wrappers (e.g. claude.cmd installed by npm).  shutil.which handles
    this correctly across platforms.
    """
    resolved = shutil.which(name)
    return resolved if resolved else name
_DEFAULT_AGENT_RESPONSE_INSTRUCTION = (
    "Format the final answer for Slack Markdown.\n"
    "- Start with a one-line summary.\n"
    "- Use short sections with bullets.\n"
    "- Put commands/code in fenced code blocks.\n"
    "- Skip CLI metadata/log headers."
)
_MEMORY_EXTRACTION_INSTRUCTION = (
    "If you discover an important fact, decision, user preference, or reusable procedure "
    "worth remembering for future conversations, output it on its own line as:\n"
    "[MEMORY]: <concise fact>\n"
    "Examples:\n"
    "[MEMORY]: Project uses Python 3.11 with pytest for testing\n"
    "[MEMORY]: User prefers tabs over spaces\n"
    "[MEMORY]: Deploy process: git tag vX.Y.Z then push to trigger CI\n"
    "Only tag genuinely durable facts — skip transient details or things already in memory."
)

class TaskExecutor:
    def __init__(
        self,
        *,
        dry_run: bool,
        timeout_seconds: int,
        response_format_instruction: str = _DEFAULT_AGENT_RESPONSE_INSTRUCTION,
        memory_enabled: bool = False,
        memory_dir: str = "",
        memory_injection_max_chars: int = 2000,
        memory_auto_extract: bool = False,
    ) -> None:
        self._dry_run = dry_run
        self._timeout_seconds = timeout_seconds
        self._response_format_instruction = response_format_instruction.strip()
        self._memory_enabled = memory_enabled
        self._memory_dir = memory_dir
        self._memory_injection_max_chars = memory_injection_max_chars
        self._memory_auto_extract = memory_auto_extract
        self._agent_workdir = (os.environ.get("AGENT_WORKDIR") or "").strip()
        self._kimi_permission_mode = (os.environ.get("KIMI_PERMISSION_MODE") or "yolo").strip().lower()
        self._codex_permission_mode = (os.environ.get("CODEX_PERMISSION_MODE") or "full-auto").strip().lower()
        self._codex_sandbox_mode = (os.environ.get("CODEX_SANDBOX_MODE") or "workspace-write").strip().lower()
        self._claude_permission_mode = (os.environ.get("CLAUDE_PERMISSION_MODE") or "acceptEdits").strip()

    @staticmethod
    def _agent_env() -> dict[str, str]:
        env = os.environ.copy()
        # Force UTF-8 for Python-based CLIs on Windows to avoid cp1252/charmap failures
        # when prompts or model outputs include emoji/non-ASCII text.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def execute(self, task: TaskSpec, *, store: StateStore | None = None) -> TaskExecutionResult:
        if self._dry_run:
            return TaskExecutionResult(
                status=TaskStatus.SUCCEEDED,
                summary=f"dry-run only, no command executed for {task.task_id}",
                details=(
                    f"planned command: {task.command_text}\n\n"
                    "⚠️ *Dry-run mode is enabled.* No commands are actually executed. "
                    "To run commands for real, set `DRY_RUN=false` in your config and restart SlackClaw."
                ),
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

        if command.startswith("memory:"):
            mem_args = command[7:].strip()
            if not mem_args:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid memory command: empty args",
                    details="use format: MEMORY store|recall|forget|list <args>",
                )
            # Split subcommand from rest
            parts = mem_args.split(None, 1)
            subcommand = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            if not self._memory_enabled:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="memory system is disabled",
                    details="set MEMORY_ENABLED=true to enable the memory system",
                )
            if store is None:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="memory command requires state store",
                    details="no state store available",
                )
            summary, details = handle_memory_command(
                subcommand, rest,
                trigger_user=task.trigger_user,
                channel_id=task.channel_id,
                thread_ts=task.thread_ts,
                store=store,
                memory_dir_path=self._memory_dir,
            )
            status = TaskStatus.SUCCEEDED if "failed" not in summary else TaskStatus.FAILED
            return TaskExecutionResult(status=status, summary=summary, details=details)

        if command.startswith("file:"):
            filepath = command[5:].strip()
            if not filepath:
                return TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary="invalid file command: empty path",
                    details="use format: file:<path> or Slack message `FILE <path>`",
                )
            return self._run_file(filepath)

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

    @staticmethod
    def _run_file(filepath: str) -> TaskExecutionResult:
        if not os.path.isfile(filepath):
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"file not found: {filepath}",
                details=f"The path '{filepath}' does not exist or is not a regular file.",
            )
        return TaskExecutionResult(
            status=TaskStatus.SUCCEEDED,
            summary=f"file ready for upload: {os.path.basename(filepath)}",
            details=f"uploading {filepath}",
            upload_file_path=filepath,
        )

    def _run_shell(self, command: str, *, task: TaskSpec) -> TaskExecutionResult:
        env = os.environ.copy()
        if task.attachment_paths:
            joined = "\n".join(task.attachment_paths)
            env["SLACKCLAW_ATTACHMENT_PATHS"] = joined
            env["SLACKCLAW_ATTACHMENT_COUNT"] = str(len(task.attachment_paths))
            # backward compat
            env["SLACKCLAW_IMAGE_PATHS"] = joined
            env["SLACKCLAW_IMAGE_COUNT"] = str(len(task.attachment_paths))
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
        cmd = [_resolve_cli("kimi"), "--quiet"]
        if run_cwd:
            cmd.extend(["-w", run_cwd])
        if self._kimi_permission_mode in {"yolo", "auto", "yes"}:
            cmd.append("--yolo")
        cmd.extend(["-S", session_id, "-p", prompt_with_context])
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
                env=self._agent_env(),
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
            self._auto_extract_memories(store, details or stdout or "", agent="kimi", task=task)
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
        system_ctx = self._build_system_context(prompt, task=task, store=store)
        user_prompt = prompt
        if task.attachment_paths:
            attachment_list = "\n".join(f"- {path}" for path in task.attachment_paths)
            user_prompt = (
                f"{user_prompt}\n\n"
                "Attached file paths on local disk:\n"
                f"{attachment_list}"
            )
        # When system context is present, combine it with the user prompt and
        # pipe via stdin (using "-" placeholder) to avoid CLI argument parsing
        # issues with long multiline content.
        stdin_text: str | None = None
        if system_ctx:
            combined = f"{system_ctx}\n\n{user_prompt}"
            stdin_text = combined
            prompt_arg = "-"
        else:
            prompt_arg = user_prompt

        run_cwd = self._run_cwd()
        codex_cwd = run_cwd or os.getcwd()
        codex_bin = _resolve_cli("codex")
        if existing_session_id:
            cmd = [
                codex_bin,
                "exec",
                "resume",
            ]
            cmd.extend(self._codex_permission_flags(include_sandbox=False, codex_cwd=codex_cwd))
            cmd.extend(
                [
                    "--skip-git-repo-check",
                    "--json",
                    existing_session_id,
                    prompt_arg,
                ]
            )
        else:
            cmd = [
                codex_bin,
                "exec",
            ]
            cmd.extend(self._codex_permission_flags(include_sandbox=True, codex_cwd=codex_cwd))
            cmd.extend(
                [
                    "--skip-git-repo-check",
                    "--json",
                    prompt_arg,
                ]
            )
        try:
            completed = subprocess.run(
                cmd,
                input=stdin_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
                env=self._agent_env(),
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"codex command timed out after {self._timeout_seconds}s",
                details=user_prompt,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"codex execution failed: {exc}",
                details=user_prompt,
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
            self._auto_extract_memories(store, response or "", agent="codex", task=task)
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
        system_ctx = self._build_system_context(prompt, task=task, store=store)
        user_prompt = prompt
        if task.attachment_paths:
            attachment_list = "\n".join(f"- {path}" for path in task.attachment_paths)
            user_prompt = (
                f"{user_prompt}\n\n"
                "Attached file paths on local disk (use the Read tool to open them):\n"
                f"{attachment_list}"
            )
        run_cwd = self._run_cwd()
        cmd = [_resolve_cli("claude")]
        if self._claude_permission_mode:
            cmd.extend(["--permission-mode", self._claude_permission_mode])
        if run_cwd:
            cmd.extend(["--add-dir", run_cwd])
        if task.attachment_paths:
            seen_dirs: set[str] = set()
            for path in task.attachment_paths:
                parent = os.path.dirname(os.path.abspath(path))
                if parent and parent not in seen_dirs:
                    seen_dirs.add(parent)
                    cmd.extend(["--add-dir", parent])

        # System context goes via --append-system-prompt (treated as system
        # instructions, not user input).  User prompt is piped via stdin to
        # avoid Windows CLI argument length limits.
        if system_ctx:
            cmd.extend(["--append-system-prompt", system_ctx])
        cmd.append("-p")

        try:
            completed = subprocess.run(
                cmd,
                input=user_prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
                cwd=run_cwd,
                env=self._agent_env(),
            )
        except subprocess.TimeoutExpired:
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"claude command timed out after {self._timeout_seconds}s",
                details=user_prompt,
            )
        except Exception as exc:  # pragma: no cover - OS-level failures
            return TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"claude execution failed: {exc}",
                details=user_prompt,
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        details = "\n".join(part for part in [stdout, stderr] if part)
        if completed.returncode == 0:
            self._append_thread_context(store, task=task, prompt=prompt, response=stdout or details, agent="claude")
            self._auto_extract_memories(store, details or stdout or "", agent="claude", task=task)
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

    def _build_system_context(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> str:
        """Build a system-level context string (memory, profile, thread context, format instructions).

        This is injected via --append-system-prompt for Claude Code or prepended
        for agents that lack a system prompt flag.
        """
        parts: list[str] = []

        # Memory context
        if self._memory_enabled and store is not None:
            memory_section = build_memory_context(
                store,
                trigger_user=task.trigger_user,
                channel_id=task.channel_id,
                thread_ts=task.thread_ts,
                prompt_text=prompt,
                max_chars=self._memory_injection_max_chars,
            )
            if memory_section:
                parts.append(memory_section)

        # Profile hints from thread context and memories
        context = ""
        if store is not None:
            context = store.get_thread_context(task.channel_id, task.thread_ts).strip()
        identity_query = bool(_IDENTITY_QUERY_RE.search(prompt))

        profile_hints = self._conversation_profile_hints(
            prompt=prompt,
            context=context,
            task=task,
            store=store,
        )
        if profile_hints:
            parts.append(profile_hints)
            if identity_query:
                parts.append(
                    "Identity response rule:\n"
                    "- If the user asks about your identity or name, answer directly using profile hints.\n"
                    "- Do not claim memory is missing when profile hints are present.\n"
                    "- Keep it brief: one short paragraph plus up to 3 bullets."
                )

        # Thread context
        if context:
            if identity_query:
                user_only = self._user_only_thread_context(context)
                if user_only:
                    parts.append(
                        "Shared thread context from previous user messages:\n"
                        f"{user_only}"
                    )
            else:
                parts.append(
                    "Shared thread context from previous agent runs:\n"
                    f"{context}"
                )

        # Response format instructions
        if self._response_format_instruction:
            parts.append(
                "Response format requirements:\n"
                f"{self._response_format_instruction}"
            )

        # Memory extraction instruction
        if self._memory_enabled and self._memory_auto_extract:
            parts.append(_MEMORY_EXTRACTION_INSTRUCTION)

        return "\n\n".join(parts)

    def _prompt_with_context(self, prompt: str, *, task: TaskSpec, store: StateStore | None) -> str:
        """Build a single combined prompt (system context + user prompt).

        Used by Kimi and as fallback when separate system prompt injection is not available.
        """
        system_ctx = self._build_system_context(prompt, task=task, store=store)

        base_prompt = prompt
        if task.attachment_paths:
            attachment_list = "\n".join(f"- {path}" for path in task.attachment_paths)
            base_prompt = (
                f"{base_prompt}\n\n"
                "Attached file paths on local disk (use the Read tool to open them):\n"
                f"{attachment_list}"
            )

        if system_ctx:
            return f"{system_ctx}\n\n{base_prompt}"
        return base_prompt

    @staticmethod
    def _conversation_profile_hints(
        *,
        prompt: str,
        context: str,
        task: TaskSpec,
        store: StateStore | None,
    ) -> str:
        blobs: list[str] = []
        if context:
            blobs.append(context)

        if store is not None:
            scope_keys = [f"{task.channel_id}:{task.thread_ts}", "workspace", task.trigger_user]
            for key in scope_keys:
                for mem in store.list_memories(key, limit=8):
                    blobs.append(mem.content)

        if not blobs:
            return ""

        merged = "\n".join(blobs)
        name = ""
        for pattern in (
            r"\bmy name is\s+([A-Za-z0-9_.-]{2,40})\b",
            r"\bi(?:'m| am)\s+([A-Za-z0-9_.-]{2,40})\b",
            r"\byour name is\s+([A-Za-z0-9_.-]{2,40})\b",
        ):
            for match in re.finditer(pattern, merged, flags=re.IGNORECASE):
                candidate = match.group(1).strip()
                if candidate:
                    name = candidate

        lines: list[str] = []
        if name:
            lines.append(f"- Preferred assistant name: {name}")
        if re.search(r"encourag", merged, flags=re.IGNORECASE):
            lines.append("- Preferred tone: encouraging and positive")

        if not lines:
            return ""
        return "Assistant profile:\n" + "\n".join(lines)

    @staticmethod
    def _user_only_thread_context(context: str, max_items: int = 8) -> str:
        lines = [m.group(1).strip() for m in _THREAD_USER_LINE_RE.finditer(context)]
        lines = [ln for ln in lines if ln]
        if not lines:
            return ""
        recent = lines[-max_items:]
        return "\n".join(f"- {line}" for line in recent)

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

    def _auto_extract_memories(
        self,
        store: StateStore | None,
        result_text: str,
        *,
        agent: str,
        task: TaskSpec,
    ) -> None:
        if not self._memory_auto_extract or store is None or not result_text:
            return
        try:
            extract_and_store_memories(
                store, result_text, agent,
                trigger_user=task.trigger_user,
                channel_id=task.channel_id,
                thread_ts=task.thread_ts,
                task_id=task.task_id,
                memory_dir_path=self._memory_dir,
            )
        except Exception:
            pass  # best-effort, don't fail the task

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
