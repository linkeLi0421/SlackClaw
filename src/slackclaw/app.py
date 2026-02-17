from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import shlex
import signal
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

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
ATTACHMENTS_BASE_DIR = ".slackclaw_attachments"
MAX_IMAGE_FILES_PER_TASK = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHELL_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\|)")
_SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_WRAPPER_CMDS = {"sudo", "command", "time", "nohup"}


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


def _extract_shell_command_names(command: str) -> list[str]:
    commands: list[str] = []
    for segment in _SHELL_SPLIT_RE.split(command):
        raw = segment.strip()
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if not parts:
            continue

        index = 0
        while index < len(parts) and _SHELL_ASSIGNMENT_RE.match(parts[index]):
            index += 1
        if index >= len(parts):
            continue

        cmd = parts[index]
        if cmd in _SHELL_WRAPPER_CMDS and index + 1 < len(parts):
            index += 1
            while index < len(parts) and _SHELL_ASSIGNMENT_RE.match(parts[index]):
                index += 1
            if index >= len(parts):
                continue
            cmd = parts[index]

        commands.append(Path(cmd).name.lower())
    return commands


def _disallowed_shell_commands(command: str, allowlist: tuple[str, ...]) -> list[str]:
    allow = {item.lower() for item in allowlist}
    seen: set[str] = set()
    disallowed: list[str] = []
    for cmd in _extract_shell_command_names(command):
        if cmd in allow or cmd in seen:
            continue
        seen.add(cmd)
        disallowed.append(cmd)
    return disallowed


def _sanitize_filename(name: str, fallback: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", (name or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def _guess_extension(filename: str, mimetype: str) -> str:
    suffix = Path(filename or "").suffix
    if suffix:
        return suffix
    normalized = (mimetype or "").strip().lower()
    if normalized == "image/png":
        return ".png"
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if normalized == "image/gif":
        return ".gif"
    if normalized == "image/webp":
        return ".webp"
    return ".img"


def _extract_image_entries(message: SlackMessage) -> list[dict]:
    raw_files = message.raw.get("files") or []
    if not isinstance(raw_files, list):
        return []
    image_entries: list[dict] = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        mimetype = str(raw.get("mimetype") or "").strip().lower()
        if not mimetype.startswith("image/"):
            continue
        url = str(raw.get("url_private_download") or raw.get("url_private") or "").strip()
        if not url:
            continue
        raw_size = raw.get("size")
        try:
            size_bytes = max(0, int(raw_size))
        except Exception:
            size_bytes = 0
        image_entries.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or ""),
                "mimetype": mimetype,
                "url": url,
                "size_bytes": size_bytes,
            }
        )
    return image_entries


def _materialize_task_images(task: TaskSpec, *, message: SlackMessage, client: SlackWebClient) -> TaskSpec:
    image_entries = _extract_image_entries(message)
    if not image_entries:
        return task

    output_dir = Path(ATTACHMENTS_BASE_DIR) / task.task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    image_paths: list[str] = []
    for index, entry in enumerate(image_entries[:MAX_IMAGE_FILES_PER_TASK], start=1):
        size_bytes = int(entry["size_bytes"])
        if size_bytes > MAX_IMAGE_BYTES:
            raise RuntimeError(
                f"image '{entry['name'] or entry['id'] or index}' exceeds {MAX_IMAGE_BYTES} bytes limit"
            )

        try:
            payload = client.download_private_file(str(entry["url"]))
        except Exception as exc:
            raise RuntimeError(
                f"failed to download image '{entry['name'] or entry['id'] or index}': {exc}"
            ) from exc

        if len(payload) > MAX_IMAGE_BYTES:
            raise RuntimeError(
                f"downloaded image '{entry['name'] or entry['id'] or index}' exceeds {MAX_IMAGE_BYTES} bytes limit"
            )

        filename = str(entry["name"] or entry["id"] or f"image_{index:02d}")
        stem = _sanitize_filename(Path(filename).stem, f"image_{index:02d}")
        ext = _guess_extension(filename, str(entry["mimetype"]))
        path = output_dir / f"{index:02d}_{stem}{ext}"
        path.write_bytes(payload)
        image_paths.append(str(path.resolve()))

    if not image_paths:
        return task
    return replace(task, image_paths=tuple(image_paths))


