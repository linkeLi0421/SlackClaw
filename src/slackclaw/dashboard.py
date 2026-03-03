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
                        "channel_id": t.payload.get("channel_id", ""),
                        "thread_ts": t.payload.get("thread_ts", ""),
                        "lock_key": t.payload.get("lock_key", ""),
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
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, Segoe UI, Roboto, sans-serif;
      background: #f5f6f8; color: #1a1a1a; padding: 20px;
    }
    .container { max-width: 1200px; margin: 0 auto; }
    h1 { margin-bottom: 8px; }
    .subtitle { color: #666; margin-bottom: 20px; font-size: 14px; }
    .stats-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 24px;
    }
    .stat-card {
      background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
      padding: 16px; text-align: center;
    }
    .stat-card .value { font-size: 28px; font-weight: 700; }
    .stat-card .label { font-size: 12px; color: #666; margin-top: 4px; }
    .card {
      background: #fff; border: 1px solid #e0e0e0; border-radius: 10px;
      padding: 16px; margin-bottom: 20px;
    }
    .card h2 { font-size: 16px; margin-bottom: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      text-align: left; padding: 8px 6px; border-bottom: 2px solid #e0e0e0;
      font-weight: 600; color: #444; white-space: nowrap;
    }
    td {
      padding: 7px 6px; border-bottom: 1px solid #f0f0f0;
      word-break: break-all;
    }
    tr:hover td { background: #f9fafb; }
    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 10px;
      font-size: 11px; font-weight: 600; text-transform: uppercase;
    }
    .badge-pending { background: #fff3cd; color: #856404; }
    .badge-running { background: #cce5ff; color: #004085; }
    .badge-succeeded { background: #d4edda; color: #155724; }
    .badge-failed { background: #f8d7da; color: #721c24; }
    .badge-canceled { background: #e2e3e5; color: #383d41; }
    .badge-waiting_approval { background: #fff3cd; color: #856404; }
    .badge-aborted_on_restart { background: #f8d7da; color: #721c24; }
    .badge-approved { background: #d4edda; color: #155724; }
    .badge-rejected { background: #f8d7da; color: #721c24; }
    .config-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 8px;
    }
    .config-item { font-size: 13px; }
    .config-item .key { font-weight: 600; color: #444; }
    .config-item .val { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .empty { color: #999; font-style: italic; padding: 12px; }
    .refresh-info { text-align: right; font-size: 12px; color: #999; margin-bottom: 8px; }
    .table-wrap { overflow-x: auto; }
  </style>
</head>
<body>
  <div class="container">
    <h1>SlackClaw Dashboard</h1>
    <p class="subtitle">Live status - auto-refreshes every 5 seconds</p>
    <p class="refresh-info">Last updated: <span id="last-updated">-</span></p>

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

  <script>
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
    function makeTable(headers, rows) {
      if (!rows || rows.length === 0) return '<p class="empty">No data</p>';
      var h = '<table><thead><tr>' + headers.map(function(h){return '<th>'+esc(h)+'</th>';}).join('') + '</tr></thead><tbody>';
      h += rows.join('');
      h += '</tbody></table>';
      return h;
    }

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
          {v: stats.total_tasks, l: 'Total Tasks'},
          {v: stats.tasks_by_status.pending || 0, l: 'Pending'},
          {v: stats.tasks_by_status.running || 0, l: 'Running'},
          {v: stats.tasks_by_status.succeeded || 0, l: 'Succeeded'},
          {v: stats.tasks_by_status.failed || 0, l: 'Failed'},
          {v: stats.tasks_by_status.waiting_approval || 0, l: 'Awaiting Approval'},
          {v: stats.queue_size, l: 'Queue Size'},
          {v: stats.in_flight_count, l: 'In-Flight'},
        ];
        sg.innerHTML = cards.map(function(c){
          return '<div class="stat-card"><div class="value">'+c.v+'</div><div class="label">'+c.l+'</div></div>';
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
        var tt = document.getElementById('tasks-table');
        var tRows = tasks.map(function(t){
          return '<tr><td>'+esc(trunc(t.task_id,12))+'</td><td>'+badge(t.status)+'</td><td>'+esc(trunc(t.command_text,50))+'</td>'
            +'<td>'+esc(t.trigger_user)+'</td><td>'+esc(t.channel_id)+'</td>'
            +'<td>'+esc(t.created_at)+'</td><td>'+esc(t.updated_at)+'</td></tr>';
        });
        tt.innerHTML = makeTable(['Task ID','Status','Command','User','Channel','Created','Updated'], tRows);
      }

      if (sessions) {
        var st = document.getElementById('sessions-table');
        var sRows = sessions.map(function(s){
          return '<tr><td>'+esc(s.channel_id)+'</td><td>'+esc(s.thread_ts)+'</td><td>'+esc(s.agent)+'</td>'
            +'<td>'+esc(trunc(s.session_id,20))+'</td><td>'+esc(s.updated_at)+'</td></tr>';
        });
        st.innerHTML = makeTable(['Channel','Thread','Agent','Session ID','Updated'], sRows);
      }

      if (approvals) {
        var at = document.getElementById('approvals-table');
        var aRows = approvals.map(function(a){
          return '<tr><td>'+esc(a.task_id)+'</td><td>'+badge(a.status)+'</td><td>'+esc(a.decided_by || '-')+'</td>'
            +'<td>'+esc(a.decision_reaction || '-')+'</td><td>'+esc(a.created_at)+'</td></tr>';
        });
        at.innerHTML = makeTable(['Task ID','Status','Decided By','Reaction','Created'], aRows);
      }

      if (locks) {
        var lt = document.getElementById('locks-table');
        var lRows = locks.map(function(l){
          return '<tr><td>'+esc(l.lock_key)+'</td><td>'+esc(l.task_id)+'</td><td>'+esc(l.acquired_at)+'</td></tr>';
        });
        lt.innerHTML = makeTable(['Lock Key','Task ID','Acquired At'], lRows);
      }
    }

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
