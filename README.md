# Fleet Update Manager

A self-hosted tool that keeps your Raspberry Pis, Ubuntu/Debian machines, and Proxmox nodes up to date — automatically, over SSH, from a central machine (or Docker container). A web dashboard shows each device's status, and an admin UI lets you manage your fleet without touching config files.

---

## How It Works

A central machine (or Docker container) SSH-es into each device in your fleet and runs `apt update && apt upgrade`. Results are written to timestamped JSON logs, and a web dashboard is generated from those logs. A built-in scheduler can trigger runs on a cron schedule automatically.

```
┌─────────────────────────────────────────────────┐
│  Fleet Manager  (this machine / Docker)          │
│                                                   │
│  dashboard-server.py  ←→  browser :8484          │
│      │                                            │
│      ├── run-fleet-updates.sh  ──SSH──► user@pi1  │
│      │        │                   └──► root@node1 │
│      │        └── logs/run-*.json                 │
│      │                                            │
│      └── generate-dashboard.py                   │
│               └── fleet-status.html              │
└─────────────────────────────────────────────────┘
```

---

## Quick Start (Docker — recommended)

### Prerequisites
- Docker and Docker Compose installed on the host machine
- SSH access to your fleet devices

### 1. Start the container

```bash
docker compose up -d --build
```

On first run the container automatically:
- Creates a default `fleet.conf` in the Docker volume
- Generates an ed25519 SSH key pair at `/data/.ssh/fleet_key`

### 2. Open the dashboard

Open the dashboard on the host machine running the container:

```
http://<host-ip>:8484        ← status dashboard
http://<host-ip>:8484/admin  ← fleet management
```

> The container uses `network_mode: host` so it can resolve `.local` mDNS hostnames. This means ports are bound directly to the host — replace `<host-ip>` with the IP or hostname of the machine running Docker.

### 3. Add your devices

Go to **Admin → Devices → + Add Device**. For each device you need:
- **Name** — a short label (e.g. `pi1`)
- **Host** — `user@hostname.local` (e.g. `pi@raspberry.local`, `root@pve1.local`)
- **Description** — optional

### 4. Push the SSH key to your devices

Go to **Admin → SSH Keys**. Copy the public key, or click **Run Setup on All Devices** to have the container try to push it automatically.

The setup script tries three methods in order:
1. Fleet key already works — skip (already set up)
2. Your existing personal key (via SSH agent) — use it to `ssh-copy-id` the fleet key
3. Interactive password prompt — prompts you and then installs the key

> **SSH agent passthrough for Docker:** To use your existing personal key inside the container during setup, uncomment the `SSH_AUTH_SOCK` lines in `docker-compose.yml` to forward your host SSH agent.

### 5. Run your first update

Click **Run Updates Now** on the dashboard, or go to **Admin → Schedule** to set a recurring schedule.

---

## Quick Start (local — no Docker)

If you'd rather run directly on a Mac or Linux machine:

```bash
# 1. Generate an SSH key (skip if you already have one)
mkdir -p .ssh
ssh-keygen -t ed25519 -f .ssh/fleet_key -N "" -C "fleet-updater"

# 2. Push the key to your devices
bash setup-ssh-access.sh

# 3. Start the dashboard server
python3 dashboard-server.py

# 4. Open http://localhost:8484
```

No additional Python packages are needed for the basic setup. Install `croniter` if you want the built-in auto-schedule:

```bash
pip3 install croniter
```

---

## File Reference

| File | Purpose |
|------|---------|
| `fleet.conf` | Device list + update settings + schedule config |
| `run-fleet-updates.sh` | Main update script — SSH into each device and run apt |
| `setup-ssh-access.sh` | One-time script to push the SSH key to each device |
| `generate-dashboard.py` | Reads JSON logs and writes `fleet-status.html` |
| `dashboard-server.py` | HTTP server on port 8484: serves the dashboard and admin UI |
| `admin.html` | Admin UI — fleet CRUD, SSH key management, schedule config |
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Compose service: port, volume, restart policy |
| `entrypoint.sh` | Docker startup: initialises `/data`, generates SSH key, starts server |

### Data directory layout

In Docker all persistent data lives in the `fleet-data` Docker volume, mounted at `/data`:

```
/data/                      ← FLEET_DATA_DIR in Docker, SCRIPT_DIR locally
  fleet.conf                ← device list + settings
  fleet-status.html         ← generated dashboard (served at /)
  .ssh/
    fleet_key               ← private key (chmod 600)
    fleet_key.pub           ← public key (copy to devices)
    known_hosts             ← SSH host fingerprints
  logs/
    run-20260401-020000.json
    run-20260408-020000.json
    ...
```

---

## fleet.conf Reference

