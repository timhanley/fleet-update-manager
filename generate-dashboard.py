#!/usr/bin/env python3
"""
generate-dashboard.py
Reads all JSON run logs and produces fleet-status.html.
Called automatically by the scheduled task after each update run.
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR", str(SCRIPT_DIR)))
LOG_DIR = DATA_DIR / "logs"
FLEET_CONF = DATA_DIR / "fleet.conf"
OUTPUT = DATA_DIR / "fleet-status.html"

# ── Load data ─────────────────────────────────────────────────────────────────
def load_runs():
    runs = []
    for f in sorted(LOG_DIR.glob("run-*.json"), reverse=True):
        try:
            runs.append(json.loads(f.read_text()))
        except Exception:
            pass
    return runs

def load_fleet_conf():
    try:
        return json.loads(FLEET_CONF.read_text())
    except Exception:
        return {"devices": []}

def latest_per_device(runs):
    """Return the most recent result for each device name."""
    seen = {}
    for run in runs:
        for dev in run.get("devices", []):
            if dev["name"] not in seen:
                seen[dev["name"]] = {**dev, "run_timestamp": run["run_timestamp"]}
    return seen

def status_badge(status):
    badges = {
        "success":        ('<span class="badge ok">✔ OK</span>', "ok"),
        "reboot_required":('<span class="badge warn">↻ Reboot needed</span>', "warn"),
        "rebooting":       ('<span class="badge rebooting">↻ Rebooting…</span>', "rebooting"),
        "error":          ('<span class="badge err">✖ Error</span>', "err"),
        "unreachable":    ('<span class="badge unreachable">⚡ Unreachable</span>', "unreachable"),
    }
    return badges.get(status, ('<span class="badge">Unknown</span>', ""))

def fmt_ts(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d&nbsp;%H:%M UTC")
    except Exception:
        return ts_str

def history_rows(runs, limit=10):
    rows = []
    for run in runs[:limit]:
        ts = fmt_ts(run.get("run_timestamp", ""))
        devices = run.get("devices", [])
        total = len(devices)
        ok = sum(1 for d in devices if d["status"] == "success")
        reboot = sum(1 for d in devices if d["status"] in ("reboot_required", "rebooting"))
        errors = sum(1 for d in devices if d["status"] in ("error", "unreachable"))
        pkgs = sum(d.get("packages_upgraded", 0) for d in devices)
        dur = run.get("duration_seconds", 0)
        run_id = html.escape("run-" + run.get("run_id", "")) if run.get("run_id") else ""

        row_class = "err-row" if errors else ("warn-row" if reboot else "")
        has_log = "history-row" if run_id else ""
        title = ' title="Click to view run log"' if run_id else ""
        rows.append(f"""
        <tr class="{row_class} {has_log}" data-run-id="{run_id}" onclick="showRunLog(this)"{title}>
          <td>{ts}</td>
          <td>{total}</td>
          <td class="ok-txt">{ok}</td>
          <td class="warn-txt">{reboot}</td>
          <td class="err-txt">{errors if errors else '—'}</td>
          <td>{pkgs}</td>
          <td>{dur}s</td>
        </tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="7" class="muted">No runs yet</td></tr>'

