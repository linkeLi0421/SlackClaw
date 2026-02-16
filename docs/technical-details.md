# SlackClaw Technical Details

## 1. System Scope
SlackClaw is a local Python Slack agent that monitors one command channel, decides whether messages should become tasks, executes tasks on the local machine, and reports outcomes to a separate report channel.

Current implemented scope includes:
- Poll and Socket Mode listeners.
- Explicit trigger parsing (`!do`, mention mode, and simple `SHELL/KIMI/CODEX/CLAUDE` commands).
- Queueing, deduplication, execution locks, and restart recovery.
- Optional reaction approval flow.
- Thread-scoped context/session handling for coding agents.
- Slack Block Kit reporting.
- Image attachment ingestion for command messages.

## 2. Runtime Architecture
Main runtime modules under `src/slackclaw/`:

| Module | Responsibility |
|---|---|
| `app.py` | Process lifecycle, orchestration loop, approvals, queue drain, attachment preparation |
| `config.py` | Environment parsing and validation |
| `listener.py` | Polling and Socket Mode event intake |
| `decider.py` | Trigger matching and task construction |
| `queue.py` | In-memory FIFO task queue with task-id dedupe |
| `executor.py` | `sh:`, `kimi:`, `codex:`, `claude:` execution and context/session glue |
| `reporter.py` | Slack report formatting + posting |
| `slack_api.py` | Slack Web API wrapper and private file download |
| `state_store.py` | SQLite persistence for checkpoints, tasks, locks, approvals, sessions, thread context |
| `models.py` | Dataclasses and status enums |

## 3. End-to-End Processing Flow
### 3.1 Intake and Decision
1. Listener gets incoming message events (poll batch or socket envelope).
2. Decider rejects unsupported message subtypes, except `file_share` (needed for uploads).
3. Trigger matching builds `TaskSpec` with:
   - `task_id` hash
   - channel/message/thread metadata
   - normalized `command_text`
   - `lock_key`

### 3.2 Task Persistence and Queueing
1. Message-level dedupe via `(channel_id, message_ts)` in `processed_messages`.
2. Task-level dedupe via `task_id` existence check.
3. Task row is persisted in `tasks` table.
4. Depending on mode:
   - `APPROVAL_MODE=reaction`: task goes to `waiting_approval`.
   - otherwise: task goes to `pending` and is enqueued.

### 3.3 Execution and Reporting
1. Queue drain marks task `running`.
2. Execution lock is acquired (`execution_locks.lock_key`).
3. Executor runs command with timeout control.
4. Result status is persisted.
5. Reporter posts structured report to report channel.
6. Lock is released.

## 4. Command and Trigger Semantics
### 4.1 Trigger Modes
- `TRIGGER_MODE=prefix`: requires `TRIGGER_PREFIX` (default `!do`).
- `TRIGGER_MODE=mention`: requires leading `<@BOT_USER_ID>` mention.

### 4.2 Simple Commands (No Prefix Required)
- `SHELL <cmd>` -> `sh:<cmd>`
- `KIMI <prompt>` -> `kimi:<prompt>`
- `CODEX <prompt>` -> `codex:<prompt>`
- `CLAUDE <prompt>` -> `claude:<prompt>`

### 4.3 Lock Key Resolution
- explicit: `lock:<name> <command>` -> `lock:<name>`
- shell path lock: `sh: cd <path> ...` -> `path:<path>`
- fallback: `global`

## 5. Image Attachment Pipeline
Image support is implemented in `app.py` and runs before queueing:

1. Parse `message.raw.files` and keep `mimetype` starting with `image/`.
2. Resolve `url_private_download`/`url_private`.
3. Download via authenticated bot token (`SlackWebClient.download_private_file`).
4. Save files to `./.slackclaw_attachments/<task_id>/`.
5. Store resulting paths in `TaskSpec.image_paths` and task payload.

Safety limits:
- max 4 image files per task.
- max 20 MB per image (both metadata pre-check and downloaded payload check).

Failure behavior:
- task is marked `failed`.
- report message explains likely fix (`files:read` scope and channel access).

