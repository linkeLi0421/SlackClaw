# Slack App Setup (Socket Mode + Events)

Follow these steps once per Slack app at [api.slack.com/apps](https://api.slack.com/apps).

## 1. Open Your App

Go to your app's settings page. If you don't have one yet, click **Create New App** → **From scratch**.

## 2. Enable Socket Mode

- Navigate to **Socket Mode** in the left sidebar.
- Toggle **Enable Socket Mode** on.

## 3. Create App-Level Token

- Go to **Basic Information** → **App-Level Tokens**.
- Click **Generate Token and Scopes**.
- Name it (e.g. `slackclaw-socket`) and add the `connections:write` scope.
- Copy the `xapp-...` token and save it as `SLACK_APP_TOKEN` in your `.env`.

## 4. Enable Event Subscriptions

- Go to **Event Subscriptions** in the left sidebar.
- Toggle **Enable Events** on.
- Under **Subscribe to bot events**, add:
  - `message.channels`
  - `message.groups`
  - `reaction_added`
- Click **Save Changes**.

## 5. Add OAuth Bot Scopes

- Go to **OAuth & Permissions** → **Bot Token Scopes**.
- Add the following scopes:
  - `chat:write` — post messages and reports
  - `channels:history` — read command channel messages
  - `groups:history` — read private channel messages
  - `files:read` — download image attachments

## 6. Install the App

- Click **Install to Workspace** (or **Reinstall to Workspace** if updating scopes).
- Copy the **Bot User OAuth Token** (`xoxb-...`) and save it as `SLACK_BOT_TOKEN` in your `.env`.

## 7. Invite the Bot to Channels

In Slack, run these commands in both your command and report channels:

```
/invite @your-bot-name
```

## 8. Find Your Channel IDs

Right-click a channel name in Slack → **View channel details** → copy the Channel ID at the bottom. Set these in `.env`:

```bash
COMMAND_CHANNEL_ID=C0123456789
REPORT_CHANNEL_ID=C0987654321
```

## 9. Start SlackClaw

```bash
set -a; source .env; set +a
./scripts/run_agent.sh
```

## What to Expect

After setup:

- New `SHELL ...` / `CLAUDE ...` / etc. messages in the command channel are detected automatically.
- With `APPROVAL_MODE=reaction`, non-allowlisted shell commands pause for emoji approval.
- React :white_check_mark: to approve or :x: to reject.
- Allowlisted commands and agent commands run according to `RUN_MODE`.
- Results appear as structured reports in the report channel.

## Troubleshooting

- **Bot not responding** — make sure the bot is invited to the command channel and `COMMAND_CHANNEL_ID` is correct.
- **"not_authed" errors** — verify `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set correctly.
- **No reactions detected** — ensure `reaction_added` is subscribed under Event Subscriptions, and the app is reinstalled after adding it.
- **Image commands failing** — confirm the `files:read` scope is added and the app is reinstalled.
