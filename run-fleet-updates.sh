#!/usr/bin/env bash
# =============================================================================
# run-fleet-updates.sh
# Connects to each device in fleet.conf via SSH, runs apt updates,
# and writes a timestamped JSON log to logs/.
#
# Usage:
#   bash run-fleet-updates.sh              # update all enabled devices
#   bash run-fleet-updates.sh pi1 pi2     # update specific devices only
# =============================================================================

set -euo pipefail

# ── Locate workspace (works regardless of session name) ──────────────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# DATA_DIR: where fleet.conf, .ssh/, logs/ live.
# In Docker this is /data (from FLEET_DATA_DIR env var); locally defaults to SCRIPT_DIR.
DATA_DIR="${FLEET_DATA_DIR:-$SCRIPT_DIR}"
FLEET_CONF="$DATA_DIR/fleet.conf"
LOG_DIR="$DATA_DIR/logs"
SSH_KEY="$DATA_DIR/$(python3 -c "import json; d=json.load(open('$FLEET_CONF')); print(d['settings']['ssh_key_path'])")"
SSH_TIMEOUT="$(python3 -c "import json; d=json.load(open('$FLEET_CONF')); print(d['settings']['ssh_timeout_seconds'])")"
APT_OPTS="$(python3 -c "import json; d=json.load(open('$FLEET_CONF')); print(d['settings']['apt_options'])")"
REBOOT_IF_REQ="$(python3 -c "import json; d=json.load(open('$FLEET_CONF')); print(str(d['settings']['reboot_if_required']).lower())")"
REBOOT_DELAY="$(python3 -c "import json; d=json.load(open('$FLEET_CONF')); print(d['settings']['reboot_delay_minutes'])")"

mkdir -p "$LOG_DIR"
chmod 700 "$SCRIPT_DIR/.ssh" 2>/dev/null || true
chmod 600 "$SSH_KEY" 2>/dev/null || true

RUN_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/run-${RUN_ID}.json"
RUN_START="$(date +%s)"

# Filter argument (optional: specific device names)
FILTER_DEVICES=("$@")

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}✔${NC}  $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
err()   { echo -e "${RED}✖${NC}  $*"; }
title() { echo -e "\n${BOLD}${CYAN}$*${NC}"; }

# ── Remote update script (runs on each device via SSH) ───────────────────────
read -r -d '' REMOTE_SCRIPT << 'REMOTE_EOF' || true
set -e
export DEBIAN_FRONTEND=noninteractive

# Update package lists
apt-get update -qq 2>&1 | tail -3

# Count upgradable packages (after update so lists are fresh)
BEFORE=$(apt-get -qq --just-print dist-upgrade 2>/dev/null | grep '^Inst' | wc -l)

# Capture the upgrade plan (shows packages + dependency context) before running
UPGRADE_PLAN=$(apt-get -s dist-upgrade 2>/dev/null \
  | grep -E "^(Inst|Remv)" \
  | sed 's|^Inst |  [install/upgrade] |; s|^Remv |  [remove]           |' \
  || true)

# Run the upgrade (dist-upgrade handles packages that require new deps or new installs,
# such as kernel updates; it does NOT upgrade the distro release)
apt-get dist-upgrade -y \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  2>&1 | grep -E "^(Setting up|Preparing to unpack|Unpacking|Removing|Reading state|Building dependency|Get:|Fetched|[0-9]+ upgraded)"

# Autoremove orphaned packages
AUTOREMOVE_OUT=$(apt-get autoremove -y -qq 2>&1 | grep -v "^$" || true)

# Collect stats
REBOOT_REQ="false"
REBOOT_PKGS=""
if [ -f /var/run/reboot-required ]; then
  REBOOT_REQ="true"
  REBOOT_PKGS=$(cat /var/run/reboot-required.pkgs 2>/dev/null | tr '\n' ',' | sed 's/,$//' || echo "")
fi