def _task_payload(task: TaskSpec) -> dict:
    return {
        "channel_id": task.channel_id,
        "message_ts": task.message_ts,
        "thread_ts": task.thread_ts,
        "trigger_user": task.trigger_user,
        "trigger_text": task.trigger_text,
        "command_text": task.command_text,
        "lock_key": task.lock_key,
        "image_paths": list(task.image_paths),
    }


def _task_from_payload(task_id: str, payload: dict) -> TaskSpec | None:
    try:
        message_ts = str(payload["message_ts"])
        thread_ts = str(payload.get("thread_ts") or message_ts)
        raw_image_paths = payload.get("image_paths") or []
        image_paths: tuple[str, ...] = ()
        if isinstance(raw_image_paths, list):
            normalized = [str(path).strip() for path in raw_image_paths if str(path).strip()]
            image_paths = tuple(normalized)
        return TaskSpec(
            task_id=task_id,
            channel_id=str(payload["channel_id"]),
            message_ts=message_ts,
            thread_ts=thread_ts,
            trigger_user=str(payload["trigger_user"]),
            trigger_text=str(payload["trigger_text"]),
            command_text=str(payload["command_text"]),
            lock_key=str(payload["lock_key"]),
            image_paths=image_paths,
        )
    except Exception:
        return None


def _approval_plan_text(config: AppConfig, task: TaskSpec, *, reason: str | None = None) -> str:
    lines = [
        f"SlackClaw plan for task `{task.task_id}`",
        f"command: `{task.command_text}`",
        f"lock: `{task.lock_key}`",
    ]
    if reason:
        lines.append(f"reason: {reason}")
    if task.image_paths:
        lines.append(f"images: {len(task.image_paths)} downloaded attachment(s)")
    lines.append(
        f"React with :{config.approve_reaction}: to run or :{config.reject_reaction}: to cancel."
    )
    return "\n".join(lines)


def _request_reaction_approval(
    config: AppConfig,
    *,
    task: TaskSpec,
    reason: str | None,
    store: StateStore,
    client: SlackWebClient,
    reporter: Reporter,
) -> bool:
    plan_text = _approval_plan_text(config, task, reason=reason)
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

    try:
        task = _materialize_task_images(task, message=message, client=client)
    except Exception as exc:
        store.upsert_task(task.task_id, TaskStatus.FAILED, payload=_task_payload(task))
        result = TaskExecutionResult(
            status=TaskStatus.FAILED,
            summary=f"failed to prepare image attachment(s): {exc}",
            details="Ensure files:read scope is granted and uploaded files are accessible to the bot.",
        )
        try:
            reporter.report(task, result)
        except Exception as report_exc:
            _event("report_failed", task_id=task.task_id, error=str(report_exc))
        _event("task_image_prepare_failed", task_id=task.task_id, error=str(exc))
        return 0

    if task.image_paths:
        _event("task_images_prepared", task_id=task.task_id, image_count=len(task.image_paths))

    approval_reason: str | None = None
    if config.approval_mode == "reaction" and task.command_text.startswith("sh:"):
        shell_command = task.command_text[3:].strip()
        disallowed = _disallowed_shell_commands(shell_command, config.shell_allowlist)
        if disallowed:
            approval_reason = "non-allowlisted shell command(s): " + ", ".join(disallowed)

    if approval_reason is not None:
        store.upsert_task(task.task_id, TaskStatus.WAITING_APPROVAL, payload=_task_payload(task))
        _request_reaction_approval(
            config,
            task=task,
            reason=approval_reason,
            store=store,
            client=client,
            reporter=reporter,
        )
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


