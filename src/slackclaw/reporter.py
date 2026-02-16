from __future__ import annotations

from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .slack_api import SlackWebClient


def _trim(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


class Reporter:
    def __init__(
        self,
        *,
        client: SlackWebClient,
        report_channel_id: str,
        input_max_chars: int = 500,
        summary_max_chars: int = 1200,
        details_max_chars: int = 4000,
    ) -> None:
        self._client = client
        self._report_channel_id = report_channel_id
        self._input_max_chars = input_max_chars
        self._summary_max_chars = summary_max_chars
        self._details_max_chars = details_max_chars

    def report(self, task: TaskSpec, result: TaskExecutionResult) -> None:
        status_icon = "✅" if result.status == TaskStatus.SUCCEEDED else "❌"
        text = "\n".join(
            [
                f"{status_icon} SlackClaw task {task.task_id}",
                f"source: {task.channel_id} @ {task.message_ts} by {task.trigger_user}",
                f"input: {_trim(task.command_text, self._input_max_chars)}",
                f"summary: {_trim(result.summary, self._summary_max_chars)}",
                f"details: {_trim(result.details, self._details_max_chars)}",
            ]
        )
        self._client.chat_post_message(channel_id=self._report_channel_id, text=text)
