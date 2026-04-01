#!/bin/bash
# entrypoint.sh – Docker container startup script
# Initialises /data volume on first run, then launches the dashboard server.
set -e

DATA_DIR="/data"

# ── Ensure data subdirectories exist ──────────────────────────────────────────
mkdir -p "$DATA_DIR/.ssh" "$DATA_DIR/logs"
chmod 700 "$DATA_DIR/.ssh"

# ── Create a default fleet.conf if none exists ────────────────────────────────
if [ ! -f "$DATA_DIR/fleet.conf" ]; then
    echo "  [init] Creating default fleet.conf in $DATA_DIR"
    cat > "$DATA_DIR/fleet.conf" << 'EOF'
{
  "_readme": "Add your devices below. Visit /admin to manage your fleet.",
  "devices": [],
  "settings": {
    "ssh_key_path": ".ssh/fleet_key",
    "ssh_timeout_seconds": 90,
    "reboot_if_required": true,
    "reboot_delay_minutes": 1,
    "apt_options": "-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
  },
  "schedule": {
    "enabled": false,
    "cron": "0 2 * * 0",
    "description": "Every Sunday at 2:00 AM"
  }
}
EOF
fi

# ── Generate SSH key pair if missing ──────────────────────────────────────────
if [ ! -f "$DATA_DIR/.ssh/fleet_key" ]; then
    echo "  [init] Generating SSH key pair..."
    ssh-keygen -t ed25519 \
               -f "$DATA_DIR/.ssh/fleet_key" \
               -N "" \
               -C "fleet-updater@$(hostname)" \
               -q
    chmod 600 "$DATA_DIR/.ssh/fleet_key"
    chmod 644 "$DATA_DIR/.ssh/fleet_key.pub"
    echo "  [init] SSH key generated: $DATA_DIR/.ssh/fleet_key.pub"
fi

# ── Create known_hosts if missing ─────────────────────────────────────────────
if [ ! -f "$DATA_DIR/.ssh/known_hosts" ]; then
    touch "$DATA_DIR/.ssh/known_hosts"
    chmod 644 "$DATA_DIR/.ssh/known_hosts"
fi

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Fleet Manager starting on port 8484    ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
echo "  Dashboard  →  http://localhost:8484"
echo "  Admin      →  http://localhost:8484/admin"
echo "  Data dir   →  $DATA_DIR"
echo ""

export FLEET_DATA_DIR="$DATA_DIR"
exec python3 /app/dashboard-server.py "$@"
