# Repository Guidelines

## Project Structure & Module Organization
- `src/slackclaw/` contains runtime modules:
  - `app.py` orchestration loop, approval flow, attachment prep
  - `decider.py` trigger parsing (`SHELL`, `KIMI`, `CODEX`, `CLAUDE`, prefix/mention)
  - `executor.py` command execution and agent integrations
  - `listener.py`, `slack_api.py`, `state_store.py`, `reporter.py`
- `tests/` mirrors runtime behavior with `unittest` files like `test_executor.py`, `test_app_images.py`.
- `scripts/run_agent.sh` is the local entrypoint.
- Runtime artifacts (`state.db*`, `.slackclaw_attachments/`, logs) are local-only and must stay untracked.

## Build, Test, and Development Commands
- `pip install -r requirements.txt`: install dependencies.
- `./scripts/run_agent.sh --once`: run one cycle for validation.
- `./scripts/run_agent.sh`: run continuously.
- `PYTHONPATH=src python3 -m unittest discover -s tests -v`: run full test suite.

## Coding Style & Naming Conventions
- Target Python 3.11+, 4-space indentation, PEP 8 naming.
- Use type hints consistently (`tuple[str, ...]`, `list[dict]`, explicit returns).
- Keep dataclasses immutable where practical (`@dataclass(frozen=True)` pattern used across models).
- Keep I/O boundaries clear:
  - Slack HTTP/WebSocket logic in `slack_api.py`/`listener.py`
  - persistence in `state_store.py`
  - execution side effects in `executor.py`

## Testing Guidelines
- Use standard `unittest`; file names must be `test_<module>.py`.
- Add tests for each behavior change and failure path, not just success paths.
- When changing Slack-visible output, update reporter tests.
- When changing command parsing or task payload shape, update decider/app/state tests.

## Commit & Pull Request Guidelines
- Follow existing history style: Conventional Commit-like subjects, e.g. `feat: ...`, `fix: ...`.
- Keep commits scoped to one behavior change.
- PRs should include:
  - behavior summary
  - config/scope changes (for example `files:read`)
  - test command and result
  - sample Slack output when user-visible formatting changes

## Security & Configuration Tips
- Never commit tokens or real `.env` values.
- For attachment workflows, ensure Slack bot scope `files:read` is present.
- Treat `sh:` commands as privileged; prefer `APPROVAL_MODE=reaction` outside local experiments.