def device_cards(latest, conf_devices):
    # Merge config devices (for display even if never run) with run data
    conf_names = [d["name"] for d in conf_devices]
    all_names = list(latest.keys()) + [n for n in conf_names if n not in latest]

    cards = []
    for name in all_names:
        dev = latest.get(name)
        conf = next((d for d in conf_devices if d["name"] == name), {})
        enabled = conf.get("enabled", True)

        if not dev:
            cards.append(f"""
      <div class="card pending">
        <div class="card-header">
          <span class="device-name">{name}</span>
          <span class="badge pending-badge">⏳ Never run</span>
        </div>
        <div class="card-body muted">{conf.get('host','—')}<br>{conf.get('description','')}</div>
      </div>""")
            continue

        badge_html, badge_cls = status_badge(dev.get("status", ""))
        ts = fmt_ts(dev.get("run_timestamp", ""))
        pkgs = dev.get("packages_upgraded", 0)
        reboot_pkgs = dev.get("reboot_packages", [])
        error = dev.get("error") or ""
        desc = dev.get("description") or conf.get("description", "")

        reboot_detail = ""
        if reboot_pkgs:
            reboot_detail = f'<div class="reboot-pkgs">Packages: {", ".join(reboot_pkgs)}</div>'

        error_detail = ""
        if error:
            error_detail = f'<div class="error-msg">⚠ {error[:200]}</div>'

        distro_upgrade = dev.get("distro_upgrade") or ""
        distro_detail = ""
        if distro_upgrade:
            import html as _html
            distro_detail = f'<div class="distro-upgrade-notice">⬆ {_html.escape(distro_upgrade.strip())}</div>'

        log_detail = ""
        output_log = dev.get("output_log", [])
        if output_log:
            import html as _html
            log_text = _html.escape("\n".join(output_log))
            log_detail = f'<details class="device-log"><summary>📋 Last update log</summary><pre class="device-log-pre">{log_text}</pre></details>'

        disabled_note = "" if enabled else '<span class="muted"> (disabled)</span>'
        btn_name_attr = json.dumps(name).replace('"', '&quot;')
        run_btn = f'<button class="card-run-btn" onclick="runDevice({btn_name_attr}, this)" title="Update {name} now">▶</button>'

        cards.append(f"""
      <div class="card {badge_cls}" data-device="{__import__('html').escape(name)}">
        <div class="card-header">
          <span class="device-name">{name}{disabled_note}</span>
          <div class="card-actions">{badge_html}{run_btn}</div>
        </div>
        <div class="card-meta">
          <span title="Host">{dev.get('host','—')}</span>
          {f'<span class="sep">·</span><span>{desc}</span>' if desc else ''}
        </div>
        <div class="card-stats">
          <div class="stat"><div class="stat-val">{pkgs}</div><div class="stat-lbl">pkgs upgraded</div></div>
          <div class="stat"><div class="stat-val">{dev.get('os','?').split('(')[0].strip()[:24]}</div><div class="stat-lbl">OS</div></div>
          <div class="stat"><div class="stat-val">{dev.get('kernel','?')[:20]}</div><div class="stat-lbl">kernel</div></div>
          <div class="stat"><div class="stat-val">{dev.get('disk','?')}</div><div class="stat-lbl">disk used</div></div>
          <div class="stat"><div class="stat-val">{dev.get('uptime','?')[:20]}</div><div class="stat-lbl">uptime</div></div>
          <div class="stat"><div class="stat-val">{dev.get('duration_seconds','?')}s</div><div class="stat-lbl">update time</div></div>
        </div>
        {reboot_detail}{error_detail}{distro_detail}{log_detail}
        <div class="card-footer muted">Last run: {ts}</div>
      </div>""")
    return "\n".join(cards)

