# SlackClaw

Local resident Slack agent for command-channel task execution with dedupe, locking, reporting, and optional reaction approval.

## Features (M1-M5)
- M1: config loader, SQLite state store, startup entrypoint.
- M2: polling listener, explicit trigger decider, dedupe, FIFO queue.
- M3: executor + reporter integration with standardized result messages.
- M4: execution locks, dry-run default, restart recovery, 429 retry.
- M5: Socket Mode listener + plan-then-approve flow via reactions.

## Requirements
- Python 3.11+
- `pip install -r requirements.txt`

## Configuration
Required:
- `SLACK_BOT_TOKEN`
- `COMMAND_CHANNEL_ID`
- `REPORT_CHANNEL_ID`

Socket Mode (default):
- `SLACK_APP_TOKEN`

Common optional:
- `LISTENER_MODE=socket|poll` (default `socket`)
- `APPROVAL_MODE=reaction|none` (default `reaction`)
- `APPROVE_REACTION=white_check_mark`
- `REJECT_REACTION=x`
- `TRIGGER_MODE=prefix|mention`
- `TRIGGER_PREFIX=!do`
- `BOT_USER_ID=` (required when `TRIGGER_MODE=mention`)
- `STATE_DB_PATH=./state.db`
- `POLL_INTERVAL=3`
- `POLL_BATCH_SIZE=100`
- `DRY_RUN=true`
- `EXEC_TIMEOUT_SECONDS=120`

## Use `.env`
1. Copy template:
```bash
cp .env.example .env
```
2. Edit `.env` with real values.
3. Load env vars into current shell and run:
```bash
set -a
source .env
set +a
./scripts/run_agent.sh --once
```

One-line variant:
```bash
set -a; source .env; set +a; ./scripts/run_agent.sh
```

Notes:
- `LISTENER_MODE=poll` requires `APPROVAL_MODE=none`.
- `BOT_USER_ID` is required only when `TRIGGER_MODE=mention`.

## Run
- Single cycle:
```bash
./scripts/run_agent.sh --once
```

- Continuous:
```bash
./scripts/run_agent.sh
```

## Test
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
