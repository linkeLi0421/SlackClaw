# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SlackClaw is a Python bot that monitors a Slack channel for commands and dispatches them to shell or AI agents (Claude, Codex, Kimi). It posts structured reports back to Slack. Standalone binaries are built with PyInstaller for macOS, Linux, and Windows.

## Build & Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run full test suite
PYTHONPATH=src python3 -m unittest discover -s tests -v

# Run a single test file
PYTHONPATH=src python3 -m unittest tests.test_executor -v

# Run a single test method
PYTHONPATH=src python3 -m unittest tests.test_executor.TestExecutor.test_method_name -v

# Run app locally (sources .env)
./scripts/run_agent.sh

# Run one cycle for validation
./scripts/run_agent.sh --once

# Build binary (macOS/Linux)
./scripts/build_app.sh

# Build binary (Windows)
.\scripts\build_app.ps1
```

## Architecture

**Data flow:** Slack Message → Listener → Decider → Queue → Executor → Reporter → Slack

| Module | Responsibility |
|---|---|
| `app.py` | Main loop, approval flow, attachment handling, orchestration |
| `decider.py` | Parses trigger type (prefix/mention) and command kind (SHELL/KIMI/CODEX/CLAUDE/FILE) |
| `executor.py` | Runs shell commands via subprocess or invokes agent CLIs with thread context |
| `listener.py` | Receives Slack events (socket mode via WebSocket or HTTP polling) |
| `slack_api.py` | Raw HTTP wrapper around Slack Web API (uses urllib, no requests library); includes file upload via 3-step API |
| `state_store.py` | SQLite persistence (WAL mode) — tasks, approvals, sessions, thread context, checkpoints |
| `reporter.py` | Formats execution results into Slack Block Kit messages; auto-uploads large output as files |
| `dashboard.py` | Built-in local web dashboard (auto-refresh, stats, tasks, config) served via `ThreadingHTTPServer` |
| `queue.py` | In-memory FIFO task queue |
| `models.py` | Frozen dataclasses: TaskSpec, TaskStatus, SlackMessage, etc. |
| `packaging/launcher.py` | End-user entry point with browser-based setup UI |

**Execution modes:** Single-threaded (default) or multi-process via `ProcessPoolExecutor` (controlled by `WORKER_PROCESSES`).

**Approval flow:** Shell commands not on the allowlist require emoji reaction approval when `APPROVAL_MODE=reaction`. Allowlisted commands (echo, ls, git, python, pytest, etc.) and AI agents run immediately based on their `RUN_MODE` setting.

## Coding Conventions

- Python 3.11+, PEP 8, 4-space indentation
- Type hints everywhere (`tuple[str, ...]`, `list[dict]`, explicit returns)
- Immutable dataclasses: `@dataclass(frozen=True)`
- I/O boundaries: Slack I/O in `slack_api.py`/`listener.py`, persistence in `state_store.py`, execution side effects in `executor.py`
- Only external runtime dependency: `websocket-client`

## Testing

- Framework: `unittest` (standard library)
- File naming: `test_<module>.py` in `tests/`
- Test both success and failure paths
- Mock-based: subprocess, Slack API, and file I/O are mocked
- When changing Slack-visible output, update reporter tests
- When changing command parsing or task payload shape, update decider/app/state tests

## Configuration

Config loads from JSON file (`~/.config/SlackClaw/config.json` on Linux, platform-appropriate paths elsewhere), then falls back to environment variables, then defaults.

**Required:** `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` (socket mode), `COMMAND_CHANNEL_ID`, `REPORT_CHANNEL_ID`

**Required Bot OAuth Scopes:** `chat:write`, `channels:history`, `groups:history`, `files:read`, `files:write`. After adding or changing scopes, the app must be reinstalled to the workspace for the token to reflect the new scopes.

**Key defaults:** `DRY_RUN=true`, `LISTENER_MODE=socket`, `APPROVAL_MODE=reaction`, `WORKER_PROCESSES=1`, `EXEC_TIMEOUT_SECONDS=120`, `FILE_OUTPUT_THRESHOLD=4000`

## Commits

Conventional Commit subjects: `feat:`, `fix:`, etc. Keep commits scoped to one behavior change.