```json
{
  "devices": [
    {
      "name": "pi1",
      "host": "pi@raspberry.local",
      "description": "Kitchen Pi",
      "enabled": true
    },
    {
      "name": "pve1",
      "host": "root@pve1.local",
      "description": "Proxmox Node 1",
      "enabled": true
    }
  ],
  "settings": {
    "ssh_key_path": ".ssh/fleet_key",
    "ssh_timeout_seconds": 90,
    "reboot_if_required": true,
    "reboot_delay_minutes": 1,
    "apt_options": "-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
  },
  "schedule": {
    "enabled": true,
    "cron": "0 2 * * 0",
    "description": "Every Sunday at 2:00 AM"
  }
}
```

### Device fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique short label for the device |
| `host` | Yes | SSH target in `user@hostname` format |
| `description` | No | Human-readable label shown in the dashboard |
| `enabled` | No | `true` by default; set to `false` to skip without deleting |

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `ssh_key_path` | `.ssh/fleet_key` | Path to private key, relative to the data directory |
| `ssh_timeout_seconds` | `90` | SSH connection timeout per device |
| `reboot_if_required` | `true` | If `true`, schedules a reboot via `shutdown -r` when `/var/run/reboot-required` exists after updates. The dashboard shows **↻ Rebooting…** and automatically clears to **✔ OK** once the device comes back online. |
| `reboot_delay_minutes` | `1` | Minutes to wait before issuing the reboot (`shutdown -r +N`) |
| `apt_options` | see above | Flags passed to `apt-get upgrade` |

### Schedule (cron)

The `schedule.cron` field is a standard 5-field cron expression evaluated in the **server's local time**:

```
┌───────── minute  (0-59)
│ ┌─────── hour    (0-23)
│ │ ┌───── day of month (1-31)
│ │ │ ┌─── month  (1-12)
│ │ │ │ ┌─ day of week  (0-6, 0=Sunday)
│ │ │ │ │
0 2 * * 0    →  Every Sunday at 2:00 AM
0 2 * * *    →  Every night at 2:00 AM
0 3 * * 1    →  Every Monday at 3:00 AM
0 0 1 * *    →  First day of every month at midnight
```

Scheduling requires the `croniter` Python package (automatically installed in Docker). Without it the schedule tab in the admin UI will show a warning and runs must be triggered manually.

---

## Dashboard

The dashboard (`fleet-status.html`) is a static HTML file generated by `generate-dashboard.py` after every successful update run. It is also regenerated whenever you save changes in the admin UI.

### Status meanings

| Badge | Meaning |
|-------|---------|
| ✔ OK | All packages up to date, no reboot required |
| ↻ Reboot needed | Updates applied but reboot is required and `reboot_if_required` is disabled (or the scheduled reboot failed) |
| ↻ Rebooting… | Reboot was successfully scheduled. The dashboard server polls SSH in the background and automatically clears this to ✔ OK once the device comes back online (within 10 minutes). |
| ✖ Error | Update script exited with a non-zero code |
| ⚡ Unreachable | SSH connection failed (exit code 255) — device may be offline |
| ⏳ Never run | Device is in fleet.conf but has never been updated |

Each device card also shows:
- **⬆ New release available** (purple bar) — shown on Ubuntu/Debian devices where `do-release-upgrade` detects a new distro release. The fleet manager will never perform a dist-upgrade automatically; this is informational only.
- **📋 Last update log** — a collapsible section showing the full output of the most recent update run for that device.

### Per-device update button

Each device card has a **▶** button that triggers an update for that device only, with live output streamed directly into the dashboard. While a single-device run is active, only that device's card is dimmed to show it is pending — all other device cards remain unchanged.

### Update history log

The history table at the bottom of the dashboard shows the last 10 runs. Click any row to open a modal showing the full live-stream output that was captured during that run. This lets you review exactly what happened on any past run without digging into the raw JSON log files.

---

## Admin UI (`/admin`)

Four tabs:

**Devices** — Add, edit, enable/disable, or delete devices. Changes are saved to `fleet.conf` immediately and the dashboard is regenerated.

**Settings** — Edit SSH timeout, reboot behaviour, and reboot delay.

**SSH Keys** — View and copy the fleet public key. Generate a new key pair (requires re-running setup on all devices). Run the SSH setup script against all devices or individual ones, with live output streamed in the browser.

**Schedule** — Enable/disable automatic updates, choose from preset schedules or enter a custom cron expression. Displays the next scheduled run time. Changes take effect immediately without restarting the server.

---

## API Endpoints

