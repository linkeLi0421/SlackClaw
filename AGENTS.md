# Repository Guidelines

## Project Structure & Module Organization
- `src/slackclaw/`: runtime code for polling, decisioning, execution, reporting, and app orchestration.
- `tests/`: `unittest`-based test suite (for example `test_decider.py`, `test_listener_incremental.py`).
- `scripts/run_agent.sh`: local entrypoint; sets `PYTHONPATH=src` then runs `python3 -m slackclaw.app`.
- `docs/`: design and implementation notes.

Keep new modules under `src/slackclaw/` and mirror test files in `tests/` as `test_<module>.py`.

## Build, Test, and Development Commands
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: run all tests.
- `./scripts/run_agent.sh --once`: run one poll/execute cycle for local validation.
- `./scripts/run_agent.sh`: start the long-running agent loop.

There is no packaging/build pipeline yet; focus on runnable source and passing tests.

## Coding Style & Naming Conventions
- Follow Python 3.11+ style with 4-space indentation and PEP 8 naming.
- Use type hints consistently (`list[str]`, dataclasses, explicit return types), matching existing code.
- Use `snake_case` for functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Keep functions focused and side-effect boundaries clear (for example, Slack I/O in `slack_api.py`, persistence in `state_store.py`).

No formatter/linter is configured in-repo yet; keep style consistent with current files.

## Testing Guidelines
- Framework: standard library `unittest`.
- Name tests `test_<behavior>` and files `test_<module>.py`.
- Add or update tests for each behavior change, especially around dedupe, locks, config validation, and reporting paths.
- Run full suite before opening a PR: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## Commit & Pull Request Guidelines
- Git history is currently empty, so no established commit pattern exists yet.
- Use imperative, scoped commit messages (recommended: Conventional Commits), e.g. `feat(decider): support lock key parsing`.
- PRs should include:
  - clear summary of behavior change,
  - linked issue/task (if any),
  - test evidence (command + result),
  - sample Slack/report output when behavior changes user-visible messaging.

## Security & Configuration Tips
- Configure secrets via environment variables (`SLACK_BOT_TOKEN`, channel IDs); never commit tokens.
- Keep local runtime artifacts (for example `state.db`, logs, `__pycache__/`) out of commits.