# ── HTML template ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Status Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PGNpcmNsZSBjeD0iMTYiIGN5PSIxNiIgcj0iMTUiIGZpbGw9IiMxYTdmNjQiLz48cmVjdCB4PSI4IiB5PSI5IiB3aWR0aD0iMTYiIGhlaWdodD0iMyIgcng9IjEiIGZpbGw9IiNmZmYiLz48cmVjdCB4PSI4IiB5PSIxNCIgd2lkdGg9IjE2IiBoZWlnaHQ9IjMiIHJ4PSIxIiBmaWxsPSIjZmZmIi8+PHJlY3QgeD0iOCIgeT0iMTkiIHdpZHRoPSIxNiIgaGVpZ2h0PSIzIiByeD0iMSIgZmlsbD0iI2ZmZiIvPjxjaXJjbGUgY3g9IjIxIiBjeT0iMTAuNSIgcj0iMSIgZmlsbD0iIzFhN2Y2NCIvPjxjaXJjbGUgY3g9IjIxIiBjeT0iMTUuNSIgcj0iMSIgZmlsbD0iIzFhN2Y2NCIvPjxjaXJjbGUgY3g9IjIxIiBjeT0iMjAuNSIgcj0iMSIgZmlsbD0iIzFhN2Y2NCIvPjwvc3ZnPg==">
<style>
  :root {{
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --ok: #22c55e; --warn: #f59e0b; --err: #ef4444; --muted: #6b7280;
    --unreachable: #64748b;
    --text: #e5e7eb; --text2: #9ca3af; --border: #2d3148;
    --accent: #6366f1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }}
  a {{ color: var(--accent); text-decoration: none; }}

  header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 18px 32px; display: flex; align-items: center; gap: 16px; }}
  header h1 {{ font-size: 20px; font-weight: 700; }}
  header .subtitle {{ color: var(--text2); font-size: 13px; margin-left: auto; }}
  .admin-link {{
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(99,102,241,.12); color: var(--accent);
    border: 1px solid rgba(99,102,241,.3); padding: 7px 14px;
    border-radius: 8px; font-size: 13px; font-weight: 600;
    white-space: nowrap; transition: background .15s;
  }}
  .admin-link:hover {{ background: rgba(99,102,241,.22); }}

  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 32px; }}

  /* Summary bar */
  .summary {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .summary-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 24px; flex: 1; min-width: 140px; }}
  .summary-card .val {{ font-size: 32px; font-weight: 700; line-height: 1.1; }}
  .summary-card .lbl {{ color: var(--text2); font-size: 12px; margin-top: 4px; text-transform: uppercase; letter-spacing: .05em; }}
  .val.ok          {{ color: var(--ok); }}
  .val.warn        {{ color: var(--warn); }}
  .val.err         {{ color: var(--err); }}
  .val.unreachable {{ color: var(--unreachable); }}
  .summary-card .val.val-sm {{ font-size: 15px; font-weight: 600; line-height: 1.5; padding-top: 4px; }}

  /* Device cards */
  .section-title {{ font-size: 16px; font-weight: 600; margin-bottom: 14px; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 36px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  .card.ok          {{ border-left: 3px solid var(--ok); }}
  .card.warn        {{ border-left: 3px solid var(--warn); }}
  .card.err         {{ border-left: 3px solid var(--err); }}
  .card.unreachable {{ border-left: 3px solid var(--unreachable); opacity: .8; }}
  .card.pending     {{ border-left: 3px solid var(--muted); opacity: .7; }}
  .card-header {{ display: flex; align-items: center; justify-content: space-between; padding: 14px 16px 8px; }}
  .card-actions {{ display: flex; align-items: center; gap: 8px; }}
  .card-run-btn {{
    background: rgba(99,102,241,.12); border: none; color: var(--accent);
    width: 26px; height: 26px; border-radius: 50%; cursor: pointer; font-size: 10px;
    display: inline-flex; align-items: center; justify-content: center;
    transition: background .15s; flex-shrink: 0;
  }}
  .card-run-btn:hover {{ background: rgba(99,102,241,.28); }}
  .card-run-btn:disabled {{ opacity: .35; cursor: not-allowed; }}
  @keyframes card-spin {{ to {{ transform: rotate(360deg); }} }}
  .card-run-btn.spinning {{ animation: card-spin .8s linear infinite; cursor: default; }}
  .device-name {{ font-weight: 700; font-size: 15px; }}
  .card-meta {{ padding: 0 16px 8px; color: var(--text2); font-size: 12px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .sep {{ color: var(--border); }}
  .card-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--border); margin: 8px 0; }}
  .stat {{ background: var(--surface2); padding: 10px 12px; min-width: 0; }}
  .stat-val {{ font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .stat-lbl {{ color: var(--text2); font-size: 11px; margin-top: 2px; white-space: nowrap; }}
  .card-footer {{ padding: 8px 16px 12px; font-size: 12px; color: var(--muted); }}
  .reboot-pkgs {{ padding: 6px 16px; font-size: 12px; color: var(--warn); background: rgba(245,158,11,.08); }}
  .error-msg   {{ padding: 6px 16px; font-size: 12px; color: var(--err); background: rgba(239,68,68,.08); word-break: break-word; }}
  .distro-upgrade-notice {{ padding: 6px 16px; font-size: 12px; color: #a78bfa; background: rgba(139,92,246,.1); border-top: 1px solid rgba(139,92,246,.2); }}
  .card-body   {{ padding: 8px 16px 14px; font-size: 13px; }}

  /* Badge */
  .badge {{ font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 20px; white-space: nowrap; }}
  .badge.ok          {{ background: rgba(34,197,94,.15);   color: var(--ok); }}
  .badge.warn        {{ background: rgba(245,158,11,.15);  color: var(--warn); }}
  .badge.rebooting   {{ background: rgba(99,102,241,.15);  color: #a5b4fc; }}
  .badge.err         {{ background: rgba(239,68,68,.15);   color: var(--err); }}
  .badge.unreachable {{ background: rgba(100,116,139,.15); color: var(--unreachable); }}
  .badge.pending-badge {{ background: rgba(107,114,128,.15); color: var(--muted); }}

  /* History table */
  .history {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 36px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: var(--surface2); padding: 10px 16px; text-align: left; font-size: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }}
  td {{ padding: 10px 16px; border-top: 1px solid var(--border); font-size: 13px; }}
  tr.warn-row td {{ background: rgba(245,158,11,.04); }}
  tr.err-row  td {{ background: rgba(239,68,68,.04); }}
  .ok-txt   {{ color: var(--ok); }}
  .warn-txt {{ color: var(--warn); }}
  .err-txt  {{ color: var(--err); }}
  .muted    {{ color: var(--muted); }}

  footer {{ text-align: center; color: var(--muted); font-size: 12px; padding: 24px; border-top: 1px solid var(--border); }}

  /* Run Now button */
  .run-btn {{
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--accent); color: #fff; border: none;
    padding: 9px 18px; border-radius: 8px; font-size: 13px;
    font-weight: 600; cursor: pointer; transition: opacity .15s;
    white-space: nowrap;
  }}
  .run-btn:hover {{ opacity: .85; }}
  .run-btn:disabled {{ opacity: .45; cursor: not-allowed; }}
  .run-btn .spinner {{
    width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
    border-top-color: #fff; border-radius: 50%;
    animation: spin .7s linear infinite; display: none;
  }}
  .run-btn.running .spinner {{ display: block; }}
  .run-btn.running .btn-icon {{ display: none; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  /* Live output panel */
  #output-panel {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 28px; overflow: hidden;
    display: none;
  }}
  #output-panel.visible {{ display: block; }}
  .output-header {{
    background: var(--surface2); padding: 10px 16px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border);
  }}
  .output-header span {{ font-size: 13px; font-weight: 600; }}
  .output-close {{
    background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 18px; line-height: 1; padding: 0 4px;
  }}
  .output-close:hover {{ color: var(--text); }}
  #output-log {{
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px;
    padding: 14px 16px; max-height: 360px; overflow-y: auto;
    line-height: 1.6; white-space: pre-wrap; word-break: break-all;
    color: #d1d5db;
  }}
  #output-log .ok-line   {{ color: var(--ok); }}
  #output-log .err-line  {{ color: var(--err); }}
  #output-log .warn-line {{ color: var(--warn); }}
  #output-log .dim-line  {{ color: var(--muted); }}

  /* Per-device last-run log toggle */
  .device-log {{
    margin: 8px 0 4px; border-top: 1px solid var(--border);
    padding-top: 6px;
  }}
  .device-log summary {{
    font-size: 12px; color: var(--muted); cursor: pointer;
    user-select: none; list-style: none; display: flex; align-items: center; gap: 6px;
  }}
  .device-log summary::-webkit-details-marker {{ display: none; }}
  .device-log summary::before {{ content: '▶'; font-size: 9px; transition: transform .15s; }}
  .device-log[open] summary::before {{ transform: rotate(90deg); }}
  .device-log-pre {{
    margin: 8px 0 0; padding: 10px 12px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px;
    line-height: 1.55; color: #9ca3af; white-space: pre-wrap; word-break: break-all;
    max-height: 260px; overflow-y: auto;
  }}

  /* Pending state during active run */
  .card.pending-update {{
    opacity: .45; transition: opacity .4s;
  }}
  .card.pending-update .card-stats .stat-val::after {{
    content: '\2026'; color: var(--muted);
  }}
  .card.pending-update .card-stats .stat-val > * {{ display: none; }}
  .sum-pending {{ color: var(--muted) !important; }}

  /* History rows — clickable */
  .history-row {{ cursor: pointer; }}
  .history-row:hover {{ background: rgba(99,102,241,.08) !important; }}

  /* Run log modal */
  #run-log-modal {{
    display: none; position: fixed; inset: 0; z-index: 999;
    background: rgba(0,0,0,.7); align-items: center; justify-content: center;
  }}
  #run-log-modal.visible {{ display: flex; }}
  #run-log-box {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    width: min(860px, 96vw); max-height: 80vh; display: flex; flex-direction: column;
    box-shadow: 0 24px 64px rgba(0,0,0,.6);
  }}
  #run-log-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px; border-bottom: 1px solid var(--border);
  }}
  #run-log-title {{ font-weight: 600; font-size: 14px; }}
  #run-log-close {{
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 20px; line-height: 1; padding: 0 4px;
  }}
  #run-log-close:hover {{ color: var(--text); }}
  #run-log-pre {{
    flex: 1; overflow-y: auto; padding: 14px 18px;
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px;
    line-height: 1.6; color: #9ca3af; white-space: pre-wrap; word-break: break-all;
    margin: 0;
  }}

  /* Server-offline notice */
  #offline-notice {{
    display: none; background: rgba(99,102,241,.1);
    border: 1px solid rgba(99,102,241,.3); border-radius: 8px;
    padding: 10px 16px; margin-bottom: 20px; font-size: 13px;
    color: #a5b4fc;
  }}
  #offline-notice.visible {{ display: block; }}