The dashboard server exposes a simple REST API used by both the dashboard and admin UI:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the generated dashboard HTML |
| `GET` | `/admin` | Serve the admin UI |
| `GET` | `/api/status` | Run state (running, exit code, line count) |
| `POST` | `/api/run-updates` | Trigger a fleet update; body `{"device":"name"}` for one device |
| `GET` | `/api/run-updates/stream` | SSE stream of live update output |
| `GET` | `/api/fleet` | Return full fleet.conf as JSON |
| `POST` | `/api/fleet` | Save fleet.conf (full replacement) |
| `GET` | `/api/ssh/pubkey` | Return the fleet public key |
| `POST` | `/api/ssh/generate` | Generate a new SSH key pair |
| `POST` | `/api/ssh/setup` | Run setup-ssh-access.sh; body `{"devices":["name"]}` or empty for all |
| `GET` | `/api/ssh/setup/stream` | SSE stream of live setup output |
| `GET` | `/api/schedule` | Return schedule config + next run time |
| `POST` | `/api/schedule` | Save schedule config |
| `GET` | `/api/run-log/{run_id}` | Return the captured stream log for a past run (e.g. `run-20260401-020000`) |

---

## Updating a Single Device Manually

From the terminal (outside Docker):

```bash
bash run-fleet-updates.sh pi1
```

From inside Docker:

```bash
docker exec fleet-manager bash run-fleet-updates.sh pi1
```

Or click the **▶** button on the device card in the dashboard.

---

## Proxmox Notes

Proxmox nodes SSH in as `root`. The update script detects this and skips `sudo` — it runs `bash -s` directly. No special configuration is needed.

---

## Troubleshooting

### Device shows ⚡ Unreachable

The SSH connection returned exit code 255 (network-level failure). Check:
- Is the device powered on and reachable? (`ping hostname.local`)
- Is the hostname resolving? Try `ssh user@hostname.local` manually
- Is the SSH service running on the device? (`sudo systemctl status ssh`)

### Device shows ✖ Error

The SSH connection succeeded but the update script failed on the device. The error message is shown in the card. Common causes:
- `dpkg` lock held by another process (a local apt run is happening)
- Disk full
- Broken package state — run `sudo dpkg --configure -a` on the device

### SSH key not accepted

Run `bash setup-ssh-access.sh` (locally) or use **Admin → SSH Keys → Run Setup** (in the browser). If the device requires a password and you're running in Docker without SSH agent forwarding, SSH into the device manually and append the public key to `~/.ssh/authorized_keys`.

### Live output not streaming behind nginx reverse proxy

SSE requires buffering to be disabled and the connection to stay open. In Nginx Proxy Manager, edit the proxy host → **Advanced** tab and add:

```nginx
proxy_read_timeout 600s;
proxy_send_timeout 600s;
proxy_http_version 1.1;
proxy_set_header Connection '';
```

The server already sends `X-Accel-Buffering: no` on all SSE responses, which nginx honours to disable response buffering automatically.

### Dashboard not updating after a run

`generate-dashboard.py` is called automatically at the end of each successful run. If it fails (shown in the live output), you can run it manually:

```bash
python3 generate-dashboard.py          # locally
docker exec fleet-manager python3 /app/generate-dashboard.py   # Docker
```

### Scheduler not working

Make sure `croniter` is installed:

```bash
pip3 install croniter   # local
```

In Docker it is installed automatically. Verify the server log at startup — it prints whether `croniter` is available.

### Container can't reach devices by hostname

The container uses `network_mode: host` and relies on the **host machine's** `avahi-daemon` for `.local` mDNS resolution. The host's D-Bus socket is mounted into the container (`/run/dbus/system_bus_socket`) so `libnss-mdns` inside the container can query the host's Avahi daemon directly.

Requirements on the Docker host:
```bash
sudo apt install avahi-daemon libnss-mdns
sudo systemctl enable --now avahi-daemon
```

If devices are still unreachable:
- Confirm the host can resolve the name: `ping device.local`
- Confirm `avahi-daemon` is running on the host: `systemctl status avahi-daemon`
- On non-Linux hosts (Mac/Windows Docker Desktop), `network_mode: host` is not supported — use static IP addresses in `fleet.conf` instead

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DATA_DIR` | Script directory | Where to read/write fleet.conf, .ssh/, logs/, and fleet-status.html. Set to `/data` automatically by the Docker entrypoint. |

---

## Docker Volume Management

```bash
# View volume contents (data dir)
docker exec fleet-manager ls -la /data

# Backup the volume
docker run --rm -v fleet-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/fleet-backup-$(date +%Y%m%d).tar.gz -C /data .

# Restore from backup
docker run --rm -v fleet-data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/fleet-backup-20260401.tar.gz -C /data

# Rebuild image without losing data
docker compose up -d --build

# Full reset (destroys all data including SSH keys and logs)
docker compose down -v
```
