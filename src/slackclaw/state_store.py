from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ApprovalStatus,
    MemoryCategory,
    MemoryRecord,
    MemoryScope,
    TERMINAL_TASK_STATUSES,
    TaskApprovalRecord,
    TaskRecord,
    TaskStatus,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

            CREATE TABLE IF NOT EXISTS memories (
              memory_id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              scope_key TEXT NOT NULL,
              category TEXT NOT NULL,
              content TEXT NOT NULL,
              file_path TEXT NOT NULL,
              source_task_id TEXT NOT NULL DEFAULT '',
              source_agent TEXT NOT NULL DEFAULT '',
              access_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_accessed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_scope
              ON memories(scope, scope_key);
            """
        )
        # FTS5 virtual table must be created outside executescript because
        # CREATE VIRTUAL TABLE is not supported inside executescript on all
        # SQLite builds.  Use separate execute calls guarded by IF NOT EXISTS.
        try:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    content=memories,
                    content_rowid=rowid,
                    tokenize='porter unicode61'
                )
                """
            )
        except Exception:
            # FTS5 not available – search will fall back to LIKE queries
            pass

        # Triggers to keep FTS in sync (silently skip if FTS5 table missing)
        for trigger_sql in [
            """
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
                INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
            END
            """,
        ]:
            try:
                self._conn.execute(trigger_sql)
            except Exception:
                pass

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

    def list_tasks(self, limit: int = 200) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT task_id, status, payload, created_at, updated_at "
            "FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[TaskRecord] = []
        for row in rows:
            payload_raw = row["payload"]
            parsed_payload = json.loads(payload_raw) if payload_raw else {}
            result.append(
                TaskRecord(
                    task_id=str(row["task_id"]),
                    status=TaskStatus(str(row["status"])),
                    payload=parsed_payload,
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return result

    def store_task_result(self, task_id: str, summary: str, details: str, report_text: str = "") -> None:
        row = self._conn.execute(
            "SELECT payload FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return
        payload = json.loads(row["payload"]) if row["payload"] else {}
        payload["result_summary"] = summary
        payload["result_details"] = details
        if report_text:
            payload["report_text"] = report_text
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self._conn.execute(
            "UPDATE tasks SET payload = ?, updated_at = ? WHERE task_id = ?",
            (encoded, _utc_now(), task_id),
        )
        self._conn.commit()

    def count_tasks_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["cnt"]) for row in rows}

    def list_agent_sessions(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT channel_id, thread_ts, agent, session_id, updated_at "
            "FROM agent_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "channel_id": str(row["channel_id"]),
                "thread_ts": str(row["thread_ts"]),
                "agent": str(row["agent"]),
                "session_id": str(row["session_id"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def list_task_approvals(self, limit: int = 200) -> list[TaskApprovalRecord]:
        rows = self._conn.execute(
            "SELECT task_id, channel_id, source_message_ts, approval_message_ts, "
            "approve_reaction, reject_reaction, status, decided_by, "
            "decision_reaction, created_at, updated_at "
            "FROM task_approvals ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._approval_record_from_row(row) for row in rows]

    def list_execution_locks(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT lock_key, task_id, acquired_at FROM execution_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return [
            {
                "lock_key": str(row["lock_key"]),
                "task_id": str(row["task_id"]),
                "acquired_at": str(row["acquired_at"]),
            }
            for row in rows
        ]

    def list_checkpoints(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT key, value FROM checkpoint ORDER BY key"
        ).fetchall()
        return [{"key": str(row["key"]), "value": str(row["value"])} for row in rows]

    # --- Memory methods ---

    def upsert_memory(self, record: MemoryRecord) -> None:
        now = _utc_now()
        self._conn.execute(
            """
            INSERT INTO memories(
                memory_id, scope, scope_key, category, content, file_path,
                source_task_id, source_agent, access_count,
                created_at, updated_at, last_accessed_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                content = excluded.content,
                file_path = excluded.file_path,
                category = excluded.category,
                source_task_id = excluded.source_task_id,
                source_agent = excluded.source_agent,
                updated_at = excluded.updated_at
            """,
            (
                record.memory_id,
                record.scope.value,
                record.scope_key,
                record.category.value,
                record.content,
                record.file_path,
                record.source_task_id,
                record.source_agent,
                record.access_count,
                record.created_at or now,
                now,
                record.last_accessed_at or now,
            ),
        )
        self._conn.commit()

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return self._memory_record_from_row(row)

    def list_memories(self, scope_key: str, *, limit: int = 50) -> list[MemoryRecord]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE scope_key = ? ORDER BY updated_at DESC LIMIT ?",
            (scope_key, limit),
        ).fetchall()
        return [self._memory_record_from_row(r) for r in rows]

    def search_memories(
        self,
        *,
        scope_keys: list[str],
        query: str,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        if not scope_keys or not query.strip():
            return []

        # Try FTS5 first
        try:
            placeholders = ",".join("?" for _ in scope_keys)
            sql = f"""
                SELECT m.*
                FROM memories_fts mf
                JOIN memories m ON mf.rowid = m.rowid
                WHERE mf.memories_fts MATCH ?
                  AND m.scope_key IN ({placeholders})
                ORDER BY mf.rank
                LIMIT ?
            """
            params: list = [query] + scope_keys + [limit]
            rows = self._conn.execute(sql, params).fetchall()
            return [self._memory_record_from_row(r) for r in rows]
        except Exception:
            # FTS5 not available – fall back to LIKE
            placeholders = ",".join("?" for _ in scope_keys)
            like_pattern = f"%{query}%"
            sql = f"""
                SELECT * FROM memories
                WHERE scope_key IN ({placeholders})
                  AND content LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params = scope_keys + [like_pattern, limit]
            rows = self._conn.execute(sql, params).fetchall()
            return [self._memory_record_from_row(r) for r in rows]

    def touch_memory_access(self, memory_id: str) -> None:
        self._conn.execute(
            """
            UPDATE memories
            SET access_count = access_count + 1,
                last_accessed_at = ?
            WHERE memory_id = ?
            """,
            (_utc_now(), memory_id),
        )
        self._conn.commit()

    def delete_memory(self, memory_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_memories_by_scope(self, scope_key: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM memories WHERE scope_key = ?", (scope_key,)
        )
        self._conn.commit()
        return cur.rowcount

    def purge_old_memories(self, retention_days: int) -> list[str]:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        rows = self._conn.execute(
            "SELECT memory_id, file_path FROM memories WHERE last_accessed_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [str(r["memory_id"]) for r in rows]
        paths = [str(r["file_path"]) for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"DELETE FROM memories WHERE memory_id IN ({placeholders})", ids
            )
            self._conn.commit()
        return paths

    def count_memories_by_scope(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT scope_key, COUNT(*) as cnt FROM memories GROUP BY scope_key"
        ).fetchall()
        return {str(r["scope_key"]): int(r["cnt"]) for r in rows}

    def count_memories_total(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        return int(row["cnt"]) if row else 0

    def list_all_memories(self, *, limit: int = 200) -> list[MemoryRecord]:
        rows = self._conn.execute(
            "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._memory_record_from_row(r) for r in rows]

    def list_thread_contexts(self, *, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT channel_id, thread_ts, context, updated_at FROM thread_context ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "channel_id": str(r["channel_id"]),
                "thread_ts": str(r["thread_ts"]),
                "context": str(r["context"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    @staticmethod
    def _memory_record_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            scope=MemoryScope(str(row["scope"])),
            scope_key=str(row["scope_key"]),
            category=MemoryCategory(str(row["category"])),
            content=str(row["content"]),
            file_path=str(row["file_path"]),
            source_task_id=str(row["source_task_id"]),
            source_agent=str(row["source_agent"]),
            access_count=int(row["access_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_accessed_at=str(row["last_accessed_at"]),
        )

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
