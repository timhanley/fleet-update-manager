# ── Fleet Manager – Dockerfile ───────────────────────────────────────────────
# Keeps your Raspberry Pis and Ubuntu VMs up to date.
# Data (fleet.conf, SSH keys, logs) is stored in the /data volume.
FROM python:3.11-slim

LABEL description="Fleet Update Manager – auto-updates Raspberry Pis and Ubuntu VMs via SSH"

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        bash \
        sshpass \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ───────────────────────────────────────────────────────────
# croniter: powers the built-in cron scheduler
RUN pip install --no-cache-dir croniter

# ── App files ─────────────────────────────────────────────────────────────────
WORKDIR /app
COPY dashboard-server.py \
     generate-dashboard.py \
     run-fleet-updates.sh \
     setup-ssh-access.sh \
     admin.html \
     entrypoint.sh \
     ./

RUN chmod +x entrypoint.sh run-fleet-updates.sh setup-ssh-access.sh

# ── Runtime ───────────────────────────────────────────────────────────────────
# /data is the persistent volume (fleet.conf, .ssh/, logs/, fleet-status.html)
VOLUME ["/data"]

EXPOSE 8484

ENTRYPOINT ["/app/entrypoint.sh"]
