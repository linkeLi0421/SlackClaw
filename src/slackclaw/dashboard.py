from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import AppConfig
from .state_store import StateStore


@dataclass
class DashboardContext:
    config: AppConfig
    state_db_path: str
    queue_snapshot: Callable[[], list[dict]]
    in_flight_snapshot: Callable[[], list[dict]]
    queue_len: Callable[[], int]


def _json_response(handler: BaseHTTPRequestHandler, data: Any, *, status: int = 200) -> None:
    payload = json.dumps(data, default=str, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _html_response(handler: BaseHTTPRequestHandler, body: str, *, status: int = 200) -> None:
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _make_handler(ctx: DashboardContext) -> type:

    class DashboardHandler(BaseHTTPRequestHandler):

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            routes: dict[str, Callable[[], None]] = {
                "/": lambda: _html_response(self, _dashboard_html()),
                "/api/tasks": self._handle_api_tasks,
                "/api/sessions": self._handle_api_sessions,
                "/api/approvals": self._handle_api_approvals,
                "/api/locks": self._handle_api_locks,
                "/api/stats": self._handle_api_stats,
                "/api/config": self._handle_api_config,
            }
            route = routes.get(path)
            if route:
                route()
            else:
                self.send_response(404)
                self.end_headers()

        def _open_store(self) -> StateStore:
            return StateStore(ctx.state_db_path)

        def _handle_api_tasks(self) -> None:
            store = self._open_store()
            try:
                tasks = store.list_tasks(limit=200)
                data = [
                    {
                        "task_id": t.task_id,
                        "status": t.status.value,
                        "command_text": t.payload.get("command_text", ""),
                        "trigger_user": t.payload.get("trigger_user", ""),
                        "trigger_text": t.payload.get("trigger_text", ""),
                        "channel_id": t.payload.get("channel_id", ""),
                        "thread_ts": t.payload.get("thread_ts", ""),
                        "lock_key": t.payload.get("lock_key", ""),
                        "result_summary": t.payload.get("result_summary", ""),
                        "result_details": t.payload.get("result_details", ""),
                        "report_text": t.payload.get("report_text", ""),
                        "created_at": t.created_at,
                        "updated_at": t.updated_at,
                    }
                    for t in tasks
                ]
            finally:
                store.close()
            _json_response(self, data)

        def _handle_api_sessions(self) -> None:
            store = self._open_store()
            try:
                sessions = store.list_agent_sessions(limit=200)
            finally:
                store.close()
            _json_response(self, sessions)

        def _handle_api_approvals(self) -> None:
            store = self._open_store()
            try:
                approvals = store.list_task_approvals(limit=200)
                data = [
                    {
                        "task_id": a.task_id,
                        "channel_id": a.channel_id,
                        "status": a.status.value,
                        "decided_by": a.decided_by,
                        "decision_reaction": a.decision_reaction,
                        "created_at": a.created_at,
                        "updated_at": a.updated_at,
                    }
                    for a in approvals
                ]
            finally:
                store.close()
            _json_response(self, data)

        def _handle_api_locks(self) -> None:
            store = self._open_store()
            try:
                locks = store.list_execution_locks()
            finally:
                store.close()
            _json_response(self, locks)

        def _handle_api_stats(self) -> None:
            store = self._open_store()
            try:
                status_counts = store.count_tasks_by_status()
                checkpoints = store.list_checkpoints()
            finally:
                store.close()
            total = sum(status_counts.values())
            _json_response(self, {
                "total_tasks": total,
                "tasks_by_status": status_counts,
                "queue_size": ctx.queue_len(),
                "in_flight_count": len(ctx.in_flight_snapshot()),
                "in_flight": ctx.in_flight_snapshot(),
                "queue": ctx.queue_snapshot(),
                "checkpoints": checkpoints,
            })

        def _handle_api_config(self) -> None:
            cfg = ctx.config
            safe = {
                "command_channel_id": cfg.command_channel_id,
                "report_channel_id": cfg.report_channel_id,
                "listener_mode": cfg.listener_mode,
                "trigger_mode": cfg.trigger_mode,
                "trigger_prefix": cfg.trigger_prefix,
                "run_mode": cfg.run_mode,
                "approval_mode": cfg.approval_mode,
                "approve_reaction": cfg.approve_reaction,
                "reject_reaction": cfg.reject_reaction,
                "dry_run": cfg.dry_run,
                "worker_processes": cfg.worker_processes,
                "exec_timeout_seconds": cfg.exec_timeout_seconds,
                "poll_interval": cfg.poll_interval,
                "poll_batch_size": cfg.poll_batch_size,
                "state_db_path": cfg.state_db_path,
                "dashboard_port": cfg.dashboard_port,
                "shell_allowlist": list(cfg.shell_allowlist),
            }
            _json_response(self, safe)

        def log_message(self, *_args) -> None:
            return

    return DashboardHandler


def start_dashboard(ctx: DashboardContext) -> tuple[threading.Thread, ThreadingHTTPServer]:
    handler_class = _make_handler(ctx)
    port = ctx.config.dashboard_port
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="slackclaw-dashboard",
        daemon=True,
    )
    thread.start()
    return thread, server