</style>
<script>
const API = '';   // same origin – works regardless of hostname

async function checkServer() {{
  try {{
    const r = await fetch(API + '/api/status', {{signal: AbortSignal.timeout(1500)}});
    return r.ok;
  }} catch {{ return false; }}
}}

function setRunning(running, label, cardBtn) {{
  const btn = document.getElementById('run-btn');
  btn.disabled = running;
  if (running) btn.classList.add('running'); else btn.classList.remove('running');
  btn.querySelector('.btn-label').textContent = running ? label : 'Run Updates Now';
  document.querySelectorAll('.card-run-btn').forEach(b => {{ b.disabled = running; }});
  if (cardBtn) {{
    if (running) {{
      cardBtn.dataset.origText = cardBtn.textContent;
      cardBtn.textContent = '↻';
      cardBtn.classList.add('spinning');
    }} else {{
      cardBtn.textContent = cardBtn.dataset.origText || '▶';
      cardBtn.classList.remove('spinning');
    }}
  }}
}}

async function startRun(device, cardBtn) {{
  const panel = document.getElementById('output-panel');
  const log = document.getElementById('output-log');
  const notice = document.getElementById('offline-notice');

  if (!(await checkServer())) {{ notice.classList.add('visible'); return; }}
  notice.classList.remove('visible');

  let resp;
  try {{
    const body = device ? JSON.stringify({{device}}) : null;
    resp = await fetch(API + '/api/run-updates', {{
      method: 'POST',
      headers: body ? {{'Content-Type': 'application/json'}} : {{}},
      body
    }});
  }} catch(e) {{ notice.classList.add('visible'); return; }}

  if (resp.status === 409) {{ alert('An update is already in progress.'); return; }}

  const label = device ? `Updating ${{device}}…` : 'Running…';
  setRunning(true, label, cardBtn);
  log.innerHTML = '';
  panel.classList.add('visible');  startProgressPolling();  // Use rAF so the browser lays out the panel before scrolling to it
  requestAnimationFrame(() => panel.scrollIntoView({{behavior: 'smooth', block: 'start'}}));

  const es = new EventSource(API + '/api/run-updates/stream');
  es.onmessage = (e) => {{
    const data = JSON.parse(e.data);
    if (data.done) {{
      es.close();
      setRunning(false, null, cardBtn);
      const exitOk = data.exit_code === 0;
      appendLine('', '');
      appendLine(exitOk ? '✔ Done. Reloading dashboard…' : '✖ Finished with errors.', exitOk ? 'ok-line' : 'err-line');
      if (exitOk) setTimeout(() => location.reload(), 1800);
      return;
    }}
    if (data.line !== undefined) appendLine(data.line);
  }};
  es.onerror = () => {{
    es.close();
    setRunning(false, null, cardBtn);
    appendLine('Connection lost.', 'err-line');
  }};
}}

