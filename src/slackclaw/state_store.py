from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import ApprovalStatus, TERMINAL_TASK_STATUSES, TaskApprovalRecord, TaskRecord, TaskStatus


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        db_file = Path(db_path)
        if db_file.parent and not db_file.parent.exists():
            db_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=30)
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

            CREATE TABLE IF NOT EXISTS agent_sessions (
              channel_id TEXT NOT NULL,
              thread_ts TEXT NOT NULL,
              agent TEXT NOT NULL,
              session_id TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(channel_id, thread_ts, agent)
            );

            CREATE TABLE IF NOT EXISTS thread_context (
              channel_id TEXT NOT NULL,
              thread_ts TEXT NOT NULL,
              context TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(channel_id, thread_ts)
            );

            CREATE TABLE IF NOT EXISTS task_approvals (
              task_id TEXT PRIMARY KEY,
              channel_id TEXT NOT NULL,
              source_message_ts TEXT NOT NULL,
              approval_message_ts TEXT NOT NULL,
              approve_reaction TEXT NOT NULL,
              reject_reaction TEXT NOT NULL,
              status TEXT NOT NULL,
              decided_by TEXT NOT NULL,
              decision_reaction TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_task_approvals_lookup
              ON task_approvals(channel_id, source_message_ts, approval_message_ts, status);

            CREATE INDEX IF NOT EXISTS idx_agent_sessions_lookup
              ON agent_sessions(channel_id, thread_ts, agent);
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

    def transition_task_status(self, task_id: str, from_status: TaskStatus, to_status: TaskStatus) -> bool:
        cur = self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND status = ?
            """,
            (to_status.value, _utc_now(), task_id, from_status.value),
        )
        self._conn.commit()
        return cur.rowcount == 1

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

    def get_agent_session(self, channel_id: str, thread_ts: str, agent: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT session_id
            FROM agent_sessions
            WHERE channel_id = ? AND thread_ts = ? AND agent = ?
            LIMIT 1
            """,
            (channel_id, thread_ts, agent),
        ).fetchone()
        if row is None:
            return None
        return str(row["session_id"])

    def upsert_agent_session(self, channel_id: str, thread_ts: str, agent: str, session_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO agent_sessions(channel_id, thread_ts, agent, session_id, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, thread_ts, agent)
            DO UPDATE SET
              session_id = excluded.session_id,
              updated_at = excluded.updated_at
            """,
            (channel_id, thread_ts, agent, session_id, _utc_now()),
        )
        self._conn.commit()

    def get_thread_context(self, channel_id: str, thread_ts: str) -> str:
        row = self._conn.execute(
            """
            SELECT context
            FROM thread_context
            WHERE channel_id = ? AND thread_ts = ?
            LIMIT 1
            """,
            (channel_id, thread_ts),
        ).fetchone()
        if row is None:
            return ""
        return str(row["context"] or "")

    def upsert_thread_context(self, channel_id: str, thread_ts: str, context: str) -> None:
        self._conn.execute(
            """
            INSERT INTO thread_context(channel_id, thread_ts, context, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(channel_id, thread_ts)
            DO UPDATE SET
              context = excluded.context,
              updated_at = excluded.updated_at
            """,
            (channel_id, thread_ts, context, _utc_now()),
        )
        self._conn.commit()

    def upsert_task_approval(
        self,
        *,
        task_id: str,
        channel_id: str,
        source_message_ts: str,
        approval_message_ts: str,
        approve_reaction: str,
        reject_reaction: str,
        status: ApprovalStatus = ApprovalStatus.PENDING,
    ) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO task_approvals(
              task_id,
              channel_id,
              source_message_ts,
              approval_message_ts,
              approve_reaction,
              reject_reaction,
              status,
              decided_by,
              decision_reaction,
              created_at,
              updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              channel_id = excluded.channel_id,
              source_message_ts = excluded.source_message_ts,
              approval_message_ts = excluded.approval_message_ts,
              approve_reaction = excluded.approve_reaction,
              reject_reaction = excluded.reject_reaction,
              status = excluded.status,
              decided_by = excluded.decided_by,
              decision_reaction = excluded.decision_reaction,
              updated_at = excluded.updated_at
            """,
            (
                task_id,
                channel_id,
                source_message_ts,
                approval_message_ts,
                approve_reaction,
                reject_reaction,
                status.value,
                "",
                "",
                now,
                now,
            ),
        )
        self._conn.commit()

    def get_task_approval(self, task_id: str) -> TaskApprovalRecord | None:
        row = self._conn.execute(
            """
            SELECT
              task_id,
              channel_id,
              source_message_ts,
              approval_message_ts,
              approve_reaction,
              reject_reaction,
              status,
              decided_by,
              decision_reaction,
              created_at,
              updated_at
            FROM task_approvals
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._approval_record_from_row(row)

    def get_pending_approval_for_message(self, channel_id: str, message_ts: str) -> TaskApprovalRecord | None:
        row = self._conn.execute(
            """
            SELECT
              task_id,
              channel_id,
              source_message_ts,
              approval_message_ts,
              approve_reaction,
              reject_reaction,
              status,
              decided_by,
              decision_reaction,
              created_at,
              updated_at
            FROM task_approvals
            WHERE channel_id = ?
              AND status = ?
              AND (source_message_ts = ? OR approval_message_ts = ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (channel_id, ApprovalStatus.PENDING.value, message_ts, message_ts),
        ).fetchone()
        if row is None:
            return None
        return self._approval_record_from_row(row)

    def resolve_task_approval(
        self,
        *,
        task_id: str,
        status: ApprovalStatus,
        decided_by: str,
        decision_reaction: str,
    ) -> bool:
        cur = self._conn.execute(
            """
            UPDATE task_approvals
            SET
              status = ?,
              decided_by = ?,
              decision_reaction = ?,
              updated_at = ?
            WHERE task_id = ? AND status = ?
            """,
            (
                status.value,
                decided_by,
                decision_reaction,
                _utc_now(),
                task_id,
                ApprovalStatus.PENDING.value,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def _approval_record_from_row(row: sqlite3.Row) -> TaskApprovalRecord:
        return TaskApprovalRecord(
            task_id=str(row["task_id"]),
            channel_id=str(row["channel_id"]),
            source_message_ts=str(row["source_message_ts"]),
            approval_message_ts=str(row["approval_message_ts"]),
            approve_reaction=str(row["approve_reaction"]),
            reject_reaction=str(row["reject_reaction"]),
            status=ApprovalStatus(str(row["status"])),
            decided_by=str(row["decided_by"]),
            decision_reaction=str(row["decision_reaction"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
