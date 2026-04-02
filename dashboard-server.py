#!/usr/bin/env python3
"""
dashboard-server.py
Local HTTP server for the fleet status dashboard.

Serves the dashboard at http://localhost:8484 and the admin UI at /admin.
Provides REST API endpoints for fleet management, SSH key management,
schedule configuration, and live update streaming via Server-Sent Events.

Usage:
    python3 dashboard-server.py           # default port 8484
    python3 dashboard-server.py 9090      # custom port

Environment:
    FLEET_DATA_DIR   Path for fleet.conf, .ssh/, logs/ (defaults to script dir)
"""

import datetime
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

# DATA_DIR: where fleet.conf, .ssh/, logs/, fleet-status.html live.
# In Docker this is /data; locally it mirrors SCRIPT_DIR.
DATA_DIR = Path(os.environ.get("FLEET_DATA_DIR", str(SCRIPT_DIR)))

FLEET_CONF_PATH = DATA_DIR / "fleet.conf"
SSH_DIR         = DATA_DIR / ".ssh"
LOG_DIR         = DATA_DIR / "logs"
HTML_PATH       = DATA_DIR / "fleet-status.html"
ADMIN_HTML_PATH = SCRIPT_DIR / "admin.html"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8484

# Optional croniter for scheduling
try:
    from croniter import croniter as _croniter
    HAVE_CRONITER = True
except ImportError:
    HAVE_CRONITER = False


# ── Fleet config helpers ──────────────────────────────────────────────────────
_DEFAULT_CONF = {
    "_readme": "Add your devices below. Run SSH Setup from /admin to push the key.",
    "devices": [],
    "settings": {
        "ssh_key_path": ".ssh/fleet_key",
        "ssh_timeout_seconds": 90,
        "reboot_if_required": True,
        "reboot_delay_minutes": 1,
        "apt_options": "-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold",
    },
    "schedule": {
        "enabled": False,
        "cron": "0 2 * * 0",
        "description": "Every Sunday at 2:00 AM",
    },
}
_conf_lock = threading.Lock()


def load_fleet_conf():
    try:
        conf = json.loads(FLEET_CONF_PATH.read_text())
    except Exception:
        return dict(_DEFAULT_CONF)
    # Back-fill any keys added after initial fleet.conf creation
    conf.setdefault("schedule", {
        "enabled": False,
        "cron": "0 2 * * 0",
        "description": "Every Sunday at 2:00 AM",
    })
    return conf


def save_fleet_conf(conf):
    with _conf_lock:
        FLEET_CONF_PATH.write_text(json.dumps(conf, indent=2))


# ── SSH key helpers ───────────────────────────────────────────────────────────
def get_pubkey():
    pub = SSH_DIR / "fleet_key.pub"
    if pub.exists():
        return pub.read_text().strip()
    return None


def generate_ssh_key():
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    SSH_DIR.chmod(0o700)
    key_path = SSH_DIR / "fleet_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "fleet-updater"],
        check=True, capture_output=True,
    )
    key_path.chmod(0o600)
    (SSH_DIR / "fleet_key.pub").chmod(0o644)
    return get_pubkey()


# ── Shared run state ──────────────────────────────────────────────────────────
class RunState:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.output_lines = []   # list of {"ts": float, "line": str}
        self.exit_code = None
        self.started_at = None
        self.finished_at = None

    def start(self):
        with self._lock:
            self.running = True
            self.output_lines = []
            self.exit_code = None
            self.started_at = time.time()
            self.finished_at = None

    def append(self, line):
        with self._lock:
            self.output_lines.append({"ts": time.time(), "line": line})

    def finish(self, exit_code):
        with self._lock:
            self.running = False
            self.exit_code = exit_code
            self.finished_at = time.time()

    def snapshot(self, since_idx=0):
        with self._lock:
            return (
                self.output_lines[since_idx:],
                len(self.output_lines),
                self.running,
                self.exit_code,
            )


state       = RunState()   # fleet update runs
setup_state = RunState()   # SSH setup runs


# ── Background: fleet update runner ──────────────────────────────────────────
def run_updates(device=None):
    update_script   = SCRIPT_DIR / "run-fleet-updates.sh"
    dashboard_script = SCRIPT_DIR / "generate-dashboard.py"

    state.start()
    label = f"=== Updating {device} ===" if device else "=== Fleet update started ==="
    state.append(label)

    try:
        env = os.environ.copy()
        env["TERM"] = "dumb"

        cmd = ["bash", str(update_script)]
        if device:
            cmd.append(device)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(SCRIPT_DIR), env=env,
        )

        for line in proc.stdout:
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line.rstrip())
            state.append(clean)

        proc.wait()
        exit_code = proc.returncode

        if exit_code == 0:
            state.append("")
            state.append("=== Regenerating dashboard ===")
            result = subprocess.run(
                ["python3", str(dashboard_script)],
                capture_output=True, text=True, cwd=str(SCRIPT_DIR), env=env,
            )
            state.append(result.stdout.strip() or "Dashboard updated.")
            if result.stderr:
                state.append("Warning: " + result.stderr.strip())
        else:
            state.append(f"=== Update script exited with code {exit_code} ===")

        state.finish(exit_code)

    except Exception as e:
        state.append(f"ERROR: {e}")
        state.finish(1)


