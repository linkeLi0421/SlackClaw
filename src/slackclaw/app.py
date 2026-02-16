from __future__ import annotations

import argparse
import json
import signal
import traceback
import sys
import time

from .config import ConfigError, load_config
from .decider import decide_message
from .executor import TaskExecutor
from .listener import SlackChannelListener
from .models import TaskExecutionResult, TaskSpec, TaskStatus
from .queue import TaskQueue
from .reporter import Reporter
from .slack_api import SlackApiError, SlackWebClient
from .state_store import StateStore


CHECKPOINT_KEY_PREFIX = "last_ts"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SlackClaw local agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll + execution cycle and exit",
    )
    return parser.parse_args(argv)


def _event(name: str, **fields) -> None:
    payload = {"event": name, **fields}
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def _checkpoint_key(channel_id: str) -> str:
    return f"{CHECKPOINT_KEY_PREFIX}:{channel_id}"


def _task_payload(task: TaskSpec) -> dict:
    return {
        "channel_id": task.channel_id,
        "message_ts": task.message_ts,
        "trigger_user": task.trigger_user,
        "trigger_text": task.trigger_text,
        "command_text": task.command_text,
        "lock_key": task.lock_key,
    }


def _drain_queue(
    queue: TaskQueue,
    *,
    store: StateStore,
    executor: TaskExecutor,
    reporter: Reporter,
) -> int:
    handled = 0
    while True:
        task = queue.dequeue()
        if task is None:
            return handled

        handled += 1
        store.update_task_status(task.task_id, TaskStatus.RUNNING)
        _event(
            "task_started",
            task_id=task.task_id,
            channel_id=task.channel_id,
            ts=task.message_ts,
            lock_key=task.lock_key,
        )

        if not store.acquire_execution_lock(task.lock_key, task.task_id):
            result = TaskExecutionResult(
                status=TaskStatus.FAILED,
                summary=f"execution lock busy: {task.lock_key}",
                details="task skipped because another execution holds the same lock",
            )
            store.update_task_status(task.task_id, result.status)
            try:
                reporter.report(task, result)
                report_ok = True
            except Exception as exc:
                report_ok = False
                _event("report_failed", task_id=task.task_id, error=str(exc))

            _event(
                "task_finished",
                task_id=task.task_id,
                status=result.status.value,
                report_ok=report_ok,
                summary=result.summary,
            )
            continue

        try:
            try:
                result = executor.execute(task)
            except Exception as exc:  # pragma: no cover - defensive boundary
                result = TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary=f"executor raised error: {exc}",
                    details=traceback.format_exc(limit=5),
                )

            store.update_task_status(task.task_id, result.status)

            try:
                reporter.report(task, result)
                report_ok = True
            except Exception as exc:
                report_ok = False
                _event("report_failed", task_id=task.task_id, error=str(exc))

            _event(
                "task_finished",
                task_id=task.task_id,
                status=result.status.value,
                report_ok=report_ok,
                summary=result.summary,
            )
        finally:
            store.release_execution_lock(task.lock_key, task.task_id)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    store = StateStore(config.state_db_path)
    store.init_schema()
    recovered = store.mark_running_tasks_aborted()

    client = SlackWebClient(config.slack_bot_token)
    try:
        auth = client.auth_test()
    except SlackApiError as exc:
        print(f"Slack auth failed: {exc}", file=sys.stderr)
        store.close()
        return 3
    except Exception as exc:
        print(f"Slack auth request failed: {exc}", file=sys.stderr)
        store.close()
        return 3
    listener = SlackChannelListener(
        client,
        channel_id=config.command_channel_id,
        batch_size=config.poll_batch_size,
    )
    queue = TaskQueue()
    executor = TaskExecutor(dry_run=config.dry_run, timeout_seconds=config.exec_timeout_seconds)
    reporter = Reporter(
        report_channel_id=config.report_channel_id,
        desktop_report_script=config.desktop_report_script,
    )

    _event(
        "startup",
        command_channel_id=config.command_channel_id,
        report_channel_id=config.report_channel_id,
        poll_interval=config.poll_interval,
        poll_batch_size=config.poll_batch_size,
        trigger_mode=config.trigger_mode,
        dry_run=config.dry_run,
        recovered_tasks=recovered,
        auth_user_id=auth.get("user_id"),
        auth_team=auth.get("team"),
    )

    should_exit = False

    def _handle_signal(signum, _frame) -> None:
        nonlocal should_exit
        should_exit = True
        _event("signal", signal=signum)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    checkpoint_key = _checkpoint_key(config.command_channel_id)
    last_ts = store.get_checkpoint(checkpoint_key)

    try:
        while not should_exit:
            cycle_started = time.time()
            enqueued = 0
            polled = 0

            try:
                poll_result = listener.poll(last_ts=last_ts)
                polled = len(poll_result.messages)
                for message in poll_result.messages:
                    if not store.mark_message_processed(message.channel_id, message.ts):
                        continue

                    decision = decide_message(config, message)
                    if not decision.should_run or decision.task is None:
                        continue

                    task = decision.task
                    if store.task_exists(task.task_id):
                        continue

                    store.upsert_task(task.task_id, TaskStatus.PENDING, payload=_task_payload(task))
                    if queue.enqueue(task):
                        enqueued += 1

                if poll_result.newest_ts:
                    last_ts = poll_result.newest_ts
                    store.set_checkpoint(checkpoint_key, last_ts)
            except Exception as exc:
                _event("poll_error", error=str(exc))

            handled = _drain_queue(queue, store=store, executor=executor, reporter=reporter)
            elapsed_ms = int((time.time() - cycle_started) * 1000)
            _event(
                "cycle_finished",
                polled=polled,
                enqueued=enqueued,
                handled=handled,
                queue_size=len(queue),
                elapsed_ms=elapsed_ms,
                last_ts=last_ts,
            )

            if args.once:
                break
            time.sleep(config.poll_interval)
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
