from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

_APP_NAME = "SlackClaw"
_SETUP_DEFAULTS = {
    "LISTENER_MODE": "socket",
    "TRIGGER_MODE": "prefix",
    "TRIGGER_PREFIX": "!do",
    "STATE_DB_PATH": "./state.db",
    "AGENT_WORKDIR": "",
    "KIMI_PERMISSION_MODE": "yolo",
    "CODEX_PERMISSION_MODE": "full-auto",
    "CODEX_SANDBOX_MODE": "workspace-write",
    "CLAUDE_PERMISSION_MODE": "acceptEdits",
    "DRY_RUN": "true",
    "RUN_MODE": "approve",
    "APPROVAL_MODE": "reaction",
    "WORKER_PROCESSES": "1",
    "EXEC_TIMEOUT_SECONDS": "120",
    "SHELL_ALLOWLIST": (
        "echo,printf,pwd,ls,cat,head,tail,wc,grep,rg,find,sed,awk,cut,sort,uniq,date,"
        "whoami,uname,env,true,false,cd,python,python3,pip,pip3,pytest,node,npm,yarn,pnpm,"
        "go,cargo,make,git,bash,sh,zsh"
    ),
}
_REQUIRED_KEYS = ("SLACK_BOT_TOKEN", "COMMAND_CHANNEL_ID", "REPORT_CHANNEL_ID")


def _app_config_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _APP_NAME
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return base / _APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return base / _APP_NAME


def _config_path() -> Path:
    return _app_config_dir() / "config.json"


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return _app_config_dir()
    return _app_dir()


def _default_state_db_path() -> str:
    if getattr(sys, "frozen", False):
        return str((_app_config_dir() / "state.db").resolve())
    return "./state.db"


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip('"').strip("'")
        if normalized_key:
            os.environ.setdefault(normalized_key, normalized_value)


def _load_json_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip()
        if not normalized_key:
            continue
        normalized[str(normalized_key)] = str(value).strip()
    return normalized


def _apply_config_env(config: dict[str, str], *, override: bool = False) -> None:
    for key, value in config.items():
        if not key or not value:
            continue
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def _has_minimum_runtime_config() -> bool:
    for key in _REQUIRED_KEYS:
        if not (os.environ.get(key) or "").strip():
            return False
    listener_mode = (os.environ.get("LISTENER_MODE") or _SETUP_DEFAULTS["LISTENER_MODE"]).strip().lower()
    if listener_mode == "socket" and not (os.environ.get("SLACK_APP_TOKEN") or "").strip():
        return False
    return True


