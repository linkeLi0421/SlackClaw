from __future__ import annotations

import unittest

from slackclaw.models import TaskSpec
from slackclaw.queue import TaskQueue


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        channel_id="C111",
        message_ts="1.1",
        trigger_user="U1",
        trigger_text="!do x",
        command_text="x",
        lock_key="global",
    )


class QueueTests(unittest.TestCase):
    def test_enqueue_dedupes_by_task_id(self) -> None:
        queue = TaskQueue()
        self.assertTrue(queue.enqueue(_task("task-1")))
        self.assertFalse(queue.enqueue(_task("task-1")))
        self.assertEqual(len(queue), 1)

    def test_dequeue_is_fifo(self) -> None:
        queue = TaskQueue()
        queue.enqueue(_task("task-1"))
        queue.enqueue(_task("task-2"))

        first = queue.dequeue()
        second = queue.dequeue()
        third = queue.dequeue()

        assert first is not None
        assert second is not None
        self.assertEqual(first.task_id, "task-1")
        self.assertEqual(second.task_id, "task-2")
        self.assertIsNone(third)


if __name__ == "__main__":
    unittest.main()