def _dashboard_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SlackClaw Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #f5f0eb;
      --bg-card: #ffffff;
      --bg-card-hover: #faf8f5;
      --bg-input: #f0ece6;
      --border: #ddd6cb;
      --border-accent: #c05d3b;
      --text-primary: #2a2520;
      --text-secondary: #6b6360;
      --text-muted: #a09890;
      --accent: #c05d3b;
      --accent-dim: rgba(192,93,59,0.07);
      --success: #2d8659;
      --danger: #c93d3d;
      --warning: #b47a1e;
      --info: #2e7d9c;
      --font-display: 'Outfit', sans-serif;
      --font-mono: 'DM Mono', ui-monospace, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font-mono);
      background: var(--bg-primary); color: var(--text-primary);
      min-height: 100vh;
    }

    @keyframes fadeSlideIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes pulseAccent {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }

    .header {
      background: var(--bg-card);
      border-bottom: 2px solid var(--accent);
      padding: 24px 32px 18px;
      display: flex; align-items: baseline; gap: 20px; flex-wrap: wrap;
      box-shadow: 0 1px 3px rgba(42,37,32,0.06);
    }
    .header h1 {
      font-family: var(--font-display);
      font-size: 24px; font-weight: 700; letter-spacing: -0.5px;
      color: var(--accent);
    }
    .header .subtitle {
      font-size: 12px; color: var(--text-secondary); letter-spacing: 0.2px;
    }
    .header .refresh-info {
      font-size: 11px; color: var(--text-muted); margin-left: auto;
    }
    .container { max-width: 1280px; margin: 0 auto; padding: 24px 24px 48px; }

    .stats-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px; margin-bottom: 28px;
      animation: fadeSlideIn 0.4s ease-out;
    }
    .stat-card {
      background: var(--bg-card); border: 1px solid var(--border);
      padding: 16px 14px; text-align: center;
      border-radius: 6px;
      transition: border-color 0.2s, box-shadow 0.2s;
      position: relative; overflow: hidden;
    }
    .stat-card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0;
      height: 3px; background: var(--border); border-radius: 6px 6px 0 0;
    }
    .stat-card:hover { border-color: var(--accent); box-shadow: 0 2px 8px rgba(192,93,59,0.1); }
    .stat-card .value {
      font-family: var(--font-display);
      font-size: 30px; font-weight: 700; letter-spacing: -1px;
    }
    .stat-card .label {
      font-size: 10px; color: var(--text-muted); margin-top: 6px;
      text-transform: uppercase; letter-spacing: 1.5px;
    }
    .sc-total .value { color: var(--text-primary); }
    .sc-total::before { background: var(--accent); }
    .sc-pending .value { color: var(--warning); }
    .sc-pending::before { background: var(--warning); }
    .sc-running .value { color: var(--info); }
    .sc-running::before { background: var(--info); animation: pulseAccent 2s ease-in-out infinite; }
    .sc-succeeded .value { color: var(--success); }
    .sc-succeeded::before { background: var(--success); }
    .sc-failed .value { color: var(--danger); }
    .sc-failed::before { background: var(--danger); }
    .sc-approval .value { color: var(--warning); }
    .sc-approval::before { background: var(--warning); }
    .sc-queue .value { color: var(--text-secondary); }
    .sc-queue::before { background: var(--text-secondary); }
    .sc-inflight .value { color: var(--info); }
    .sc-inflight::before { background: var(--info); }

    .card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 6px;
      padding: 20px; margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(42,37,32,0.04);
      animation: fadeSlideIn 0.5s ease-out both;
    }
    .card:nth-child(2) { animation-delay: 0.05s; }
    .card:nth-child(3) { animation-delay: 0.1s; }
    .card:nth-child(4) { animation-delay: 0.15s; }
    .card h2 {
      font-family: var(--font-display);
      font-size: 14px; font-weight: 600; color: var(--accent);
      text-transform: uppercase; letter-spacing: 1.2px;
      margin-bottom: 14px; padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th {
      text-align: left; padding: 8px 10px;
      font-weight: 500; color: var(--text-muted); white-space: nowrap;
      text-transform: uppercase; font-size: 10px; letter-spacing: 1px;
      border-bottom: 2px solid var(--border);
    }
    td {
      padding: 8px 10px; border-bottom: 1px solid rgba(221,214,203,0.6);
      word-break: break-all; color: var(--text-secondary);
    }
    tr:hover td { background: var(--accent-dim); color: var(--text-primary); }
    tr.task-row { cursor: pointer; }

    .badge {
      display: inline-block; padding: 2px 8px;
      font-size: 10px; font-weight: 500; text-transform: uppercase;
      letter-spacing: 0.5px; border-radius: 3px;
    }
    .badge-pending { background: rgba(180,122,30,0.12); color: var(--warning); }
    .badge-running { background: rgba(46,125,156,0.12); color: var(--info); }
    .badge-succeeded { background: rgba(45,134,89,0.12); color: var(--success); }
    .badge-failed { background: rgba(201,61,61,0.12); color: var(--danger); }
    .badge-canceled { background: rgba(160,152,144,0.12); color: var(--text-muted); }
    .badge-waiting_approval { background: rgba(180,122,30,0.12); color: var(--warning); }
    .badge-aborted_on_restart { background: rgba(201,61,61,0.12); color: var(--danger); }
    .badge-approved { background: rgba(45,134,89,0.12); color: var(--success); }
    .badge-rejected { background: rgba(201,61,61,0.12); color: var(--danger); }

    .config-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 6px;
    }
    .config-item { font-size: 12px; padding: 3px 0; }
    .config-item .key { color: var(--text-muted); }
    .config-item .val { color: var(--accent); font-weight: 500; }
    .empty { color: var(--text-muted); font-style: italic; padding: 12px; }
    .table-wrap { overflow-x: auto; }

    /* Modal */
    .modal-overlay {
      display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
      background: rgba(42,37,32,0.4); z-index: 1000;
      justify-content: center; align-items: center;
      backdrop-filter: blur(4px);
    }
    .modal-overlay.active { display: flex; }
    .modal {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: 8px;
      width: 90%; max-width: 720px; max-height: 80vh;
      display: flex; flex-direction: column;
      box-shadow: 0 8px 32px rgba(42,37,32,0.15);
      animation: fadeSlideIn 0.25s ease-out;
    }
    .modal-header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 16px 20px; border-bottom: 1px solid var(--border);
    }
    .modal-header h3 {
      font-family: var(--font-display);
      font-size: 15px; font-weight: 600; color: var(--accent);
      text-transform: uppercase; letter-spacing: 1px;
    }
    .modal-close {
      background: none; border: 1px solid var(--border); color: var(--text-muted);
      font-size: 16px; cursor: pointer; padding: 4px 10px; line-height: 1;
      border-radius: 4px;
      transition: border-color 0.2s, color 0.2s, background 0.2s;
    }
    .modal-close:hover { border-color: var(--danger); color: var(--danger); background: rgba(201,61,61,0.06); }
    .modal-body { padding: 20px; overflow-y: auto; flex: 1; }

    .chat-row { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 12px; }
    .chat-row.row-user { flex-direction: row; }
    .chat-row.row-system { flex-direction: row-reverse; }
    .chat-avatar {
      width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
      border: 2px solid var(--border);
    }
    .chat-bubble {
      max-width: 80%; padding: 10px 14px;
      font-size: 12px; line-height: 1.6;
      border-radius: 10px;
    }
    .chat-user { background: var(--bg-input); color: var(--text-primary); border: 1px solid var(--border); }
    .chat-system { background: rgba(192,93,59,0.06); color: var(--text-secondary); border: 1px solid rgba(192,93,59,0.15); }
    .chat-label {
      font-size: 10px; font-weight: 500; text-transform: uppercase;
      color: var(--text-muted); margin-bottom: 4px; letter-spacing: 0.5px;
    }
    .chat-bubble pre {
      background: var(--bg-input); padding: 8px 10px;
      overflow-x: auto; font-size: 11px; margin-top: 6px;
      white-space: pre-wrap; word-break: break-all;
      border: 1px solid var(--border); border-radius: 4px; color: var(--text-secondary);
    }
    .chat-placeholder { color: var(--text-muted); font-style: italic; }
  </style>