def _write_json_config(config_path: Path, payload: dict[str, str]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except Exception:
        pass


def _slack_api_validate_token(token: str, *, app_token: bool = False) -> tuple[bool, str]:
    endpoint = "https://slack.com/api/apps.connections.open" if app_token else "https://slack.com/api/auth.test"
    req = Request(
        endpoint,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"",
    )
    try:
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        return False, f"request failed: {exc}"
    try:
        payload = json.loads(raw)
    except Exception:
        return False, "invalid JSON response from Slack API"
    if bool(payload.get("ok")):
        return True, ""
    return False, str(payload.get("error") or "unknown_error")


def _setup_form_html(defaults: dict[str, str], error: str = "") -> str:
    def v(key: str) -> str:
        return defaults.get(key, "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    error_html = f"<p class='error'>{error}</p>" if error else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SlackClaw Setup</title>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color: #111; }}
    .card {{ max-width: 900px; margin: 0 auto; border: 1px solid #ddd; border-radius: 12px; padding: 20px; }}
    h1 {{ margin-top: 0; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    label {{ display:block; font-size: 13px; margin-bottom: 4px; color: #444; }}
    input, select, textarea {{ width: 100%; padding: 9px; border: 1px solid #bbb; border-radius: 8px; box-sizing: border-box; }}
    textarea {{ min-height: 96px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .full {{ grid-column: 1 / -1; }}
    .actions {{ margin-top: 18px; display:flex; gap:10px; align-items:center; }}
    button {{ border: 0; background: #0b57d0; color: #fff; padding: 10px 14px; border-radius: 8px; cursor: pointer; }}
    .muted {{ color:#666; font-size: 13px; }}
    .error {{ color: #b00020; background: #fde7ec; padding: 10px; border-radius: 8px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>SlackClaw Setup</h1>
    <p class="muted">Configuration is saved to <code>{_config_path()}</code>. This is local to your machine.</p>
    {error_html}
    <form method="post" action="/save">
      <div class="grid">
        <div class="full">
          <label>Slack Bot Token (required)</label>
          <input type="password" name="SLACK_BOT_TOKEN" value="{v("SLACK_BOT_TOKEN")}" />
        </div>
        <div>
          <label>Listener Mode</label>
          <select name="LISTENER_MODE">
            <option value="socket" {"selected" if v("LISTENER_MODE") != "poll" else ""}>socket</option>
            <option value="poll" {"selected" if v("LISTENER_MODE") == "poll" else ""}>poll</option>
          </select>
        </div>
        <div>
          <label>Slack App Token (required for socket mode)</label>
          <input type="password" name="SLACK_APP_TOKEN" value="{v("SLACK_APP_TOKEN")}" />
        </div>
        <div>
          <label>Command Channel ID (required)</label>
          <input name="COMMAND_CHANNEL_ID" value="{v("COMMAND_CHANNEL_ID")}" />
        </div>
        <div>
          <label>Report Channel ID (required)</label>
          <input name="REPORT_CHANNEL_ID" value="{v("REPORT_CHANNEL_ID")}" />
        </div>
        <div>
          <label>Run Mode</label>
          <select name="RUN_MODE">
            <option value="approve" {"selected" if v("RUN_MODE") != "run" else ""}>approve</option>
            <option value="run" {"selected" if v("RUN_MODE") == "run" else ""}>run</option>
          </select>
        </div>
        <div>
          <label>Approval Mode</label>
          <select name="APPROVAL_MODE">
            <option value="reaction" {"selected" if v("APPROVAL_MODE") != "none" else ""}>reaction</option>
            <option value="none" {"selected" if v("APPROVAL_MODE") == "none" else ""}>none</option>
          </select>
        </div>
        <div>
          <label>Dry Run</label>
          <select name="DRY_RUN">
            <option value="true" {"selected" if v("DRY_RUN").lower() != "false" else ""}>true</option>
            <option value="false" {"selected" if v("DRY_RUN").lower() == "false" else ""}>false</option>
          </select>
        </div>
        <div>
          <label>Worker Processes</label>
          <input type="number" min="1" name="WORKER_PROCESSES" value="{v("WORKER_PROCESSES") or "1"}" />
        </div>
        <div class="full">
          <label>State DB Path</label>
          <input name="STATE_DB_PATH" value="{v("STATE_DB_PATH")}" />
        </div>
        <div class="full">
          <label>Agent Workdir (optional absolute path)</label>
          <input name="AGENT_WORKDIR" value="{v("AGENT_WORKDIR")}" />
        </div>
        <div>
          <label>Kimi Permission Mode</label>
          <select name="KIMI_PERMISSION_MODE">
            <option value="yolo" {"selected" if v("KIMI_PERMISSION_MODE") != "default" else ""}>yolo</option>
            <option value="default" {"selected" if v("KIMI_PERMISSION_MODE") == "default" else ""}>default</option>
          </select>
        </div>
        <div>
          <label>Codex Permission Mode</label>
          <select name="CODEX_PERMISSION_MODE">
            <option value="full-auto" {"selected" if v("CODEX_PERMISSION_MODE") not in {"default", "dangerous"} else ""}>full-auto</option>
            <option value="default" {"selected" if v("CODEX_PERMISSION_MODE") == "default" else ""}>default</option>
            <option value="dangerous" {"selected" if v("CODEX_PERMISSION_MODE") == "dangerous" else ""}>dangerous</option>
          </select>
        </div>
        <div>
          <label>Codex Sandbox Mode</label>
          <select name="CODEX_SANDBOX_MODE">
            <option value="workspace-write" {"selected" if v("CODEX_SANDBOX_MODE") not in {"read-only", "danger-full-access"} else ""}>workspace-write</option>
            <option value="read-only" {"selected" if v("CODEX_SANDBOX_MODE") == "read-only" else ""}>read-only</option>
            <option value="danger-full-access" {"selected" if v("CODEX_SANDBOX_MODE") == "danger-full-access" else ""}>danger-full-access</option>
          </select>
        </div>
        <div>
          <label>Claude Permission Mode</label>
          <select name="CLAUDE_PERMISSION_MODE">
            <option value="acceptEdits" {"selected" if v("CLAUDE_PERMISSION_MODE") == "acceptEdits" else ""}>acceptEdits</option>
            <option value="default" {"selected" if v("CLAUDE_PERMISSION_MODE") == "default" else ""}>default</option>
            <option value="dontAsk" {"selected" if v("CLAUDE_PERMISSION_MODE") == "dontAsk" else ""}>dontAsk</option>
            <option value="bypassPermissions" {"selected" if v("CLAUDE_PERMISSION_MODE") == "bypassPermissions" else ""}>bypassPermissions</option>
            <option value="delegate" {"selected" if v("CLAUDE_PERMISSION_MODE") == "delegate" else ""}>delegate</option>
            <option value="plan" {"selected" if v("CLAUDE_PERMISSION_MODE") == "plan" else ""}>plan</option>
          </select>
        </div>
        <div class="full">
          <label>Shell Allowlist (comma or whitespace separated)</label>
          <textarea name="SHELL_ALLOWLIST">{v("SHELL_ALLOWLIST")}</textarea>
        </div>
      </div>
      <div class="actions">
        <button type="submit">Save and Start</button>
        <span class="muted">After save, this page will close and SlackClaw starts.</span>
      </div>
    </form>
  </div>
</body>
</html>
"""


def _run_setup_server(config_path: Path) -> bool:
    existing = _load_json_config(config_path)
    defaults = dict(_SETUP_DEFAULTS)
    defaults["STATE_DB_PATH"] = _default_state_db_path()
    defaults.update(existing)
    defaults.setdefault("STATE_DB_PATH", _default_state_db_path())
    saved = {"ok": False}

    class Handler(BaseHTTPRequestHandler):
        def _html(self, body: str, *, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                self._html(_setup_form_html(defaults))
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/save":
                self.send_response(404)
                self.end_headers()
                return

            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_length = 0
            raw_body = self.rfile.read(content_length).decode("utf-8", "replace")
            form = parse_qs(raw_body, keep_blank_values=True)

            payload: dict[str, str] = {}
            for key in set(_SETUP_DEFAULTS) | set(_REQUIRED_KEYS) | {"SLACK_APP_TOKEN"}:
                value = (form.get(key) or [""])[0].strip()
                if value:
                    payload[key] = value

            for key, default in _SETUP_DEFAULTS.items():
                payload.setdefault(key, default)

            missing = [key for key in _REQUIRED_KEYS if not payload.get(key, "").strip()]
            listener_mode = payload.get("LISTENER_MODE", "socket").strip().lower()
            if listener_mode == "socket" and not payload.get("SLACK_APP_TOKEN", "").strip():
                missing.append("SLACK_APP_TOKEN (required when LISTENER_MODE=socket)")

            if missing:
                self._html(_setup_form_html(payload, "Missing required fields: " + ", ".join(missing)), status=400)
                return

            bot_token = payload.get("SLACK_BOT_TOKEN", "")
            bot_ok, bot_error = _slack_api_validate_token(bot_token, app_token=False)
            if not bot_ok:
                self._html(
                    _setup_form_html(payload, f"SLACK_BOT_TOKEN validation failed: {bot_error}"),
                    status=400,
                )
                return

            if listener_mode == "socket":
                app_token = payload.get("SLACK_APP_TOKEN", "")
                app_ok, app_error = _slack_api_validate_token(app_token, app_token=True)
                if not app_ok:
                    self._html(
                        _setup_form_html(payload, f"SLACK_APP_TOKEN validation failed: {app_error}"),
                        status=400,
                    )
                    return

            _write_json_config(config_path, payload)
            saved["ok"] = True
            self._html(
                "<html><body><h3>Saved.</h3><p>You can close this tab. SlackClaw is starting.</p></body></html>"
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *_args) -> None:  # noqa: D401
            # Silence setup HTTP logs for cleaner CLI output.
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"SlackClaw setup: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()
    return bool(saved["ok"])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    show_config_path = False
    force_setup = False
    passthrough: list[str] = []
    for arg in args:
        if arg == "--show-config-path":
            show_config_path = True
            continue
        if arg == "--setup":
            force_setup = True
            continue
        passthrough.append(arg)

    cfg_path = _config_path()
    if show_config_path:
        print(cfg_path)
        if not passthrough and not force_setup:
            return 0

    root = _app_dir()
    _load_dotenv(root / ".env")
    _apply_config_env(_load_json_config(cfg_path), override=True)
    if force_setup or not _has_minimum_runtime_config():
        print("Launching local setup UI...", flush=True)
        if not _run_setup_server(cfg_path):
            print("Setup was not completed.", file=sys.stderr)
            return 2
        _apply_config_env(_load_json_config(cfg_path), override=True)

    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("STATE_DB_PATH", _default_state_db_path())
    if not getattr(sys, "frozen", False):
        src_path = str((root / "src").resolve())
        if src_path not in sys.path:
            sys.path.insert(0, src_path)
    os.chdir(runtime_dir)
    from slackclaw.app import run

    return run(passthrough)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
