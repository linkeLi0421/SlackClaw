from __future__ import annotations

import subprocess

from .models import TaskExecutionResult, TaskSpec, TaskStatus


def _trim(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class Reporter:
    def __init__(self, *, report_channel_id: str, desktop_report_script: str) -> None:
        self._report_channel_id = report_channel_id
        self._desktop_report_script = desktop_report_script

    def report(self, task: TaskSpec, result: TaskExecutionResult) -> None:
        status_icon = "✅" if result.status == TaskStatus.SUCCEEDED else "❌"
        text = "\n".join(
            [
                f"{status_icon} SlackClaw task {task.task_id}",
                f"source: {task.channel_id} @ {task.message_ts} by {task.trigger_user}",
                f"input: {_trim(task.command_text, 160)}",
                f"summary: {_trim(result.summary, 200)}",
                f"details: {_trim(result.details, 400)}",
            ]
        )
        self._post_to_slack(text)

    def _post_to_slack(self, text: str) -> None:
        cmd = [
            "python3",
            self._desktop_report_script,
            "--channel",
            self._report_channel_id,
            "--text",
            text,
            "--quiet",
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(f"reporter failed to post to slack: {stderr or completed.stdout}")
