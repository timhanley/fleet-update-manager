#!/usr/bin/env bash
# =============================================================================
# setup-ssh-access.sh
# Pushes the fleet SSH public key to each device listed in fleet.conf,
# and optionally configures passwordless sudo for apt on each device.
#
# Run this ONCE from your Mac/PC after filling in fleet.conf with your devices.
# You'll need to enter each device's password once (for the initial ssh-copy-id).
#
# Usage:
#   bash setup-ssh-access.sh              # push key to all devices
#   bash setup-ssh-access.sh pi1 pi2     # push key to specific devices only
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# DATA_DIR: where fleet.conf and .ssh/ keys live.
# In Docker this is /data (from FLEET_DATA_DIR env var); locally defaults to SCRIPT_DIR.
DATA_DIR="${FLEET_DATA_DIR:-$SCRIPT_DIR}"
FLEET_CONF="$DATA_DIR/fleet.conf"
PUB_KEY="$DATA_DIR/.ssh/fleet_key.pub"
SSH_KEY="$DATA_DIR/.ssh/fleet_key"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}✔${NC}  $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
err()   { echo -e "${RED}✖${NC}  $*"; }
title() { echo -e "\n${BOLD}$*${NC}"; }

# ── Checks ────────────────────────────────────────────────────────────────────
if [[ ! -f "$PUB_KEY" ]]; then
  err "Public key not found: $PUB_KEY"
  echo "  Run the setup from the Claude project first to generate the key pair."
  exit 1
fi

if ! command -v ssh-copy-id &>/dev/null; then
  err "ssh-copy-id not found. Install it (brew install openssh on Mac)."
  exit 1
fi

title "Fleet SSH Key Setup"
echo "Public key to push:"
echo "  $(cat "$PUB_KEY")"
echo ""

# Filter args
FILTER_DEVICES=("$@")

# ── Load devices ──────────────────────────────────────────────────────────────
DEVICES_JSON="$(python3 - "$FLEET_CONF" << 'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
for d in data["devices"]:
    if d.get("enabled", True):
        print(f"{d['name']}\t{d['host']}\t{d.get('description','')}")
PYEOF
)"

while IFS=$'\t' read -r name host desc <&3; do
  # Apply filter
  if [[ ${#FILTER_DEVICES[@]} -gt 0 ]]; then
    found=0
    for f in "${FILTER_DEVICES[@]}"; do [[ "$f" == "$name" ]] && found=1; done
    [[ $found -eq 0 ]] && continue
  fi

  title "▶ $name — $host"

  SSH_COMMON_OPTS="-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$DATA_DIR/.ssh/known_hosts"

  # Step 1: fleet key already works — nothing to do
  if ssh -i "$SSH_KEY" -o BatchMode=yes $SSH_COMMON_OPTS "$host" "echo ok" &>/dev/null; then
    info "Fleet key already installed on $name — skipping"

  # Step 2: user's existing key (agent / ~/.ssh/id_*) works — use it to push the fleet key
  elif ssh -o BatchMode=yes $SSH_COMMON_OPTS "$host" "echo ok" &>/dev/null; then
    echo "  Connected with existing key. Installing fleet key..."
    if ssh-copy-id -i "$PUB_KEY" $SSH_COMMON_OPTS "$host" 2>&1; then
      info "SSH key installed on $name"
    else
      err "Failed to install fleet key on $name via existing key."
      continue
    fi

  # Step 3: no key works — try ssh-copy-id interactively (prompts for password)
  else
    echo "  Pushing SSH key (you may be prompted for $host's password)..."
    if ssh-copy-id -i "$PUB_KEY" $SSH_COMMON_OPTS "$host" 2>&1; then
      info "SSH key installed on $name"
    else
      err "Failed to push key to $name."
      echo "  If password authentication is disabled on this device, add the key manually:"
      echo ""
      echo "    echo '$(cat "$PUB_KEY")' >> ~/.ssh/authorized_keys"
      echo ""
      continue
    fi
  fi

  # Test passwordless login
  echo "  Testing key-based login..."
  if ssh -i "$SSH_KEY" \
      -o BatchMode=yes \
      -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$DATA_DIR/.ssh/known_hosts" \
      "$host" "echo ok" &>/dev/null; then
    info "Passwordless SSH login confirmed"
  else
    err "Key login test failed on $name"
    continue
  fi

  # Configure passwordless sudo for apt (if not already set up)
  echo "  Checking sudo configuration..."
  USERNAME="$(echo "$host" | cut -d@ -f1)"

  if [[ "$USERNAME" == "root" ]]; then
    info "Running as root on $name — sudo not needed"
  else
    SUDOERS_LINE="$USERNAME ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /bin/apt-get"

    SUDO_STATUS="$(ssh -i "$SSH_KEY" \
      -o BatchMode=yes -o ConnectTimeout=10 \
      -o UserKnownHostsFile="$DATA_DIR/.ssh/known_hosts" \
      "$host" \
      "sudo -n apt-get --version > /dev/null 2>&1 && echo 'already_ok' || echo 'needs_config'" 2>/dev/null)"

    if [[ "$SUDO_STATUS" == *"already_ok"* ]]; then
      info "Passwordless sudo for apt already configured on $name"
    else
      warn "Passwordless sudo for apt not set up on $name."
      echo ""
      echo "  To fix this, run on $name:"
      echo ""
      echo "    echo '$SUDOERS_LINE' | sudo tee /etc/sudoers.d/fleet-updater"
      echo "    sudo chmod 440 /etc/sudoers.d/fleet-updater"
      echo ""
      echo "  Or to grant full passwordless sudo (common on Raspberry Pi):"
      echo "    echo '$USERNAME ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/fleet-updater"
      echo "    sudo chmod 440 /etc/sudoers.d/fleet-updater"
    fi
  fi

done 3<<< "$DEVICES_JSON"

echo ""
title "Setup complete."
echo "  Test a device manually with:"
echo "    bash run-fleet-updates.sh pi1"
echo ""
