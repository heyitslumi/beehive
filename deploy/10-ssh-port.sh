#!/bin/bash
# Phase 1: free up public port 22 for the honeypot by moving real sshd to $SSH_PORT.
# Safety nets: tailscale SSH (served by tailscaled, independent of sshd) stays up the
# whole time, and sensor1's key is installed first so the new port is testable immediately.
set -euo pipefail
NODE="$1"; BIND_IP="$2"; SSH_PORT="${3:-__ADMIN_SSH_PORT__}"
PUBKEY="$(cat /root/.ssh/id_ed25519.pub 2>/dev/null || cat /root/.ssh/id_rsa.pub 2>/dev/null || true)"
if [ -z "$PUBKEY" ]; then echo "!! no pubkey found on sensor1"; exit 1; fi

echo "### [$NODE] phase 1: move sshd 22 -> $SSH_PORT"

# NB: do NOT pass the pubkey as an ssh argv argument - ssh concatenates argv into a
# single remote command string, so the key arrives split across $2/$3 and the append
# silently no-ops. Ship it as a file.
scp -q -o ConnectTimeout=10 -o StrictHostKeyChecking=no /root/.ssh/id_ed25519.pub root@"$NODE":/tmp/hp-node.pub

ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes root@"$NODE" bash -s -- "$SSH_PORT" <<'REMOTE'
set -euo pipefail
NEWPORT="$1"; KEY="$(cat /tmp/hp-node.pub)"
mkdir -p /root/.hp-backups /root/.ssh
chmod 700 /root/.ssh

# 1. install our key so the new port is testable from sensor1
touch /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
# a file whose last line lacks a trailing newline would glue our key onto it
if [ -s /root/.ssh/authorized_keys ] && [ -n "$(tail -c 1 /root/.ssh/authorized_keys)" ]; then
  printf '\n' >> /root/.ssh/authorized_keys
fi
KBLOB="$(echo "$KEY" | awk '{print $2}')"
grep -qF "$KBLOB" /root/.ssh/authorized_keys || echo "$KEY" >> /root/.ssh/authorized_keys
echo "authorized_keys entries: $(grep -c . /root/.ssh/authorized_keys)"

# 2. back up everything we touch
STAMP="$(date +%Y%m%d-%H%M%S)"
cp -a /etc/ssh/sshd_config "/root/.hp-backups/sshd_config.$STAMP"
[ -d /etc/ssh/sshd_config.d ] && cp -a /etc/ssh/sshd_config.d "/root/.hp-backups/sshd_config.d.$STAMP"
[ -d /etc/systemd/system/ssh.socket.d ] && cp -a /etc/systemd/system/ssh.socket.d "/root/.hp-backups/ssh.socket.d.$STAMP"
echo "backup stamp: $STAMP"

echo "Port directives BEFORE: $(grep -rhiE '^[[:space:]]*Port[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | tr '\n' ' ')"

if systemctl is-active --quiet ssh.socket; then
  # socket-activated: both families must be named or one silently fails to bind
  mkdir -p /etc/systemd/system/ssh.socket.d
  printf '[Socket]\nListenStream=\nListenStream=0.0.0.0:%s\nListenStream=[::]:%s\n' "$NEWPORT" "$NEWPORT" > /etc/systemd/system/ssh.socket.d/listen.conf
  systemctl daemon-reload
  systemctl restart ssh.socket
  echo "restarted ssh.socket"
else
  # sshd listens on EVERY Port directive it can see, so the old one must go
  if grep -rqE '^[[:space:]]*Port[[:space:]]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null; then
    sed -i -E 's/^[[:space:]]*Port[[:space:]]+[0-9]+/# & (moved by honeypot deploy, see 00-honeypot-port.conf)/' /etc/ssh/sshd_config
    for f in /etc/ssh/sshd_config.d/*.conf; do
      [ -e "$f" ] || continue
      sed -i -E 's/^[[:space:]]*Port[[:space:]]+[0-9]+/# & (moved by honeypot deploy)/' "$f"
    done
  fi
  mkdir -p /etc/ssh/sshd_config.d
  printf 'Port %s\n' "$NEWPORT" > /etc/ssh/sshd_config.d/00-honeypot-port.conf
  sshd -t || { echo "!! sshd config test FAILED - rolling back"; cp -a "/root/.hp-backups/sshd_config.$STAMP" /etc/ssh/sshd_config; exit 1; }
  systemctl daemon-reload
  systemctl restart ssh 2>/dev/null || systemctl restart sshd
  echo "restarted ssh.service"
fi

sleep 2
echo "listeners now: $(ss -tln | awk '{print $4}' | grep -E ":(22|$NEWPORT)$" | tr '\n' ' ')"
REMOTE

echo "### [$NODE] verify"
timeout 12 ssh -o ConnectTimeout=8 -o BatchMode=yes root@"$NODE" 'echo "  tailscale-ssh (safety net): OK on $(hostname)"' || echo "  !! tailscale ssh FAILED"
timeout 12 ssh -p "$SSH_PORT" -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes root@"$BIND_IP" 'echo "  sshd on public :'"$SSH_PORT"' -> OK ($(hostname))"' || echo "  !! public :$SSH_PORT unreachable (sshd still reachable via tailscale)"
printf "  public 22: "; timeout 5 nc -zvw3 "$BIND_IP" 22 >/dev/null 2>&1 && echo "STILL TAKEN - honeypot will not bind" || echo "free for the honeypot"