async function runNow() {{ startRun(null, null); }}
async function runDevice(name, btn) {{ startRun(name, btn); }}

function appendLine(text, cls) {{
  const log = document.getElementById('output-log');
  const div = document.createElement('div');
  if (cls) div.className = cls;
  else if (text.startsWith('✔') || text.includes('OK') || text.includes('[INFO]')) div.className = 'ok-line';
  else if (text.startsWith('✖') || text.includes('ERROR') || text.includes('Failed')) div.className = 'err-line';
  else if (text.startsWith('⚠') || text.includes('WARN') || text.includes('Reboot')) div.className = 'warn-line';
  else if (text.startsWith('===') || text.startsWith('───')) div.className = 'dim-line';
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}}

function closePanel() {{
  document.getElementById('output-panel').classList.remove('visible');
}}

// ── Run-progress polling ──────────────────────────────────────────────────────
// When a run is active, grey out summary stats and dim device cards that
// haven't been processed yet. Clears automatically when the run finishes.
let _pollTimer = null;

function startProgressPolling() {{
  if (_pollTimer) return;
  _pollTimer = setInterval(pollRunProgress, 3000);
  pollRunProgress(); // immediate first call
}}

function stopProgressPolling() {{
  clearInterval(_pollTimer);
  _pollTimer = null;
  clearPendingState();
}}