# ── Background: SSH setup runner ──────────────────────────────────────────────
def run_setup(devices=None):
    setup_script = SCRIPT_DIR / "setup-ssh-access.sh"
    setup_state.start()
    label = (
        f"=== SSH Setup: {', '.join(devices)} ==="
        if devices else
        "=== SSH Setup: all devices ==="
    )
    setup_state.append(label)

    try:
        env = os.environ.copy()
        env["TERM"] = "dumb"

        cmd = ["bash", str(setup_script)]
        if devices:
            cmd.extend(devices)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(SCRIPT_DIR), env=env,
        )
        for line in proc.stdout:
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line.rstrip())
            setup_state.append(clean)

        proc.wait()
        setup_state.finish(proc.returncode)

    except Exception as e:
        setup_state.append(f"ERROR: {e}")
        setup_state.finish(1)


# ── Background: scheduler ─────────────────────────────────────────────────────
_schedule_next_run = None   # ISO string, for /api/schedule response
_schedule_last_triggered = datetime.datetime.now()


def schedule_thread():
    global _schedule_next_run, _schedule_last_triggered
    if not HAVE_CRONITER:
        print("  [schedule] croniter not installed — auto-schedule disabled.")
        print("             Install with: pip3 install croniter")
        return

    while True:
        time.sleep(60)
        try:
            conf = load_fleet_conf()
            sched = conf.get("schedule", {})
            if not sched.get("enabled"):
                _schedule_next_run = None
                continue

            cron_expr = sched.get("cron", "0 2 * * 0")
            now = datetime.datetime.now()

            citer = _croniter(cron_expr, _schedule_last_triggered)
            next_run = citer.get_next(datetime.datetime)
            _schedule_next_run = next_run.isoformat()

            if next_run <= now:
                _schedule_last_triggered = now
                print(f"  [schedule] Triggering scheduled run at {now.strftime('%Y-%m-%d %H:%M')}")
                if not state.running:
                    threading.Thread(target=run_updates, args=(None,), daemon=True).start()
        except Exception as e:
            print(f"  [schedule] Error: {e}")


