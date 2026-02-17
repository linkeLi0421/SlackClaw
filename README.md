# SlackClaw

Local resident Slack agent for command-channel task execution with dedupe, locking, reporting, and optional reaction approval.

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
- `SHELL_ALLOWLIST=...` (comma/space separated shell commands allowed to run without reaction approval)
- `TRIGGER_MODE=prefix|mention`
- `TRIGGER_PREFIX=!do`
- `BOT_USER_ID=` (required when `TRIGGER_MODE=mention`)
- `STATE_DB_PATH=./state.db`
- `POLL_INTERVAL=3`
- `POLL_BATCH_SIZE=100`
- `DRY_RUN=true`
- `EXEC_TIMEOUT_SECONDS=120`
- `WORKER_PROCESSES=1` (set `>1` to execute multiple tasks in parallel)
- `RUN_MODE=approve|run` (default `approve`)
- `REPORT_INPUT_MAX_CHARS=500` (default)
- `REPORT_SUMMARY_MAX_CHARS=1200` (default)
- `REPORT_DETAILS_MAX_CHARS=4000` (default)
- `AGENT_RESPONSE_INSTRUCTION=...` (optional prompt style for KIMI/CODEX/CLAUDE; empty disables)

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
     - `files:read` (required for image attachments)
6. Install or reinstall app to workspace:
   - Click `Install to Workspace` or `Reinstall`.
7. Invite bot to channels:
   - In Slack command and report channels, run `/invite @your-bot`.
8. Restart agent:
   - `./scripts/run_agent.sh`

Expected behavior after setup:
- New `!do ...` messages in `COMMAND_CHANNEL_ID` are detected.
- With `APPROVAL_MODE=reaction`, only non-allowlisted `sh:` commands require reaction approval.
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
- To use shell allowlist approvals, keep `RUN_MODE=approve` and `APPROVAL_MODE=reaction`.
- `WORKER_PROCESSES>1` enables multi-process task execution.
- Use `REPORT_*_MAX_CHARS` to control Slack report truncation lengths.
- Reports are posted with Slack Block Kit + mrkdwn for cleaner formatting.
- If `AGENT_RESPONSE_INSTRUCTION` contains spaces, wrap it in quotes in `.env`.

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
WORKER_PROCESSES=4
RUN_MODE=approve
APPROVAL_MODE=reaction
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

Image command flow:
- Upload image(s) with a command in the same message (for example: `KIMI describe this screenshot`).
- SlackClaw downloads image attachments to `./.slackclaw_attachments/<task_id>/`.
- For `KIMI`/`CODEX`/`CLAUDE`, local image file paths are appended to the prompt.
- For `SHELL`, image paths are exposed via env vars:
  - `SLACKCLAW_IMAGE_PATHS` (newline-delimited)
  - `SLACKCLAW_IMAGE_COUNT`
- Limits:
  - up to 4 image files per task
  - 20 MB max per image (pre-check and post-download check)

Shell example that reads image env vars:
```text
SHELL python3 -c "import os; print(os.getenv('SLACKCLAW_IMAGE_COUNT')); print(os.getenv('SLACKCLAW_IMAGE_PATHS'))"
```

Troubleshooting image tasks:
- Ensure `files:read` is granted, then reinstall the Slack app.
- Confirm the bot is invited to the command channel where the image was posted.
- Include command text in the same message as the upload; image-only messages are ignored.

SlackClaw maps these automatically:
- `SHELL <cmd>` -> `sh:<cmd>`
- `KIMI <prompt>` -> non-interactive `kimi --quiet -p "<prompt>"`
- `CODEX <prompt>` -> non-interactive `codex exec --skip-git-repo-check -C <cwd> "<prompt>"`
- `CLAUDE <prompt>` -> non-interactive `claude -p "<prompt>"`

Shell allowlist approval behavior:
- With `APPROVAL_MODE=reaction`, only non-allowlisted `sh:` commands pause for emoji approval.
- Allowlisted shell commands run immediately.
- Example disallowed command requiring approval: `SHELL rm -rf /tmp/example`.

Thread-scoped behavior for `KIMI`/`CODEX`/`CLAUDE`:
- Session/context key = Slack thread root (`thread_ts`).
- Replies in the same Slack thread reuse agent state for that thread.
- Shared thread context is persisted and injected into later agent prompts in that thread.
- Default lock key for these agent commands is thread-scoped (`thread:<thread_ts>`), so different Slack threads can run in parallel when `WORKER_PROCESSES>1`.

Codex output behavior:
- Uses `codex exec --json` internally.
- Reporter keeps assistant response text and filters noisy CLI metadata/log lines.

Formatting behavior:
- Slack reports are rendered as structured Block Kit sections (`Input`, `Summary`, `Details`).
- KIMI/CODEX/CLAUDE prompts include `AGENT_RESPONSE_INSTRUCTION` to encourage cleaner markdown output.

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
!do sh:cd /absolute/path/to/repo && claude -p "Read README.md and propose 3 concrete fixes"
```

If your Claude command differs, run `claude --help` locally and update the Slack command accordingly.

## Guardrails (Recommended)
This project can execute local shell commands. Keep these protections enabled:

1. Keep approval on:
   - `APPROVAL_MODE=reaction`
   - Only react `:white_check_mark:` after reviewing the plan message.
2. Start safe, then expand:
   - Test with `DRY_RUN=true` first.
   - Switch to `DRY_RUN=false` only after command flow is verified.
3. Set timeouts and monitor reports:
   - Keep `EXEC_TIMEOUT_SECONDS` reasonable.
   - Watch report channel for failures, lock conflicts, or unexpected output.
4. Never run privileged or destructive commands from Slack:
   - Avoid `sudo`, filesystem wipes, credential dumps, and production-impacting commands.

## Packaging
Build a single-file app binary:

```bash
./scripts/build_app.sh
```

Build outputs:
- `dist/SlackClaw` (or `dist/SlackClaw.exe` on Windows)
- `release/SlackClaw-<os>-<arch>` (single executable file)

First run (no `.env` required for packaged usage):
1. start the binary
2. SlackClaw opens a local setup page in your browser
3. save tokens/channel IDs once, then SlackClaw starts immediately

Setup UI behavior:
- validates `SLACK_BOT_TOKEN` (and `SLACK_APP_TOKEN` when `LISTENER_MODE=socket`) before saving
- keeps the setup page open if token validation fails, so you can correct values

Config/runtime location:
- macOS: `~/Library/Application Support/SlackClaw/`
- Linux: `~/.config/SlackClaw/` (or `$XDG_CONFIG_HOME/SlackClaw/`)
- Windows: `%APPDATA%\SlackClaw\`
- saved config: `config.json`
- default state db: `state.db` in the same folder

Useful binary flags:
- `--setup` force-open setup UI again
- `--show-config-path` print config file path and exit

GitHub Actions build pipeline:
- workflow: `.github/workflows/build-binaries.yml`
- manual trigger: `workflow_dispatch`
- tag trigger: push tag like `v0.1.0`
- outputs single binaries for Linux/macOS/Windows and attaches them to GitHub Releases on tag builds

## Test
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