async function pollRunProgress() {{
  let status;
  try {{
    const r = await fetch(API + '/api/status', {{signal: AbortSignal.timeout(2000)}});
    if (!r.ok) return;
    status = await r.json();
  }} catch {{ return; }}

  if (!status.running) {{
    stopProgressPolling();
    return;
  }}

  const done = new Set((status.completed_devices || []).map(d => d.name));
  const target = status.target_device || null;

  // Summary stats \u2014 show \u2026 while running
  ['sum-ok','sum-reboot','sum-err','sum-unreachable','sum-pkgs'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.classList.add('sum-pending');
  }});

  // Device cards \u2014 dim only the target device (single-device run) or all not-yet-done (fleet run)
  document.querySelectorAll('.cards .card[data-device]').forEach(card => {{
    const name = card.dataset.device;
    if (target !== null && name !== target) return; // leave other cards alone
    if (done.has(name)) {{
      card.classList.remove('pending-update');
    }} else {{
      card.classList.add('pending-update');
    }}
  }});
}}

function clearPendingState() {{
  ['sum-ok','sum-reboot','sum-err','sum-unreachable','sum-pkgs'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.classList.remove('sum-pending');
  }});
  document.querySelectorAll('.card.pending-update').forEach(c => c.classList.remove('pending-update'));
}}

// ── Run log modal ─────────────────────────────────────────────────────────────
async function showRunLog(row) {{
  const runId = row.dataset.runId;
  if (!runId) return;
  const modal = document.getElementById('run-log-modal');
  const pre   = document.getElementById('run-log-pre');
  const title = document.getElementById('run-log-title');
  title.textContent = 'Run log: ' + runId;
  pre.textContent = 'Loading\u2026';
  modal.classList.add('visible');
  try {{
    const r = await fetch(API + '/api/run-log/' + runId, {{signal: AbortSignal.timeout(5000)}});
    if (!r.ok) {{ pre.textContent = 'Log not available (HTTP ' + r.status + ').'; return; }}
    const data = await r.json();
    const lines = data.stream_log || [];
    pre.textContent = lines.length ? lines.join('\n') : '(No stream log recorded for this run.)';
  }} catch(e) {{
    pre.textContent = 'Failed to load log: ' + e.message;
  }}
}}

function closeRunLog() {{
  document.getElementById('run-log-modal').classList.remove('visible');
}}

// Close modal on backdrop click
document.addEventListener('click', e => {{
  const modal = document.getElementById('run-log-modal');
  if (e.target === modal) closeRunLog();
}});

// Close on Escape
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeRunLog();
}});

// ── Startup: check if run is already in progress ──────────────────────────────
// Check on page load whether a run is already in progress
(async () => {{
  try {{
    const r = await fetch(API + '/api/status', {{signal: AbortSignal.timeout(2000)}});
    if (r.ok) {{
      const s = await r.json();
      if (s.running) startProgressPolling();
    }}
  }} catch {{}}
}})();
</script>
</head>
<body>

<!-- Run log modal -->
<div id="run-log-modal" role="dialog" aria-modal="true">
  <div id="run-log-box">
    <div id="run-log-header">
      <span id="run-log-title">Run log</span>
      <button id="run-log-close" onclick="closeRunLog()" title="Close">&times;</button>
    </div>
    <pre id="run-log-pre"></pre>
  </div>
</div>

<header>
  <div>
    <h1>🖥️ Fleet Status Dashboard</h1>
  </div>
  <div class="subtitle">Generated: {generated_at} &nbsp;·&nbsp; {device_count} devices</div>
  <a href="/admin" class="admin-link">⚙ Admin</a>
  <button class="run-btn" id="run-btn" onclick="runNow()">
    <div class="spinner"></div>
    <span class="btn-icon">▶</span>
    <span class="btn-label">Run Updates Now</span>
  </button>
