from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import TERMINAL_TASK_STATUSES, TaskRecord, TaskStatus


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        db_file = Path(db_path)
        if db_file.parent and not db_file.parent.exists():
            db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoint (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
              channel_id TEXT NOT NULL,
              message_ts TEXT NOT NULL,
              processed_at TEXT NOT NULL,
              PRIMARY KEY(channel_id, message_ts)
            );

            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_locks (
              lock_key TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              acquired_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_checkpoint(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM checkpoint WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_checkpoint(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO checkpoint(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def mark_message_processed(self, channel_id: str, message_ts: str) -> bool:
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages(channel_id, message_ts, processed_at)
            VALUES(?, ?, ?)
            """,
            (channel_id, message_ts, _utc_now()),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def is_message_processed(self, channel_id: str, message_ts: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1
            FROM processed_messages
            WHERE channel_id = ? AND message_ts = ?
            LIMIT 1
            """,
            (channel_id, message_ts),
        ).fetchone()
        return row is not None

    def upsert_task(self, task_id: str, status: TaskStatus, payload: dict | None = None) -> None:
        now = _utc_now()
        encoded_payload = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO tasks(task_id, status, created_at, updated_at, payload)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(task_id)
            DO UPDATE SET
              status = excluded.status,
              updated_at = excluded.updated_at,
              payload = excluded.payload
            """,
            (task_id, status.value, now, now, encoded_payload),
        )
        self._conn.commit()

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status.value, _utc_now(), task_id),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT task_id, status, payload, created_at, updated_at FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None

        payload_raw = row["payload"]
        parsed_payload = json.loads(payload_raw) if payload_raw else {}
        return TaskRecord(
            task_id=str(row["task_id"]),
            status=TaskStatus(str(row["status"])),
            payload=parsed_payload,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def task_exists(self, task_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
        return row is not None

    def is_task_terminal(self, task_id: str) -> bool:
        row = self._conn.execute("SELECT status FROM tasks WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return False
        try:
            status = TaskStatus(str(row["status"]))
        except ValueError:
            return False
        return status in TERMINAL_TASK_STATUSES

    def mark_running_tasks_aborted(self) -> int:
        cur = self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE status = ?
            """,
            (TaskStatus.ABORTED_ON_RESTART.value, _utc_now(), TaskStatus.RUNNING.value),
        )
        self._conn.commit()
        return cur.rowcount

    def acquire_execution_lock(self, lock_key: str, task_id: str) -> bool:
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO execution_locks(lock_key, task_id, acquired_at)
            VALUES(?, ?, ?)
            """,
            (lock_key, task_id, _utc_now()),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def release_execution_lock(self, lock_key: str, task_id: str) -> None:
        self._conn.execute(
            """
            DELETE FROM execution_locks
            WHERE lock_key = ? AND task_id = ?
            """,
            (lock_key, task_id),
        )
        self._conn.commit()
