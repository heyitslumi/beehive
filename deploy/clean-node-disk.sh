#!/bin/bash
# Reclaim space on sensor8: journal + package caches only. Nothing user-level, no docker images
# (both are in use: cowrie and the globalping probe), cowrie's own logs untouched.
set -uo pipefail
timeout 300 ssh -o BatchMode=yes root@sensor8 'bash -s' <<'REMOTE' 2>&1 | tail -25
set -uo pipefail
echo "=== before ==="
df -h / | tail -1

echo
echo "=== 1. cap journald permanently so it cannot regrow to 900MB ==="
if ! grep -q '^SystemMaxUse=' /etc/systemd/journald.conf; then
  sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
  grep -q '^SystemMaxUse=' /etc/systemd/journald.conf || echo 'SystemMaxUse=200M' >> /etc/systemd/journald.conf
fi
grep -E '^SystemMaxUse=' /etc/systemd/journald.conf
journalctl --vacuum-size=200M 2>&1 | tail -2
systemctl restart systemd-journald

echo
echo "=== 2. package caches (re-downloadable, nothing lost) ==="
apt-get clean
rm -rf /var/lib/apt/lists/*
apt-get update -qq >/dev/null 2>&1 || true

echo
echo "=== after ==="
df -h / | tail -1
echo
echo "=== sanity: services still fine ==="
systemctl is-active cowrie-dashboard alloy beszel-agent 2>/dev/null | tr '\n' ' '
docker ps --format '{{.Names}} {{.Status}}' | head -3
echo
echo "=== cowrie logs untouched ==="
du -sh /root/cowrie/var/log/cowrie /root/cowrie/var/lib/cowrie 2>/dev/null
REMOTE