UPTIME=$(uptime -p 2>/dev/null || uptime)
KERNEL=$(uname -r)
DISK=$(df -h / 2>/dev/null | awk 'NR==2{print $3"/"$2" ("$5")"}' || echo "unknown")
OS=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo "Unknown")
ARCH=$(uname -m)
AFTER=$(apt-get -qq --just-print dist-upgrade 2>/dev/null | grep '^Inst' | wc -l)
UPGRADED=$((BEFORE - AFTER))
if [ "$UPGRADED" -lt 0 ]; then UPGRADED=0; fi

# Check for a new distro release (Ubuntu/Debian only; silently skipped elsewhere)
DISTRO_UPGRADE=""
if command -v do-release-upgrade &>/dev/null; then
  DISTRO_UPGRADE=$(do-release-upgrade -c 2>/dev/null | grep -i "new release" | head -1 || true)
fi

printf '\n__FLEET_STATS_BEGIN__\n'
printf 'packages_upgraded=%d\n'  "$UPGRADED"
printf 'reboot_required=%s\n'    "$REBOOT_REQ"
printf 'reboot_packages=%s\n'    "$REBOOT_PKGS"
printf 'uptime=%s\n'             "$UPTIME"
printf 'kernel=%s\n'             "$KERNEL"
printf 'disk=%s\n'               "$DISK"
printf 'os=%s\n'                 "$OS"
printf 'arch=%s\n'               "$ARCH"
printf 'distro_upgrade=%s\n'     "$DISTRO_UPGRADE"
printf '__UPGRADE_PLAN_BEGIN__\n'
printf '%s\n'                    "$UPGRADE_PLAN"
printf '__UPGRADE_PLAN_END__\n'
printf '__AUTOREMOVE_BEGIN__\n'
printf '%s\n'                    "$AUTOREMOVE_OUT"
printf '__AUTOREMOVE_END__\n'
printf '__FLEET_STATS_END__\n'
REMOTE_EOF

# ── SSH helper ────────────────────────────────────────────────────────────────
ssh_run() {
  local host="$1"
  local user="${host%%@*}"
  local run_cmd; [[ "$user" == "root" ]] && run_cmd="bash -s" || run_cmd="sudo bash -s"
  ssh \
    -i "$SSH_KEY" \
    -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout="$SSH_TIMEOUT" \
    -o BatchMode=yes \
    -o ServerAliveInterval=15 \
    -o UserKnownHostsFile="$DATA_DIR/.ssh/known_hosts" \
    "$host" \
    "$run_cmd" <<< "$REMOTE_SCRIPT"
}

# ── Parse stats block from SSH output ────────────────────────────────────────
parse_stat() {
  local output="$1" key="$2"
  echo "$output" \
    | sed -n '/__FLEET_STATS_BEGIN__/,/__FLEET_STATS_END__/p' \
    | grep "^${key}=" \
    | cut -d= -f2- \
    | head -1
}

# ── Load devices from fleet.conf ──────────────────────────────────────────────
DEVICES_JSON="$(python3 - "$FLEET_CONF" << 'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
for d in data["devices"]:
    if d.get("enabled", True):
        print(f"{d['name']}\t{d['host']}\t{d.get('description','')}")
PYEOF
)"

# ── Main loop ─────────────────────────────────────────────────────────────────
RESULTS_JSON="[]"

title "Fleet Update Run — $RUN_TIMESTAMP"
echo "──────────────────────────────────────────────────"