## 6. Executor Details
### 6.1 Shell
- `sh:` runs via `subprocess.run(..., shell=True)`.
- If images exist, env vars are injected:
  - `SLACKCLAW_IMAGE_PATHS` (newline-delimited absolute paths)
  - `SLACKCLAW_IMAGE_COUNT`

### 6.2 KIMI
- Non-interactive: `kimi --quiet -S <session_id> -p <prompt>`.
- Session id is persisted per `(channel_id, thread_ts, agent)`.

### 6.3 CODEX
- Uses JSON mode: `codex exec --json`.
- Reuses existing session with `codex exec resume ... --json`.
- Parses JSON events and keeps assistant message content.
- Filters noisy stderr lines seen in codex rollout logs.

### 6.4 CLAUDE
- Non-interactive command execution via `claude code <prompt>`.

### 6.5 Shared Thread Context
For `KIMI/CODEX/CLAUDE`, prompts can include:
- prior thread context from `thread_context` table
- attached image paths
- optional response style instruction (`AGENT_RESPONSE_INSTRUCTION`)

Context is appended after successful runs and capped to avoid unbounded growth.

## 7. Data Model and SQLite Schema
Key tables in `state_store.py`:
- `checkpoint(key, value)`
- `processed_messages(channel_id, message_ts, processed_at)`
- `tasks(task_id, status, payload, created_at, updated_at)`
- `execution_locks(lock_key, task_id, acquired_at)`
- `task_approvals(task_id, source_message_ts, approval_message_ts, status, ...)`
- `agent_sessions(channel_id, thread_ts, agent, session_id, updated_at)`
- `thread_context(channel_id, thread_ts, context, updated_at)`

Durability notes:
- SQLite WAL mode enabled.
- restart recovery marks stale `running` tasks as `aborted_on_restart`.

## 8. Reporting Layer
`reporter.py` uses both:
- fallback plain text (for compatibility)
- Block Kit `mrkdwn` sections for richer Slack formatting

Report structure:
- header (`task_id`, status icon)
- context metadata (status/source/thread/user)
- input block
- summary block
- one or more details blocks (chunked/truncated to Slack limits)

Configurable output size controls:
- `REPORT_INPUT_MAX_CHARS`
- `REPORT_SUMMARY_MAX_CHARS`
- `REPORT_DETAILS_MAX_CHARS`

## 9. Configuration Overview
Required core:
- `SLACK_BOT_TOKEN`
- `COMMAND_CHANNEL_ID`
- `REPORT_CHANNEL_ID`

Runtime modes:
- `LISTENER_MODE=socket|poll`
- `RUN_MODE=approve|run`
- `APPROVAL_MODE=reaction|none`

Slack setup highlights:
- `SLACK_APP_TOKEN` required for Socket Mode.
- `files:read` required for attachment download.

Agent formatting control:
- `AGENT_RESPONSE_INSTRUCTION` (quoted in `.env` if it contains spaces)

## 10. Test Coverage Snapshot
The test suite (`tests/`) currently validates:
- config parsing and mode constraints
- decider command/subtype behavior
- listener incremental and socket handling
- approval lifecycle
- queue semantics
- state store persistence and lock behavior
- executor behavior across shell/kimi/codex/claude
- reporter truncation and block payload behavior
- image task ingestion and failure reporting

Run command:
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 11. Known Limitations
- Single-process, single-worker execution model.
- No built-in sandboxing for shell commands.
- No automatic cleanup policy for attachment directories yet.
- Agent output parsing is strongest for codex JSON mode; other CLIs may still emit noisy text.
- Non-interactive mode cannot pause for multi-turn tool prompts unless additional orchestration is added.

## 12. Operations Checklist
- If startup fails with `invalid_auth`, verify actual `.env` token value loaded by shell.
- If commands are not consumed, verify bot is invited to command channel and event scopes are enabled.
- If image tasks fail, verify `files:read` scope and reinstall the Slack app.
- If using poll mode, set `APPROVAL_MODE=none`.
