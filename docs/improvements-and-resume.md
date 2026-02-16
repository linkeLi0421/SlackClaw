# Improvement Backlog and Resume Positioning

## 1. Improvement Backlog (Prioritized)

### P0: Safety and Reliability
1. Add command allowlist mode for `sh:` execution.
2. Add attachment retention cleanup job (TTL + max total disk usage).
3. Add per-command timeout overrides (`EXEC_TIMEOUT_SECONDS` is currently global).
4. Add stronger Slack API retry strategy (currently basic retry for HTTP 429 in API wrapper).

### P1: Product Experience
1. Add `APPROVAL_MODE=smart`:
   - always approve required for `SHELL`
   - auto-run for `KIMI/CODEX/CLAUDE`.
2. Add thread UX helpers:
   - explicit `/status <task_id>`
   - task list command for recent failures.
3. Improve output controls:
   - env flags for details chunk count
   - optional compact report mode.

### P2: Maintainability
1. Add lint/format/type CI pipeline (`ruff`, `black`, `mypy`, GitHub Actions).
2. Add packaging and release metadata for easier deployment.
3. Split larger orchestration logic in `app.py` into smaller service objects.
4. Add migration versioning for SQLite schema evolution.

## 2. Suggested Next Metrics to Track
To improve engineering confidence and resume value, track:
- task success rate and failure categories
- p50/p95 task latency
- approval-to-execution latency
- duplicate-task prevention count
- attachment processing failures by cause

A lightweight approach is to emit structured JSON logs and aggregate later.

## 3. Can This Project Go on Your Resume?
Yes. This is resume-worthy if you frame it as a production-style systems project, not just a script.

Strong signals already present:
- event-driven + polling Slack integrations
- robust task-state persistence and dedupe
- concurrency guard via execution locks
- approval workflow design
- CLI-agent orchestration with thread-scoped context
- test coverage across core modules and failure paths

## 4. Resume Bullet Drafts
Choose 2-4 bullets depending on space:

- Built a local Slack operations agent in Python that converts channel messages into executable tasks with deduplication, queueing, and SQLite-backed task state.
- Implemented dual intake modes (Slack Socket Mode and polling) with reaction-based approval workflow and restart-safe task recovery.
- Integrated CLI coding agents (Codex, Kimi, Claude) with thread-scoped context/session persistence and structured Slack reporting via Block Kit.
- Added secure image-attachment ingestion (authenticated download, validation limits, local materialization) and propagated attachment context into shell and agent execution paths.
- Designed and maintained a unit/integration test suite covering config validation, listener behavior, execution semantics, persistence, and reporting.

## 5. Project Title Ideas for Resume
- Slack-Based Local Task Orchestration Agent
- Slack-to-CLI Automation Platform (Python)
- Stateful Slack Operations Agent with Approval Workflow

## 6. Interview Talking Points
Use this story arc:
1. Problem: reduce friction from manual command execution and fragmented reporting.
2. Architecture: listener -> decider -> queue -> executor -> reporter with persistent state.
3. Hard parts: idempotency, lock design, non-interactive agent integration, noisy CLI output handling.
4. Tradeoffs: single-worker simplicity vs scalability.
5. Next iteration: policy engine, metrics dashboard, and least-privilege execution model.
