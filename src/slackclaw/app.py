from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback

from .config import AppConfig, ConfigError, load_config
from .decider import decide_message
from .executor import TaskExecutor
from .listener import SlackChannelListener, SlackSocketModeListener
from .models import ApprovalStatus, SlackMessage, SlackReaction, TaskExecutionResult, TaskSpec, TaskStatus
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
        "thread_ts": task.thread_ts,
        "trigger_user": task.trigger_user,
        "trigger_text": task.trigger_text,
        "command_text": task.command_text,
        "lock_key": task.lock_key,
    }


def _task_from_payload(task_id: str, payload: dict) -> TaskSpec | None:
    try:
        message_ts = str(payload["message_ts"])
        thread_ts = str(payload.get("thread_ts") or message_ts)
        return TaskSpec(
            task_id=task_id,
            channel_id=str(payload["channel_id"]),
            message_ts=message_ts,
            thread_ts=thread_ts,
            trigger_user=str(payload["trigger_user"]),
            trigger_text=str(payload["trigger_text"]),
            command_text=str(payload["command_text"]),
            lock_key=str(payload["lock_key"]),
        )
    except Exception:
        return None


def _approval_plan_text(config: AppConfig, task: TaskSpec) -> str:
    return "\n".join(
        [
            f"SlackClaw plan for task `{task.task_id}`",
            f"command: `{task.command_text}`",
            f"lock: `{task.lock_key}`",
            (
                f"React with :{config.approve_reaction}: to run "
                f"or :{config.reject_reaction}: to cancel."
            ),
        ]
    )


def _request_reaction_approval(
    config: AppConfig,
    *,
    task: TaskSpec,
    store: StateStore,
    client: SlackWebClient,
    reporter: Reporter,
) -> bool:
    plan_text = _approval_plan_text(config, task)
    try:
        posted = client.chat_post_message(
            channel_id=task.channel_id,
            thread_ts=task.thread_ts,
            text=plan_text,
        )
        approval_message_ts = str(posted.get("ts") or task.message_ts)
        store.upsert_task_approval(
            task_id=task.task_id,
            channel_id=task.channel_id,
            source_message_ts=task.message_ts,
            approval_message_ts=approval_message_ts,
            approve_reaction=config.approve_reaction,
            reject_reaction=config.reject_reaction,
        )
        _event(
            "task_waiting_approval",
            task_id=task.task_id,
            source_ts=task.message_ts,
            approval_ts=approval_message_ts,
            approve_reaction=config.approve_reaction,
            reject_reaction=config.reject_reaction,
        )
        return True
    except Exception as exc:
        store.update_task_status(task.task_id, TaskStatus.FAILED)
        result = TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"failed to request approval: {exc}",
            details=plan_text,
        )
        try:
            reporter.report(task, result)
        except Exception as report_exc:
            _event("report_failed", task_id=task.task_id, error=str(report_exc))
        _event("approval_request_failed", task_id=task.task_id, error=str(exc))
        return False


def _process_command_message(
    config: AppConfig,
    message: SlackMessage,
    *,
    store: StateStore,
    queue: TaskQueue,
    client: SlackWebClient,
    reporter: Reporter,
) -> int:
    if not store.mark_message_processed(message.channel_id, message.ts):
        return 0

    decision = decide_message(config, message)
    if not decision.should_run or decision.task is None:
        return 0

    task = decision.task
    if store.task_exists(task.task_id):
        return 0

    if config.approval_mode == "reaction":
        store.upsert_task(task.task_id, TaskStatus.WAITING_APPROVAL, payload=_task_payload(task))
        _request_reaction_approval(config, task=task, store=store, client=client, reporter=reporter)
        return 0

    store.upsert_task(task.task_id, TaskStatus.PENDING, payload=_task_payload(task))
    return 1 if queue.enqueue(task) else 0