while IFS=$'\t' read -r name host desc; do
  # Apply filter if specific devices were requested
  if [[ ${#FILTER_DEVICES[@]} -gt 0 ]]; then
    found=0
    for f in "${FILTER_DEVICES[@]}"; do
      [[ "$f" == "$name" ]] && found=1
    done
    [[ $found -eq 0 ]] && continue
  fi

  echo -e "\n${BOLD}▶ $name${NC} ($host)${desc:+ — $desc}"

  DEV_START="$(date +%s)"
  STATUS="success"
  ERROR_MSG=""
  PACKAGES_UPGRADED=0
  REBOOT_REQUIRED="false"
  REBOOT_PACKAGES=""
  UPTIME_VAL=""
  KERNEL_VAL=""
  DISK_VAL=""
  OS_VAL=""
  ARCH_VAL=""
  DISTRO_UPGRADE_VAL=""
  OUTPUT=""

  if OUTPUT="$(ssh_run "$host" 2>&1)"; then
    PACKAGES_UPGRADED="$(parse_stat "$OUTPUT" "packages_upgraded")"
    REBOOT_REQUIRED="$(parse_stat "$OUTPUT" "reboot_required")"
    REBOOT_PACKAGES="$(parse_stat "$OUTPUT" "reboot_packages")"
    UPTIME_VAL="$(parse_stat "$OUTPUT" "uptime")"
    KERNEL_VAL="$(parse_stat "$OUTPUT" "kernel")"
    DISK_VAL="$(parse_stat "$OUTPUT" "disk")"
    OS_VAL="$(parse_stat "$OUTPUT" "os")"
    ARCH_VAL="$(parse_stat "$OUTPUT" "arch")"
    DISTRO_UPGRADE_VAL="$(parse_stat "$OUTPUT" "distro_upgrade")"

    PACKAGES_UPGRADED="${PACKAGES_UPGRADED:-0}"

    if [[ "$REBOOT_REQUIRED" == "true" ]]; then
      STATUS="reboot_required"
      warn "Updated $PACKAGES_UPGRADED package(s). Reboot required."
      if [[ "$REBOOT_IF_REQ" == "true" ]]; then
        warn "Scheduling reboot in ${REBOOT_DELAY}m on $name ..."
        reboot_user="${host%%@*}"
        [[ "$reboot_user" == "root" ]] && sudo_prefix="" || sudo_prefix="sudo "
        if ssh -i "$SSH_KEY" \
          -o BatchMode=yes \
          -o StrictHostKeyChecking=accept-new \
          -o UserKnownHostsFile="$DATA_DIR/.ssh/known_hosts" \
          "$host" \
          "${sudo_prefix}shutdown -r +${REBOOT_DELAY} 'Scheduled reboot after automatic updates'" 2>&1; then
          STATUS="rebooting"
        else
          warn "Could not schedule reboot on $name (non-fatal)"
        fi
      fi
    else
      info "Updated $PACKAGES_UPGRADED package(s). No reboot needed."
    fi
  else
    SSH_EXIT=$?
    ERROR_MSG="$(echo "$OUTPUT" | tail -5 | tr '"' "'" | tr '\n' ' ')"
    if [ "$SSH_EXIT" -eq 255 ]; then
      STATUS="unreachable"
      err "Unreachable (SSH exit 255): $ERROR_MSG"
    else
      STATUS="error"
      err "Failed (exit $SSH_EXIT): $ERROR_MSG"
    fi
  fi

  DEV_END="$(date +%s)"
  DEV_DURATION="$((DEV_END - DEV_START))"

  # Safely escape values for JSON
  safe_json() { printf '%s' "$1" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))"; }

  # Build device JSON — pass full SSH output to Python for parsing
  RESULT_JSON="$(FLEET_SSH_OUT="$OUTPUT" python3 << PYEOF
import json, sys, os

raw = os.environ.get('FLEET_SSH_OUT', '')
lines = raw.splitlines()

# Extract the visible apt output (before stats block)
output_log = []
in_stats = False
for l in lines:
    if l == '__FLEET_STATS_BEGIN__':
        in_stats = True
    elif l == '__FLEET_STATS_END__':
        in_stats = False
    elif not in_stats and l.strip():
        output_log.append(l)

# Extract a named sub-block from within the stats section
def extract_block(start_marker, end_marker):
    capturing = False
    result = []
    for l in lines:
        if l == start_marker:
            capturing = True
        elif l == end_marker:
            break
        elif capturing and l.strip():
            result.append(l)
    return result

upgrade_plan  = extract_block('__UPGRADE_PLAN_BEGIN__', '__UPGRADE_PLAN_END__')
autoremove    = extract_block('__AUTOREMOVE_BEGIN__',   '__AUTOREMOVE_END__')

print(json.dumps({
    "name":               $(safe_json "$name"),
    "host":               $(safe_json "$host"),
    "description":        $(safe_json "$desc"),
    "status":             "$STATUS",
    "packages_upgraded":  int("${PACKAGES_UPGRADED:-0}" or 0),
    "reboot_required":    "$REBOOT_REQUIRED" == "true",
    "reboot_packages":    [p for p in $(safe_json "$REBOOT_PACKAGES").split(",") if p],
    "os":                 $(safe_json "$OS_VAL"),
    "kernel":             $(safe_json "$KERNEL_VAL"),
    "arch":               $(safe_json "$ARCH_VAL"),
    "uptime":             $(safe_json "$UPTIME_VAL"),
    "disk":               $(safe_json "$DISK_VAL"),
    "duration_seconds":   $DEV_DURATION,
    "error":              $(safe_json "$ERROR_MSG") if "$STATUS" == "error" else None,
    "distro_upgrade":     $(safe_json "$DISTRO_UPGRADE_VAL"),
    "upgrade_plan":       upgrade_plan,
    "autoremove_log":     autoremove,
    "output_log":         output_log,
}))
PYEOF
)"

  RESULTS_JSON="$(PREV_JSON="$RESULTS_JSON" python3 -c "
import json, os, sys
r = json.loads(os.environ['PREV_JSON'])
r.append(json.loads(sys.argv[1]))
print(json.dumps(r))
" "$RESULT_JSON")"

  # Signal to dashboard-server that this device is done (stripped from UI output)
  echo "__FLEET_DEVICE_DONE__:${name}:${STATUS}"

done <<< "$DEVICES_JSON"

# ── Write run log ─────────────────────────────────────────────────────────────
RUN_END="$(date +%s)"
RUN_DURATION="$((RUN_END - RUN_START))"

FLEET_RESULTS="$RESULTS_JSON" python3 - << PYEOF
import json, os
data = {
    "run_id":           "$RUN_ID",
    "run_timestamp":    "$RUN_TIMESTAMP",
    "duration_seconds": $RUN_DURATION,
    "devices":          json.loads(os.environ["FLEET_RESULTS"]),
}
with open("$LOG_FILE", "w") as f:
    json.dump(data, f, indent=2)
print(f"Log saved: $LOG_FILE")
PYEOF

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────────"
FLEET_RESULTS="$RESULTS_JSON" python3 - << PYEOF
import json, os
devices = json.loads(os.environ["FLEET_RESULTS"])
ok      = [d for d in devices if d["status"] == "success"]
reboot  = [d for d in devices if d["status"] == "reboot_required"]
errors  = [d for d in devices if d["status"] == "error"]
total_pkgs = sum(d["packages_upgraded"] for d in devices)

print(f"  Devices:   {len(devices)} total  ·  {len(ok)} OK  ·  {len(reboot)} need reboot  ·  {len(errors)} errors")
print(f"  Packages:  {total_pkgs} upgraded across the fleet")
print(f"  Duration:  $RUN_DURATION seconds")
if reboot:
    print(f"  Rebooting: {', '.join(d['name'] for d in reboot)}")
if errors:
    print(f"  Failed:    {', '.join(d['name'] for d in errors)}")
PYEOF
echo ""

echo "Run $RUN_ID complete. Log: $LOG_FILE"

# ── Regenerate dashboard ───────────────────────────────────────────────────────
python3 "$SCRIPT_DIR/generate-dashboard.py" 2>&1 || warn "Dashboard regeneration failed (non-fatal)"
