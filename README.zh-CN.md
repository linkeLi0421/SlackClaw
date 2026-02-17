# SlackClaw

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md) | [中文](README.zh-CN.md)

在 Slack 里输入命令，本地执行，并把结果回传到 Slack。

SlackClaw 是一个本地运行的 Agent：监听 Slack 命令频道，执行你发出的命令，并把结构化执行结果发送到报告频道。内置支持 Shell、Claude、Codex、Kimi。

## 工作流程

```
Slack 命令频道              你的本地机器               Slack 报告频道
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│ SHELL ls -la    │ ──▶ │ SlackClaw 拉取消息并 │ ──▶ │ 结构化执行报告      │
│ CLAUDE fix tests│     │ 在本地执行命令       │     │ 状态/输出/详情      │
│ CODEX refactor  │     │ 然后回传执行结果     │     │                     │
│ KIMI explain    │     │                      │     │                     │
└─────────────────┘     └──────────────────────┘     └─────────────────────┘
```

1. 你在 Slack 的命令频道发送消息
2. SlackClaw 识别命令（可选：先走表情审批）
3. 命令在本机执行
4. 报告发回 Slack 报告频道

## 快速开始

### 1. 创建 Slack App

访问 [api.slack.com/apps](https://api.slack.com/apps) 创建应用：

- 开启 **Socket Mode**，创建 `connections:write` 的 app-level token
- 开启 **Event Subscriptions**，订阅：`message.channels`、`message.groups`、`reaction_added`
- 添加 **OAuth Bot Scopes**：`chat:write`、`channels:history`、`groups:history`、`files:read`
- 安装到工作区
- 在命令频道和报告频道邀请机器人：`/invite @your-bot`

详细步骤见 `slack-app-setup.md`。

### 2. 构建二进制

macOS / Linux:
```bash
./scripts/build_app.sh
```

Windows (PowerShell):
```powershell
.\scripts\build_app.ps1
```

### 3. 运行打包程序

macOS:
```bash
./release/SlackClaw-macos-arm64 --setup
./release/SlackClaw-macos-arm64
```

Linux:
```bash
./release/SlackClaw-linux-x64 --setup
./release/SlackClaw-linux-x64
```

Windows (PowerShell):
```powershell
.\release\SlackClaw-windows-x64.exe --setup
.\release\SlackClaw-windows-x64.exe
```

首次运行会打开本地配置页面，填入 Slack token 和频道 ID 后即可启动。

## 在 Slack 里如何发命令

命令频道支持 4 种前缀：

| 前缀 | 作用 |
|------|------|
| `SHELL` | 执行 Shell 命令 |
| `CLAUDE` | 调用 Claude CLI |
| `CODEX` | 调用 Codex CLI |
| `KIMI` | 调用 Kimi CLI |

示例：
```
SHELL echo hello
SHELL pytest tests/ -v
CLAUDE review this repo and list top 3 issues
CODEX fix failing tests and summarize changes
KIMI how can I improve this codebase
```

也支持显式格式：
```
!do sh:echo hello
```

### 图片附件

你可以在同一条 Slack 消息里上传图片并附带命令：
```
KIMI describe this screenshot
CLAUDE what's wrong with this UI
```

- 每条任务最多 4 张图
- 单图最大 20MB
- 图片会下载到本地，并把路径注入给 Agent

### 审批流程

默认 `APPROVAL_MODE=reaction`：
- 非 allowlist 的 shell 命令需要审批
- 用 :white_check_mark: 通过，:x: 拒绝
- allowlist 命令（echo/ls/git/python 等）可直接执行

### 线程上下文

`CLAUDE` / `CODEX` / `KIMI` 使用线程上下文：
- 同一 Slack 线程内会复用上下文
- 不同线程可并行（`WORKER_PROCESSES>1`）

## 配置项说明

### 必填

| 变量 | 说明 |
|------|------|
| `SLACK_BOT_TOKEN` | Bot token（`xoxb-...`） |
| `SLACK_APP_TOKEN` | App token（`xapp-...`，socket 模式需要） |
| `COMMAND_CHANNEL_ID` | 命令频道 ID |
| `REPORT_CHANNEL_ID` | 报告频道 ID |

### 执行相关

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DRY_RUN` | `true` | 仅演练不执行；设为 `false` 才会真实执行 |
| `EXEC_TIMEOUT_SECONDS` | `120` | 单任务超时秒数 |
| `WORKER_PROCESSES` | `1` | 并行工作进程数 |
| `RUN_MODE` | `approve` | `approve` 需审批，`run` 直接执行 |

### 监听与触发

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LISTENER_MODE` | `socket` | `socket` 或 `poll` |
| `TRIGGER_MODE` | `prefix` | `prefix` 或 `mention` |
| `TRIGGER_PREFIX` | `!do` | 前缀触发词 |
| `BOT_USER_ID` | — | mention 模式必填 |
| `POLL_INTERVAL` | `3` | poll 模式轮询间隔 |

### 审批

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APPROVAL_MODE` | `reaction` | `reaction` 或 `none` |
| `APPROVE_REACTION` | `white_check_mark` | 通过表情 |
| `REJECT_REACTION` | `x` | 拒绝表情 |
| `SHELL_ALLOWLIST` | 常用命令集 | allowlist 命令跳过审批 |

### Agent 相关

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_WORKDIR` | — | 所有 Agent 的工作目录 |
| `KIMI_PERMISSION_MODE` | `yolo` | `yolo` / `auto` / `yes` / `default` |
| `CODEX_PERMISSION_MODE` | `full-auto` | `full-auto` / `dangerous` / `default` |
| `CODEX_SANDBOX_MODE` | `workspace-write` | `workspace-write` / `read-only` / `danger-full-access` |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | Claude `--permission-mode` 值 |
| `AGENT_RESPONSE_INSTRUCTION` | Markdown 提示词 | 结果格式指令；留空可关闭 |

### 报告长度

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REPORT_INPUT_MAX_CHARS` | `500` | Input 最大长度 |
| `REPORT_SUMMARY_MAX_CHARS` | `1200` | Summary 最大长度 |
| `REPORT_DETAILS_MAX_CHARS` | `4000` | Details 最大长度 |

## 打包说明

请在目标系统上构建对应二进制：

- macOS / Linux：`./scripts/build_app.sh`
- Windows：`.\scripts\build_app.ps1`

注意：
- PyInstaller 跨平台交叉编译（例如 macOS 直接出 Windows）通常不可靠
- 建议每个平台原生构建，或用 GitHub Actions 的发布流程（推送 `v*` tag）

输出命名：
- macOS / Linux：`release/SlackClaw-<os>-<arch>`
- Windows：`release/SlackClaw-windows-<arch>.exe`

首次运行时会弹出本地配置页，不需要手动准备 `.env`。

配置文件位置：
- macOS: `~/Library/Application Support/SlackClaw/config.json`
- Linux: `~/.config/SlackClaw/config.json`
- Windows: `%APPDATA%\SlackClaw\config.json`

可用参数：
- `--setup`：重新打开配置页
- `--show-config-path`：打印配置文件路径

## 开发模式（源码运行）

仅用于开发/调试，普通用户建议使用打包二进制。

```bash
cp .env.example .env
set -a; source .env; set +a; ./scripts/run_agent.sh
```

单轮运行：
```bash
./scripts/run_agent.sh --once
```

## 安全建议

- 先用 `DRY_RUN=true` 验证流程
- 生产环境建议保留 `APPROVAL_MODE=reaction`
- 设置合理超时，避免任务失控
- 不要在 Slack 里执行高风险命令（例如 `sudo`、`rm -rf`）

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 运行要求

- Python 3.11+
- `pip install -r requirements.txt`

## 许可证

MIT，见 `LICENSE`。

## 致谢

- https://github.com/korotovsky/slack-mcp-server