</header>

<div class="container">

  <div id="offline-notice">
    ⚡ <strong>Dashboard server is not running.</strong>
    Start it with: <code>python3 dashboard-server.py</code> — then click Run Updates Now again.
  </div>

  <div id="output-panel">
    <div class="output-header">
      <span>🔄 Live update output</span>
      <button class="output-close" onclick="closePanel()" title="Close">×</button>
    </div>
    <div id="output-log"></div>
  </div>

  <div class="summary">
    <div class="summary-card"><div class="val" id="sum-total">{total_devices}</div><div class="lbl">Total Devices</div></div>
    <div class="summary-card"><div class="val ok" id="sum-ok">{ok_count}</div><div class="lbl">Up to date</div></div>
    <div class="summary-card"><div class="val warn" id="sum-reboot">{reboot_count}</div><div class="lbl">Reboot needed</div></div>
    <div class="summary-card"><div class="val err" id="sum-err">{error_count}</div><div class="lbl">Errors</div></div>
    <div class="summary-card"><div class="val unreachable" id="sum-unreachable">{unreachable_count}</div><div class="lbl">Unreachable</div></div>
    <div class="summary-card"><div class="val" id="sum-pkgs">{total_pkgs}</div><div class="lbl">Pkgs this run</div></div>
    <div class="summary-card"><div class="val val-sm">{last_run}</div><div class="lbl">Last run</div></div>
  </div>

  <div class="section-title">Devices</div>
  <div class="cards">
{device_cards}
  </div>

  <div class="section-title">Update History (last 10 runs)</div>
  <div class="history">
    <table>
      <thead>
        <tr>
          <th>Run time (UTC)</th>
          <th>Devices</th>
          <th>OK</th>
          <th>Reboot</th>
          <th>Errors</th>
          <th>Pkgs upgraded</th>
          <th>Duration</th>
        </tr>
      </thead>
      <tbody>
{history_rows}
      </tbody>
    </table>
  </div>

</div>
<footer>Fleet Manager &nbsp;·&nbsp; {schedule_desc} &nbsp;·&nbsp; <a href="/admin">Manage →</a></footer>
</body>
</html>"""


def generate():
    runs = load_runs()
    conf = load_fleet_conf()
    conf_devices = conf.get("devices", [])
    latest = latest_per_device(runs)

    # Summary stats (from most recent run)
    last_run_ts = ""
    total_pkgs_last = 0
    if runs:
        last_run = runs[0]
        last_run_ts = fmt_ts(last_run.get("run_timestamp", ""))
        total_pkgs_last = sum(d.get("packages_upgraded", 0) for d in last_run.get("devices", []))

    # Count across latest per device
    statuses = [d.get("status") for d in latest.values()]
    total_devices = max(len(conf_devices), len(latest))
    ok_count          = statuses.count("success")
    reboot_count      = statuses.count("reboot_required") + statuses.count("rebooting")
    error_count       = statuses.count("error")
    unreachable_count = statuses.count("unreachable")
    never_run         = total_devices - len(latest)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sched = conf.get("schedule", {})
    if sched.get("enabled"):
        sched_desc = sched.get("description") or f"Scheduled: {sched.get('cron','?')}"
        schedule_desc = f"Auto-updates enabled · {sched_desc}"
    else:
        schedule_desc = "Auto-updates disabled · <a href='/admin#schedule'>Configure →</a>"

    html = HTML_TEMPLATE.format(
        generated_at   = generated_at,
        device_count   = total_devices,
        total_devices  = total_devices,
        ok_count          = ok_count,
        reboot_count      = reboot_count,
        error_count       = error_count,
        unreachable_count = unreachable_count,
        total_pkgs     = total_pkgs_last,
        last_run       = last_run_ts or "Never",
        device_cards   = device_cards(latest, conf_devices),
        history_rows   = history_rows(runs),
        schedule_desc  = schedule_desc,
    )

    OUTPUT.write_text(html)
    print(f"Dashboard written: {OUTPUT}")
    print(f"  {total_devices} devices · {ok_count} OK · {reboot_count} reboot · {error_count} errors")


if __name__ == "__main__":
    generate()