def _execute_task_in_worker(
    task: TaskSpec,
    state_db_path: str,
    dry_run: bool,
    timeout_seconds: int,
    response_format_instruction: str,
) -> TaskExecutionResult:
    worker_store = StateStore(state_db_path)
    try:
        executor = TaskExecutor(
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
            response_format_instruction=response_format_instruction,
        )
        return executor.execute(task, store=worker_store)
    finally:
        worker_store.close()


def _finish_task(
    *,
    task: TaskSpec,
    result: TaskExecutionResult,
    store: StateStore,
    reporter: Reporter,
) -> None:
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


def _drain_queue(
    queue: TaskQueue,
    *,
    config: AppConfig,
    store: StateStore,
    executor: TaskExecutor,
    reporter: Reporter,
    process_pool: cf.ProcessPoolExecutor | None,
) -> int:
    handled = 0
    deferred: list[TaskSpec] = []
    in_flight: list[tuple[TaskSpec, cf.Future[TaskExecutionResult]]] = []

    while True:
        task = queue.dequeue()
        if task is None:
            break

        if not store.transition_task_status(task.task_id, TaskStatus.PENDING, TaskStatus.RUNNING):
            # Another process may have already claimed/finished this task.
            continue

        handled += 1
        _event(
            "task_started",
            task_id=task.task_id,
            channel_id=task.channel_id,
            ts=task.message_ts,
            thread_ts=task.thread_ts,
            lock_key=task.lock_key,
        )

        if not store.acquire_execution_lock(task.lock_key, task.task_id):
            # Keep pending for a retry in a later cycle instead of failing fast.
            store.update_task_status(task.task_id, TaskStatus.PENDING)
            deferred.append(task)
            _event(
                "task_deferred_lock_busy",
                task_id=task.task_id,
                lock_key=task.lock_key,
            )
            continue

        if process_pool is None:
            try:
                try:
                    result = executor.execute(task, store=store)
                except Exception as exc:  # pragma: no cover - defensive boundary
                    result = TaskExecutionResult(
                        status=TaskStatus.FAILED,
                        summary=f"executor raised error: {exc}",
                        details=traceback.format_exc(limit=5),
                    )

                _finish_task(task=task, result=result, store=store, reporter=reporter)
            finally:
                store.release_execution_lock(task.lock_key, task.task_id)
            continue

        try:
            future = process_pool.submit(
                _execute_task_in_worker,
                task,
                config.state_db_path,
                config.dry_run,
                config.exec_timeout_seconds,
                config.agent_response_instruction,
            )
        except Exception as exc:
            _event(
                "process_pool_submit_failed",
                task_id=task.task_id,
                error=str(exc),
                fallback="inline",
            )
            process_pool = None
            try:
                try:
                    result = executor.execute(task, store=store)
                except Exception as inline_exc:  # pragma: no cover - defensive boundary
                    result = TaskExecutionResult(
                        status=TaskStatus.FAILED,
                        summary=f"executor raised error: {inline_exc}",
                        details=traceback.format_exc(limit=5),
                    )
                _finish_task(task=task, result=result, store=store, reporter=reporter)
            finally:
                store.release_execution_lock(task.lock_key, task.task_id)
            continue
        in_flight.append((task, future))

    for task, future in in_flight:
        try:
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - process-pool failures depend on env
                result = TaskExecutionResult(
                    status=TaskStatus.FAILED,
                    summary=f"worker process execution failed: {exc}",
                    details="task execution did not return a valid result",
                )
            _finish_task(task=task, result=result, store=store, reporter=reporter)
        finally:
            store.release_execution_lock(task.lock_key, task.task_id)

    for task in deferred:
        queue.enqueue(task)
    return handled


def run(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

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
    process_pool: cf.ProcessPoolExecutor | None = None
    if config.worker_processes > 1:
        process_pool = cf.ProcessPoolExecutor(max_workers=config.worker_processes)
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
        worker_processes=config.worker_processes,
        shell_allowlist_count=len(config.shell_allowlist),
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

            handled = _drain_queue(
                queue,
                config=config,
                store=store,
                executor=executor,
                reporter=reporter,
                process_pool=process_pool,
            )
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
        if process_pool is not None:
            process_pool.shutdown(wait=True)
        store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