def next_run_from_cron(cron_expr):
    """Return ISO string of next run for the given cron expression, or None."""
    if not HAVE_CRONITER:
        return None
    try:
        citer = _croniter(cron_expr, datetime.datetime.now())
        return citer.get_next(datetime.datetime).isoformat()
    except Exception:
        return None


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        print(f"  {self.address_string()} {fmt % args}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def send_file(self, path, content_type="text/html; charset=utf-8"):
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content))
        self.send_header("Cache-Control", "no-cache")
        self.send_cors()
        self.end_headers()
        self.wfile.write(content)

    # ── Dashboard ─────────────────────────────────────────────────────────────
    def serve_dashboard(self):
        if not HTML_PATH.exists():
            env = os.environ.copy()
            subprocess.run(
                ["python3", str(SCRIPT_DIR / "generate-dashboard.py")],
                cwd=str(SCRIPT_DIR), env=env,
            )
        self.send_file(HTML_PATH)

    # ── Admin page ────────────────────────────────────────────────────────────
    def serve_admin(self):
        self.send_file(ADMIN_HTML_PATH)

    # ── GET /api/status ───────────────────────────────────────────────────────
    def serve_status(self):
        _, total, running, exit_code = state.snapshot()
        self.send_json({
            "running": running,
            "exit_code": exit_code,
            "total_lines": total,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
        })

    # ── GET /api/run-updates/stream  (SSE) ────────────────────────────────────
    def serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_cors()
        self.end_headers()
        self._stream_state(state)

    # ── GET /api/ssh/setup/stream  (SSE) ──────────────────────────────────────
    def serve_setup_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_cors()
        self.end_headers()
        self._stream_state(setup_state)

    def _stream_state(self, st):
        idx = 0
        try:
            while True:
                new_lines, total, running, exit_code = st.snapshot(idx)
                for item in new_lines:
                    payload = json.dumps({"line": item["line"]})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                idx += len(new_lines)

                if not running and idx >= total and total > 0:
                    result = json.dumps({"done": True, "exit_code": exit_code})
                    self.wfile.write(f"data: {result}\n\n".encode())
                    self.wfile.flush()
                    break

                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── POST /api/run-updates ─────────────────────────────────────────────────
    def trigger_run(self):
        data = self.read_json_body()
        device = (data.get("device") or None) if data else None

        if state.running:
            self.send_error_json("Update already in progress", 409)
            return

        threading.Thread(target=run_updates, args=(device,), daemon=True).start()
        self.send_json({"started": True, "device": device}, 202)

    # ── GET /api/fleet ────────────────────────────────────────────────────────
    def serve_fleet(self):
        self.send_json(load_fleet_conf())

    # ── POST /api/fleet ───────────────────────────────────────────────────────
    def save_fleet(self):
        try:
            conf = self.read_json_body()
            if not isinstance(conf, dict):
                raise ValueError("Expected JSON object")
            # Validate minimal structure
            if "devices" not in conf:
                raise ValueError("Missing 'devices' key")
            save_fleet_conf(conf)
            # Regenerate dashboard with new config
            env = os.environ.copy()
            threading.Thread(
                target=lambda: subprocess.run(
                    ["python3", str(SCRIPT_DIR / "generate-dashboard.py")],
                    cwd=str(SCRIPT_DIR), env=env,
                ),
                daemon=True,
            ).start()
            self.send_json({"saved": True})
        except Exception as e:
            self.send_error_json(str(e))

    # ── GET /api/ssh/pubkey ───────────────────────────────────────────────────
    def serve_pubkey(self):
        key = get_pubkey()
        self.send_json({"pubkey": key, "exists": key is not None})

    # ── POST /api/ssh/generate ────────────────────────────────────────────────
    def trigger_generate_key(self):
        try:
            pubkey = generate_ssh_key()
            self.send_json({"generated": True, "pubkey": pubkey})
        except Exception as e:
            self.send_error_json(str(e))

    # ── POST /api/ssh/setup ───────────────────────────────────────────────────
    def trigger_setup(self):
        data = self.read_json_body()
        devices = data.get("devices") or None  # list of device names, or None=all

        if setup_state.running:
            self.send_error_json("SSH setup already in progress", 409)
            return

        threading.Thread(target=run_setup, args=(devices,), daemon=True).start()
        self.send_json({"started": True, "devices": devices}, 202)

    # ── GET /api/schedule ─────────────────────────────────────────────────────
    def serve_schedule(self):
        conf = load_fleet_conf()
        sched = conf.get("schedule", {"enabled": False, "cron": "0 2 * * 0"})

        next_run = _schedule_next_run
        if not next_run and sched.get("enabled"):
            next_run = next_run_from_cron(sched.get("cron", "0 2 * * 0"))

        self.send_json({
            **sched,
            "next_run": next_run,
            "scheduler_available": HAVE_CRONITER,
        })

    # ── POST /api/schedule ────────────────────────────────────────────────────
    def save_schedule(self):
        global _schedule_last_triggered
        try:
            data = self.read_json_body()
            conf = load_fleet_conf()

            sched = conf.get("schedule", {})
            if "enabled" in data:
                sched["enabled"] = bool(data["enabled"])
            if "cron" in data:
                sched["cron"] = str(data["cron"])
            if "description" in data:
                sched["description"] = str(data["description"])

            conf["schedule"] = sched
            save_fleet_conf(conf)

            # Reset trigger clock so new schedule is evaluated fresh
            _schedule_last_triggered = datetime.datetime.now()

            next_run = next_run_from_cron(sched.get("cron", "0 2 * * 0"))
            self.send_json({"saved": True, "schedule": sched, "next_run": next_run})
        except Exception as e:
            self.send_error_json(str(e))

    # ── Routing ───────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        routes = {
            "/":                          self.serve_dashboard,
            "/index.html":                self.serve_dashboard,
            "/admin":                     self.serve_admin,
            "/admin.html":                self.serve_admin,
            "/api/status":                self.serve_status,
            "/api/run-updates/stream":    self.serve_stream,
            "/api/fleet":                 self.serve_fleet,
            "/api/ssh/pubkey":            self.serve_pubkey,
            "/api/ssh/setup/stream":      self.serve_setup_stream,
            "/api/schedule":              self.serve_schedule,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        routes = {
            "/api/run-updates": self.trigger_run,
            "/api/fleet":       self.save_fleet,
            "/api/ssh/generate": self.trigger_generate_key,
            "/api/ssh/setup":   self.trigger_setup,
            "/api/schedule":    self.save_schedule,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_response(404)
            self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Ensure data directories exist
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    SSH_DIR.chmod(0o700)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  Fleet Dashboard  →  {url}")
    print(f"  Admin UI         →  {url}/admin")
    print(f"  Script dir:  {SCRIPT_DIR}")
    print(f"  Data dir:    {DATA_DIR}")
    print(f"  Scheduler:   {'croniter available' if HAVE_CRONITER else 'croniter not installed (pip3 install croniter)'}")
    print(f"\n  Press Ctrl+C to stop\n")

    # Regenerate dashboard on startup so stale HTML from old builds is replaced
    try:
        subprocess.run(
            ["python3", str(SCRIPT_DIR / "generate-dashboard.py")],
            capture_output=True, text=True,
            env={**os.environ, "FLEET_DATA_DIR": str(DATA_DIR)},
        )
    except Exception as e:
        print(f"  Warning: could not regenerate dashboard on startup: {e}")

    # Start scheduler background thread
    threading.Thread(target=schedule_thread, daemon=True).start()

    # Open browser (only when running locally, not in Docker)
    if not os.environ.get("FLEET_DATA_DIR"):
        def open_browser():
            time.sleep(0.8)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
