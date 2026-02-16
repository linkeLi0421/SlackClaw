from __future__ import annotations

from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .slack_api import SlackWebClient

_SLACK_BLOCK_TEXT_LIMIT = 3000
_DETAILS_CHUNK_SIZE = 2800
_MAX_DETAIL_BLOCKS = 30


def _trim(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        chunks.append(text[cursor : cursor + chunk_size])
        cursor += chunk_size
    return chunks


def _status_label_and_icon(status: TaskStatus) -> tuple[str, str]:
    if status == TaskStatus.SUCCEEDED:
        return ("succeeded", "✅")
    if status == TaskStatus.FAILED:
        return ("failed", "❌")
    if status == TaskStatus.CANCELED:
        return ("canceled", "⏹️")
    if status == TaskStatus.ABORTED_ON_RESTART:
        return ("aborted_on_restart", "⚠️")
    if status == TaskStatus.WAITING_APPROVAL:
        return ("waiting_approval", "🕒")
    if status == TaskStatus.RUNNING:
        return ("running", "🏃")
    return (status.value, "ℹ️")


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
        status_label, status_icon = _status_label_and_icon(result.status)
        trimmed_input = _trim(task.command_text, self._input_max_chars)
        trimmed_summary = _trim(result.summary, self._summary_max_chars)
        trimmed_details = _trim(result.details, self._details_max_chars)

        fallback_text = "\n".join(
            [
                f"{status_icon} SlackClaw task {task.task_id}",
                f"source: {task.channel_id} @ {task.message_ts} by {task.trigger_user}",
                f"status: {status_label}",
                f"input: {trimmed_input}",
                f"summary: {trimmed_summary}",
                f"details: {trimmed_details}",
            ]
        )
        details_chunks = _chunk_text(trimmed_details, _DETAILS_CHUNK_SIZE) or ["<no output>"]
        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{status_icon} *SlackClaw task* `{task.task_id}`",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*Status:* `{status_label}`  |  *Source:* <#{task.channel_id}>"
                            f"  |  *Thread:* `{task.thread_ts}`  |  *User:* <@{task.trigger_user}>"
                        ),
                    }
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Input*\n```{trimmed_input}```",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _trim(f"*Summary*\n{trimmed_summary}", _SLACK_BLOCK_TEXT_LIMIT),
                },
            },
        ]

        for index, chunk in enumerate(details_chunks[:_MAX_DETAIL_BLOCKS]):
            title = "*Details*" if index == 0 else f"*Details (cont. {index + 1})*"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _trim(f"{title}\n{chunk}", _SLACK_BLOCK_TEXT_LIMIT),
                    },
                }
            )

        self._client.chat_post_message(
            channel_id=self._report_channel_id,
            text=fallback_text,
            blocks=blocks,
        )
