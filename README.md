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
- `RUN_MODE=approve|run` (default `approve`)
- `REPORT_INPUT_MAX_CHARS=500` (default)
- `REPORT_SUMMARY_MAX_CHARS=1200` (default)
- `REPORT_DETAILS_MAX_CHARS=4000` (default)

## Slack App Setup (Socket Mode + Events)
Follow this once per Slack app at `https://api.slack.com/apps`:

1. Open your app.
2. Enable Socket Mode:
   - Go to `Socket Mode`.
   - Toggle `Enable Socket Mode` on.
3. Create app-level token for Socket Mode:
   - Go to `Basic Information` -> `App-Level Tokens`.
   - Create token with scope `connections:write`.
   - Put this `xapp-...` token in `.env` as `SLACK_APP_TOKEN`.
4. Enable Event Subscriptions:
   - Go to `Event Subscriptions`.
   - Toggle `Enable Events` on.
   - In `Subscribe to bot events`, add:
     - `message.channels`
     - `message.groups`
     - `reaction_added`
5. Add OAuth bot scopes:
   - Go to `OAuth & Permissions` -> `Bot Token Scopes`.
   - Add:
     - `chat:write`
     - `channels:history`
     - `groups:history`
6. Install or reinstall app to workspace:
   - Click `Install to Workspace` or `Reinstall`.
7. Invite bot to channels:
   - In Slack command and report channels, run `/invite @your-bot`.
8. Restart agent:
   - `./scripts/run_agent.sh`

Expected behavior after setup:
- New `!do ...` messages in `COMMAND_CHANNEL_ID` are detected.
- With `APPROVAL_MODE=reaction`, agent posts a plan in thread.
- React `:white_check_mark:` to run, `:x:` to cancel.

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
- `RUN_MODE=run` forces no approval (executes immediately).
- Use `REPORT_*_MAX_CHARS` to control Slack report truncation lengths.

## Run
- Single cycle:
```bash
./scripts/run_agent.sh --once
```

- Continuous:
```bash
./scripts/run_agent.sh
```

## Use With Claude/Codex/Kimi CLI
SlackClaw executes shell commands only when command text starts with `sh:` and `DRY_RUN=false`.

1. Set runtime mode for real execution:
```bash
DRY_RUN=false
EXEC_TIMEOUT_SECONDS=1800
RUN_MODE=run
```
2. Restart agent:
```bash
./scripts/run_agent.sh
```
3. Send simple commands in Slack command channel:
```text
SHELL echo hello-from-slackclaw
KIMI how to improve this repo
CODEX fix failing tests and summarize changes
CLAUDE review this repository and list top risks
```

SlackClaw maps these automatically:
- `SHELL <cmd>` -> `sh:<cmd>`
- `KIMI <prompt>` -> non-interactive `kimi --quiet -p "<prompt>"`
- `CODEX <prompt>` -> non-interactive `codex exec --skip-git-repo-check -C <cwd> "<prompt>"`
- `CLAUDE <prompt>` -> `claude code "<prompt>"`

Thread-scoped behavior for `KIMI`/`CODEX`/`CLAUDE`:
- Session/context key = Slack thread root (`thread_ts`).
- Replies in the same Slack thread reuse agent state for that thread.
- Shared thread context is persisted and injected into later agent prompts in that thread.

Codex output behavior:
- Uses `codex exec --json` internally.
- Reporter keeps assistant response text and filters noisy CLI metadata/log lines.

You can still use the advanced explicit form:
```text
!do sh:echo hello
```

Codex CLI example (non-interactive):
```text
!do sh:cd /absolute/path/to/repo && codex exec --skip-git-repo-check -C /absolute/path/to/repo "Run tests, fix failures, and summarize changed files"
```

Kimi CLI example (non-interactive):
```text
!do sh:cd /absolute/path/to/repo && kimi --quiet -p "Analyze this repo and propose a refactor plan"
```

Claude Code example (CLI syntax may vary by install):
```text
!do sh:cd /absolute/path/to/repo && claude code "Read README.md and propose 3 concrete fixes"
```

If your Claude command differs, run `claude --help` locally and update the Slack command accordingly.

## Guardrails (Recommended)
This project can execute local shell commands. Keep these protections enabled:

1. Keep approval on:
   - `APPROVAL_MODE=reaction`
   - Only react `:white_check_mark:` after reviewing the plan message.
2. Limit command surface:
   - Prefer calling reviewed wrapper scripts instead of arbitrary shell payloads.
   - Example: `!do sh:/absolute/path/to/repo/scripts/run_claude_task.sh`.
3. Keep execution scoped:
   - Use explicit `cd /path/to/repo` in commands.
   - Use lock prefix for shared repos: `!do lock:repo-a sh:cd /repo-a && ...`.
4. Start safe, then expand:
   - Test with `DRY_RUN=true` first.
   - Switch to `DRY_RUN=false` only after command flow is verified.
5. Set timeouts and monitor reports:
   - Keep `EXEC_TIMEOUT_SECONDS` reasonable.
   - Watch report channel for failures, lock conflicts, or unexpected output.
6. Never run privileged or destructive commands from Slack:
   - Avoid `sudo`, filesystem wipes, credential dumps, and production-impacting commands.

## Test
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
