from __future__ import annotations

from collections import deque

from .models import TaskSpec


class TaskQueue:
    def __init__(self) -> None:
        self._items: deque[TaskSpec] = deque()
        self._task_ids: set[str] = set()

    def enqueue(self, task: TaskSpec) -> bool:
        if task.task_id in self._task_ids:
            return False
        self._items.append(task)
        self._task_ids.add(task.task_id)
        return True

    def dequeue(self) -> TaskSpec | None:
        if not self._items:
            return None
        task = self._items.popleft()
        self._task_ids.discard(task.task_id)
        return task

    def snapshot(self) -> list[dict]:
        return [
            {
                "task_id": task.task_id,
                "command_text": task.command_text,
                "trigger_user": task.trigger_user,
                "channel_id": task.channel_id,
                "thread_ts": task.thread_ts,
            }
            for task in self._items
        ]

    def __len__(self) -> int:
        return len(self._items)