def _process_reaction_event(
    reaction: SlackReaction,
    *,
    store: StateStore,
    queue: TaskQueue,
    reporter: Reporter,
) -> int:
    approval = store.get_pending_approval_for_message(reaction.channel_id, reaction.message_ts)
    if approval is None:
        return 0

    normalized_reaction = reaction.reaction.strip().strip(":")
    decision_status: ApprovalStatus | None = None
    if normalized_reaction == approval.approve_reaction:
        decision_status = ApprovalStatus.APPROVED
    elif normalized_reaction == approval.reject_reaction:
        decision_status = ApprovalStatus.REJECTED
    if decision_status is None:
        return 0

    if not store.resolve_task_approval(
        task_id=approval.task_id,
        status=decision_status,
        decided_by=reaction.user,
        decision_reaction=normalized_reaction,
    ):
        return 0

    record = store.get_task(approval.task_id)
    if record is None:
        return 0
    task = _task_from_payload(approval.task_id, record.payload)
    if task is None:
        _event("approval_payload_invalid", task_id=approval.task_id)
        return 0

    if decision_status == ApprovalStatus.APPROVED:
        store.update_task_status(task.task_id, TaskStatus.PENDING)
        enqueued = queue.enqueue(task)
        _event(
            "task_approved",
            task_id=task.task_id,
            approved_by=reaction.user,
            reaction=normalized_reaction,
            enqueued=enqueued,
        )
        return 1 if enqueued else 0

    store.update_task_status(task.task_id, TaskStatus.CANCELED)
    result = TaskExecutionResult(
        status=TaskStatus.CANCELED,
        summary=f"task canceled by :{normalized_reaction}: from {reaction.user}",
        details="approval rejected before execution",
    )
    try:
        reporter.report(task, result)
    except Exception as exc:
        _event("report_failed", task_id=task.task_id, error=str(exc))
    _event(
        "task_canceled",
        task_id=task.task_id,
        canceled_by=reaction.user,
        reaction=normalized_reaction,
    )
    return 0


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
            thread_ts=task.thread_ts,
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
                result = executor.execute(task, store=store)
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
    polling_listener: SlackChannelListener | None = None
    socket_listener: SlackSocketModeListener | None = None
    try:
        if config.listener_mode == "poll":
            polling_listener = SlackChannelListener(
                client,
                channel_id=config.command_channel_id,
                batch_size=config.poll_batch_size,
            )
        else:
            socket_listener = SlackSocketModeListener(
                client,
                app_token=config.slack_app_token,
                command_channel_id=config.command_channel_id,
            )
    except Exception as exc:
        print(f"Listener init failed: {exc}", file=sys.stderr)
        store.close()
        return 4
    queue = TaskQueue()
    executor = TaskExecutor(
        dry_run=config.dry_run,
        timeout_seconds=config.exec_timeout_seconds,
        response_format_instruction=config.agent_response_instruction,
    )
    reporter = Reporter(
        report_channel_id=config.report_channel_id,
        client=client,
        input_max_chars=config.report_input_max_chars,
        summary_max_chars=config.report_summary_max_chars,
        details_max_chars=config.report_details_max_chars,
    )

    _event(
        "startup",
        command_channel_id=config.command_channel_id,
        report_channel_id=config.report_channel_id,
        listener_mode=config.listener_mode,
        poll_interval=config.poll_interval,
        poll_batch_size=config.poll_batch_size,
        socket_read_timeout_seconds=config.socket_read_timeout_seconds,
        trigger_mode=config.trigger_mode,
        run_mode=config.run_mode,
        approval_mode=config.approval_mode,
        approve_reaction=config.approve_reaction,
        reject_reaction=config.reject_reaction,
        dry_run=config.dry_run,
        agent_response_instruction_enabled=bool(config.agent_response_instruction),
        report_input_max_chars=config.report_input_max_chars,
        report_summary_max_chars=config.report_summary_max_chars,
        report_details_max_chars=config.report_details_max_chars,
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
    last_ts = store.get_checkpoint(checkpoint_key) if config.listener_mode == "poll" else None

    try:
        while not should_exit:
            cycle_started = time.time()
            enqueued = 0
            polled = 0
            reactions = 0
            approved = 0
            incoming_messages: list[SlackMessage] = []
            incoming_reactions: list[SlackReaction] = []

            try:
                if config.listener_mode == "poll":
                    if polling_listener is None:
                        raise RuntimeError("polling listener is not initialized")
                    poll_result = polling_listener.poll(last_ts=last_ts)
                    incoming_messages = poll_result.messages
                    polled = len(incoming_messages)
                    if poll_result.newest_ts:
                        last_ts = poll_result.newest_ts
                        store.set_checkpoint(checkpoint_key, last_ts)
                else:
                    if socket_listener is None:
                        raise RuntimeError("socket listener is not initialized")
                    socket_batch = socket_listener.receive(timeout_seconds=config.socket_read_timeout_seconds)
                    incoming_messages = socket_batch.messages
                    incoming_reactions = socket_batch.reactions
                    polled = len(incoming_messages)
                    reactions = len(incoming_reactions)
            except Exception as exc:
                _event("listen_error", error=str(exc))

            for message in incoming_messages:
                enqueued += _process_command_message(
                    config,
                    message,
                    store=store,
                    queue=queue,
                    client=client,
                    reporter=reporter,
                )

            for reaction in incoming_reactions:
                approved += _process_reaction_event(
                    reaction,
                    store=store,
                    queue=queue,
                    reporter=reporter,
                )
            enqueued += approved

            handled = _drain_queue(queue, store=store, executor=executor, reporter=reporter)
            elapsed_ms = int((time.time() - cycle_started) * 1000)
            _event(
                "cycle_finished",
                polled=polled,
                reactions=reactions,
                enqueued=enqueued,
                approved=approved,
                handled=handled,
                queue_size=len(queue),
                elapsed_ms=elapsed_ms,
                last_ts=last_ts,
            )

            if args.once:
                break
            if config.listener_mode == "poll":
                time.sleep(config.poll_interval)
    finally:
        if socket_listener is not None:
            socket_listener.close()
        store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
