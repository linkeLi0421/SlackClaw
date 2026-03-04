from __future__ import annotations

import os
import tempfile

from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .slack_api import SlackWebClient

_SLACK_BLOCK_TEXT_LIMIT = 3000
_DETAILS_CHUNK_SIZE = 2800
_MAX_DETAIL_BLOCKS = 30
_DEFAULT_FILE_OUTPUT_THRESHOLD = 4000


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
        file_output_threshold: int = _DEFAULT_FILE_OUTPUT_THRESHOLD,
    ) -> None:
        self._client = client
        self._report_channel_id = report_channel_id
        self._input_max_chars = input_max_chars
        self._summary_max_chars = summary_max_chars
        self._details_max_chars = details_max_chars
        self._file_output_threshold = file_output_threshold

    def report(self, task: TaskSpec, result: TaskExecutionResult) -> str:
        auto_file_path: str | None = None
        report_text = ""
        try:
            # Auto-file: when details exceed threshold, write to temp file for upload
            use_auto_file = (
                len(result.details) > self._file_output_threshold
                and self._file_output_threshold > 0
            )
            if use_auto_file:
                fd, auto_file_path = tempfile.mkstemp(
                    suffix=".txt", prefix=f"slackclaw_{task.task_id}_"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(result.details)
                except Exception:
                    os.close(fd)
                    raise

            status_label, status_icon = _status_label_and_icon(result.status)
            trimmed_input = _trim(task.command_text, self._input_max_chars)
            trimmed_summary = _trim(result.summary, self._summary_max_chars)

            if use_auto_file:
                # Show a short preview inline; full output goes in the file
                preview = _trim(result.details, 500)
                trimmed_details = f"{preview}\n\n_(full output uploaded as file)_"
            else:
                trimmed_details = _trim(result.details, self._details_max_chars)

            report_text = "\n".join(
                [
                    f"{status_icon} status: {status_label}",
                    f"summary: {trimmed_summary}",
                    f"details: {trimmed_details}",
                ]
            )
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

            # Upload auto-filed output
            if use_auto_file and auto_file_path:
                try:
                    self._client.upload_file(
                        channel_id=self._report_channel_id,
                        filepath=auto_file_path,
                        filename=f"{task.task_id}_output.txt",
                        title=f"Full output for task {task.task_id}",
                        initial_comment=f"Full output for task `{task.task_id}` (exceeded {self._file_output_threshold} chars)",
                    )
                except Exception as upload_exc:
                    self._client.chat_post_message(
                        channel_id=self._report_channel_id,
                        text=f"⚠️ Failed to upload full output file for task `{task.task_id}`: {upload_exc}",
                    )

            # Upload explicit file from result
            if result.upload_file_path:
                try:
                    self._client.upload_file(
                        channel_id=self._report_channel_id,
                        filepath=result.upload_file_path,
                        filename=os.path.basename(result.upload_file_path),
                        title=os.path.basename(result.upload_file_path),
                    )
                except Exception as upload_exc:
                    self._client.chat_post_message(
                        channel_id=self._report_channel_id,
                        text=f"⚠️ Failed to upload file `{result.upload_file_path}` for task `{task.task_id}`: {upload_exc}",
                    )
        finally:
            if auto_file_path:
                try:
                    os.unlink(auto_file_path)
                except OSError:
                    pass
        return report_text