</head>
<body>
  <div class="header">
    <h1>SlackClaw</h1>
    <p class="subtitle">live dashboard &mdash; 5s refresh</p>
    <p class="refresh-info"><span id="last-updated">-</span></p>
  </div>
  <div class="container">
    <div class="stats-grid" id="stats-grid"></div>

    <div class="card">
      <h2>Configuration</h2>
      <div class="config-grid" id="config-grid"></div>
    </div>

    <div class="card">
      <h2>Recent Tasks</h2>
      <div class="table-wrap" id="tasks-table"></div>
    </div>

    <div class="card">
      <h2>In-Flight Tasks</h2>
      <div class="table-wrap" id="inflight-table"></div>
    </div>

    <div class="card">
      <h2>Queue</h2>
      <div class="table-wrap" id="queue-table"></div>
    </div>

    <div class="card">
      <h2>Agent Sessions</h2>
      <div class="table-wrap" id="sessions-table"></div>
    </div>

    <div class="card">
      <h2>Approvals</h2>
      <div class="table-wrap" id="approvals-table"></div>
    </div>

    <div class="card">
      <h2>Execution Locks</h2>
      <div class="table-wrap" id="locks-table"></div>
    </div>
  </div>

  <!-- Task detail modal -->
  <div class="modal-overlay" id="task-modal">
    <div class="modal">
      <div class="modal-header">
        <h3 id="modal-title">Task Detail</h3>
        <button class="modal-close" id="modal-close">&times;</button>
      </div>
      <div class="modal-body" id="modal-body"></div>
    </div>
  </div>

  <script>
    var _taskCache = [];
    var AVATAR_USER = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCI+PHJlY3Qgd2lkdGg9IjQwIiBoZWlnaHQ9IjQwIiByeD0iMjAiIGZpbGw9IiNmMGVjZTYiLz48Y2lyY2xlIGN4PSIyMCIgY3k9IjE0IiByPSI2IiBmaWxsPSIjYzA1ZDNiIi8+PHBhdGggZD0iTTggMzRjMC03LjUgNS40LTEyIDEyLTEyczEyIDQuNSAxMiAxMiIgZmlsbD0iI2MwNWQzYiIvPjwvc3ZnPg==';
    var AVATAR_BOT = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0MCIgaGVpZ2h0PSI0MCI+PHJlY3Qgd2lkdGg9IjQwIiBoZWlnaHQ9IjQwIiByeD0iMjAiIGZpbGw9IiNlOGY1ZWUiLz48cmVjdCB4PSIxMCIgeT0iMTMiIHdpZHRoPSIyMCIgaGVpZ2h0PSIxNiIgcng9IjMiIGZpbGw9IiMyZDg2NTkiLz48Y2lyY2xlIGN4PSIxNSIgY3k9IjIwIiByPSIyLjUiIGZpbGw9IiNlOGY1ZWUiLz48Y2lyY2xlIGN4PSIyNSIgY3k9IjIwIiByPSIyLjUiIGZpbGw9IiNlOGY1ZWUiLz48cmVjdCB4PSIxNCIgeT0iMjUiIHdpZHRoPSIxMiIgaGVpZ2h0PSIyIiByeD0iMSIgZmlsbD0iI2U4ZjVlZSIvPjxsaW5lIHgxPSIyMCIgeTE9IjciIHgyPSIyMCIgeTI9IjEzIiBzdHJva2U9IiMyZDg2NTkiIHN0cm9rZS13aWR0aD0iMiIvPjxjaXJjbGUgY3g9IjIwIiBjeT0iNiIgcj0iMiIgZmlsbD0iIzJkODY1OSIvPjwvc3ZnPg==';

    function badge(status) {
      return '<span class="badge badge-' + status + '">' + status + '</span>';
    }
    function esc(s) {
      if (s == null) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function trunc(s, n) {
      s = String(s || '');
      return s.length > n ? s.substring(0, n) + '...' : s;
    }
    function localTime(s) {
      if (!s) return '';
      try { return new Date(s).toLocaleString(); }
      catch(e) { return esc(s); }
    }
    function makeTable(headers, rows) {
      if (!rows || rows.length === 0) return '<p class="empty">No data</p>';
      var h = '<table><thead><tr>' + headers.map(function(h){return '<th>'+esc(h)+'</th>';}).join('') + '</tr></thead><tbody>';
      h += rows.join('');
      h += '</tbody></table>';
      return h;
    }

    function openTaskModal(taskId) {
      var task = _taskCache.find(function(t){ return t.task_id === taskId; });
      if (!task) return;
      var overlay = document.getElementById('task-modal');
      var title = document.getElementById('modal-title');
      var body = document.getElementById('modal-body');

      // Find all tasks in the same thread
      var threadTs = task.thread_ts;
      var channelId = task.channel_id;
      var threadTasks;
      if (threadTs && channelId) {
        threadTasks = _taskCache.filter(function(t) {
          return t.thread_ts === threadTs && t.channel_id === channelId;
        });
        threadTasks.sort(function(a, b) {
          return (a.created_at || '').localeCompare(b.created_at || '');
        });
      } else {
        threadTasks = [task];
      }

      if (threadTasks.length > 1) {
        title.textContent = 'Thread (' + threadTasks.length + ' commands)';
      } else {
        title.textContent = 'Task Detail';
      }

      var html = '<div style="display:flex;flex-direction:column;">';
      threadTasks.forEach(function(t, idx) {
        var isClicked = (t.task_id === taskId);
        var highlight = isClicked ? ' style="border:1px solid var(--accent);"' : '';
        html += '<div class="chat-row row-user">';
        html += '<img class="chat-avatar" src="' + AVATAR_USER + '" alt="User">';
        html += '<div class="chat-bubble chat-user"' + highlight + '>';
        html += esc(t.command_text || '(empty)');
        html += '</div></div>';
        html += '<div class="chat-row row-system">';
        html += '<img class="chat-avatar" src="' + AVATAR_BOT + '" alt="Bot">';
        if (t.status === 'running' || t.status === 'pending') {
          html += '<div class="chat-bubble chat-system"><div class="chat-label">SlackClaw &middot; ' + badge(t.status) + '</div><span class="chat-placeholder">Awaiting result&hellip;</span></div>';
        } else if (t.report_text) {
          html += '<div class="chat-bubble chat-system"><div class="chat-label">SlackClaw &middot; ' + badge(t.status) + '</div>';
          html += '<pre>' + esc(t.report_text) + '</pre>';
          html += '</div>';
        } else if (t.result_summary || t.result_details) {
          html += '<div class="chat-bubble chat-system"><div class="chat-label">SlackClaw &middot; ' + badge(t.status) + '</div>';
          if (t.result_summary) html += '<div>' + esc(t.result_summary) + '</div>';
          if (t.result_details) html += '<pre>' + esc(t.result_details) + '</pre>';
          html += '</div>';
        } else {
          html += '<div class="chat-bubble chat-system"><div class="chat-label">SlackClaw &middot; ' + badge(t.status) + '</div><span class="chat-placeholder">No result data stored</span></div>';
        }
        html += '</div>';
        if (idx < threadTasks.length - 1) {
          html += '<hr style="border:none;border-top:1px dashed var(--border);margin:8px 0;">';
        }
      });
      html += '</div>';
      body.innerHTML = html;
      overlay.classList.add('active');
    }

    document.getElementById('modal-close').addEventListener('click', function(){ document.getElementById('task-modal').classList.remove('active'); });
    document.getElementById('task-modal').addEventListener('click', function(e){ if (e.target === this) this.classList.remove('active'); });
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape') document.getElementById('task-modal').classList.remove('active'); });
    document.getElementById('tasks-table').addEventListener('click', function(e){
      var row = e.target.closest('.task-row');
      if (row && row.dataset.taskid) openTaskModal(row.dataset.taskid);
    });

    async function fetchJson(url) {
      try { var r = await fetch(url); return await r.json(); }
      catch(e) { return null; }
    }

    async function refresh() {
      var results = await Promise.all([
        fetchJson('/api/stats'),
        fetchJson('/api/config'),
        fetchJson('/api/tasks'),
        fetchJson('/api/sessions'),
        fetchJson('/api/approvals'),
        fetchJson('/api/locks'),
      ]);
      var stats = results[0], config = results[1], tasks = results[2];
      var sessions = results[3], approvals = results[4], locks = results[5];

      document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();

      if (stats) {
        var sg = document.getElementById('stats-grid');
        var cards = [
          {v: stats.total_tasks, l: 'Total Tasks', c: 'sc-total'},
          {v: stats.tasks_by_status.pending || 0, l: 'Pending', c: 'sc-pending'},
          {v: stats.tasks_by_status.running || 0, l: 'Running', c: 'sc-running'},
          {v: stats.tasks_by_status.succeeded || 0, l: 'Succeeded', c: 'sc-succeeded'},
          {v: stats.tasks_by_status.failed || 0, l: 'Failed', c: 'sc-failed'},
          {v: stats.tasks_by_status.waiting_approval || 0, l: 'Awaiting Approval', c: 'sc-approval'},
          {v: stats.queue_size, l: 'Queue Size', c: 'sc-queue'},
          {v: stats.in_flight_count, l: 'In-Flight', c: 'sc-inflight'},
        ];
        sg.innerHTML = cards.map(function(c){
          return '<div class="stat-card '+c.c+'"><div class="value">'+c.v+'</div><div class="label">'+c.l+'</div></div>';
        }).join('');

        var ift = document.getElementById('inflight-table');
        var ifRows = (stats.in_flight || []).map(function(t){
          return '<tr><td>'+esc(t.task_id)+'</td><td>'+esc(trunc(t.command_text,60))+'</td><td>'+esc(t.trigger_user)+'</td></tr>';
        });
        ift.innerHTML = makeTable(['Task ID','Command','User'], ifRows);

        var qt = document.getElementById('queue-table');
        var qRows = (stats.queue || []).map(function(t){
          return '<tr><td>'+esc(t.task_id)+'</td><td>'+esc(trunc(t.command_text,60))+'</td><td>'+esc(t.trigger_user)+'</td></tr>';
        });
        qt.innerHTML = makeTable(['Task ID','Command','User'], qRows);
      }

      if (config) {
        var cg = document.getElementById('config-grid');
        cg.innerHTML = Object.entries(config).map(function(kv){
          var k = kv[0], v = kv[1];
          var display = Array.isArray(v) ? v.join(', ') : String(v);
          return '<div class="config-item"><span class="key">'+esc(k)+':</span> <span class="val">'+esc(trunc(display,80))+'</span></div>';
        }).join('');
      }

      if (tasks) {
        _taskCache = tasks;
        var tt = document.getElementById('tasks-table');
        var tRows = tasks.map(function(t){
          return '<tr class="task-row" data-taskid="'+esc(t.task_id)+'">'
            +'<td>'+esc(trunc(t.task_id,12))+'</td><td>'+badge(t.status)+'</td><td>'+esc(trunc(t.command_text,50))+'</td>'
            +'<td>'+esc(t.trigger_user)+'</td><td>'+esc(t.channel_id)+'</td>'
            +'<td>'+localTime(t.created_at)+'</td><td>'+localTime(t.updated_at)+'</td></tr>';
        });
        tt.innerHTML = makeTable(['Task ID','Status','Command','User','Channel','Created','Updated'], tRows);
      }

      if (sessions) {
        var st = document.getElementById('sessions-table');
        var sRows = sessions.map(function(s){
          return '<tr><td>'+esc(s.channel_id)+'</td><td>'+esc(s.thread_ts)+'</td><td>'+esc(s.agent)+'</td>'
            +'<td>'+esc(trunc(s.session_id,20))+'</td><td>'+localTime(s.updated_at)+'</td></tr>';
        });
        st.innerHTML = makeTable(['Channel','Thread','Agent','Session ID','Updated'], sRows);
      }

      if (approvals) {
        var at = document.getElementById('approvals-table');
        var aRows = approvals.map(function(a){
          return '<tr><td>'+esc(a.task_id)+'</td><td>'+badge(a.status)+'</td><td>'+esc(a.decided_by || '-')+'</td>'
            +'<td>'+esc(a.decision_reaction || '-')+'</td><td>'+localTime(a.created_at)+'</td></tr>';
        });
        at.innerHTML = makeTable(['Task ID','Status','Decided By','Reaction','Created'], aRows);
      }

      if (locks) {
        var lt = document.getElementById('locks-table');
        var lRows = locks.map(function(l){
          return '<tr><td>'+esc(l.lock_key)+'</td><td>'+esc(l.task_id)+'</td><td>'+localTime(l.acquired_at)+'</td></tr>';
        });
        lt.innerHTML = makeTable(['Lock Key','Task ID','Acquired At'], lRows);
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
