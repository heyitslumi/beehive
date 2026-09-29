#!/bin/bash
# Phase 2: deploy cowrie + the stats dashboard on a node.
#   public :22  -> cowrie (bound to the node's interface IP, never 0.0.0.0: the tailnet
#                  and netbird addresses would collide and the bind is a boot-order race)
#   tailnet :8099 -> dashboard JSON API, reached by the sensor1 aggregator
# usage: 20-deploy.sh <node> <bind_ip> <bait_hostname> [dash_port]
set -euo pipefail
NODE="$1"; BIND_IP="$2"; BAIT="$3"; DASH_PORT="${4:-8099}"
STAGE="/tmp/hp-stage"
mkdir -p "$STAGE"

# ---------- stage templated files locally ----------
sed -e "s|^hostname = .*|hostname = ${BAIT}|" \
    -e "s|^internet_facing_ip = .*|internet_facing_ip = ${BIND_IP}|" \
    /root/cowrie/etc/cowrie.cfg > "$STAGE/cowrie.cfg"
cp /root/cowrie/etc/userdb.txt "$STAGE/userdb.txt"

TSIP="$(tailscale ip -4 "$NODE" 2>/dev/null | head -1)"
if [ -z "$TSIP" ]; then echo "!! cannot resolve tailnet IP for $NODE"; exit 1; fi
# bind the DASHBOARD to the tailnet IP only: the aggregator reaches it, the public
# internet does not, and no packet filter is needed to keep it private.
sed -e "s|^SERVER_NAME = \"sensor1\"$|SERVER_NAME = \"${NODE}\"|" \
    -e "s|\"127.0.0.1\", 8099|\"${TSIP}\", ${DASH_PORT}|" \
    /root/cowrie-dashboard/server.py > "$STAGE/server.py"
grep -q "\"${TSIP}\", ${DASH_PORT}" "$STAGE/server.py" || { echo "!! dashboard bind rewrite failed"; exit 1; }

cat > "$STAGE/docker-compose.yml" <<EOF
name: cowrie-honeypot

services:
  cowrie:
    image: cowrie/cowrie:latest
    container_name: cowrie
    restart: unless-stopped
    ports:
      - "${BIND_IP}:22:2222"
    volumes:
      - ./etc/cowrie.cfg:/cowrie/cowrie-git/etc/cowrie.cfg:ro
      - ./etc/userdb.txt:/cowrie/cowrie-git/etc/userdb.txt:ro
      - ./var/log/cowrie:/cowrie/cowrie-git/var/log/cowrie
      - ./var/lib/cowrie:/cowrie/cowrie-git/var/lib/cowrie
    environment:
      - TZ=UTC
EOF

cat > "$STAGE/cowrie-dashboard.service" <<EOF
[Unit]
Description=Cowrie Honeypot Live Stats Dashboard (${NODE})
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/cowrie-dashboard
ExecStart=/usr/bin/python3 /root/cowrie-dashboard/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "### [$NODE] shipping files"
ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes root@"$NODE" \
  'mkdir -p /root/cowrie/etc /root/cowrie/var/log/cowrie /root/cowrie/var/lib/cowrie/downloads /root/cowrie/var/lib/cowrie/state /root/cowrie/var/lib/cowrie/tty /root/cowrie/honeyfs/root /root/cowrie-dashboard'
scp -q -o StrictHostKeyChecking=no \
  "$STAGE/cowrie.cfg" root@"$NODE":/root/cowrie/etc/cowrie.cfg
scp -q -o StrictHostKeyChecking=no \
  "$STAGE/userdb.txt" root@"$NODE":/root/cowrie/etc/userdb.txt
scp -q -o StrictHostKeyChecking=no \
  "$STAGE/docker-compose.yml" root@"$NODE":/root/cowrie/docker-compose.yml
scp -q -o StrictHostKeyChecking=no \
  "$STAGE/server.py" root@"$NODE":/root/cowrie-dashboard/server.py
scp -q -o StrictHostKeyChecking=no \
  "$STAGE/cowrie-dashboard.service" root@"$NODE":/etc/systemd/system/cowrie-dashboard.service
scp -q -o StrictHostKeyChecking=no -r /root/cowrie/honeyfs/root/.env \
  /root/cowrie/honeyfs/root/id_rsa /root/cowrie/honeyfs/root/wallet_backup.txt \
  root@"$NODE":/root/cowrie/honeyfs/root/

# The cowrie image runs as uid 999 but the host-mounted var/ dirs are created by
# root: without this the SSH factory dies with PermissionError and port 22 accepts
# then never answers. (sensor1's working dirs are 999:999.)
echo "### [$NODE] fixing var/ ownership"
ssh -o ConnectTimeout=10 -o BatchMode=yes root@"$NODE" 'chown -R 999:999 /root/cowrie/var && echo "var/ -> 999:999"'

echo "### [$NODE] starting stack"
ssh -o ConnectTimeout=20 -o StrictHostKeyChecking=no -o BatchMode=yes root@"$NODE" bash -s -- "$DASH_PORT" <<'REMOTE'
set -euo pipefail
PORT="$1"; TSIP_REMOTE="$(tailscale ip -4 2>/dev/null | head -1)"
cd /root/cowrie
docker compose config -q && echo "compose config OK"
docker compose up -d 2>&1 | tail -3
systemctl daemon-reload
systemctl enable --now cowrie-dashboard >/dev/null 2>&1 || systemctl restart cowrie-dashboard
sleep 4
echo "cowrie: $(docker ps --filter name=cowrie --format '{{.Status}}')"
echo "bound: $(docker port cowrie 2>/dev/null | tr '\n' ' ')"
echo "listeners: $(ss -tln | awk '{print $4}' | grep -E ":(22|$PORT)$" | tr '\n' ' ')"
for ep in /api/summary "/api/flat/timeseries?hours=6" /api/flat/attackers /api/flat/events /api/flat/geo; do
  printf "  %-32s " "$ep"
  curl -s -o /tmp/r -w "http=%{http_code} " --max-time 25 "http://$TSIP_REMOTE:$PORT$ep"
  python3 -c "import json;d=json.load(open('/tmp/r'));print(type(d).__name__, len(d))" 2>/dev/null || echo "-"
done
REMOTE

echo "### [$NODE] reachable from sensor1 over the tailnet?"
TSIP="$(tailscale ip -4 "$NODE" 2>/dev/null | head -1)"
printf "  http://%s:%s/api/summary -> " "$TSIP" "$DASH_PORT"
timeout 25 curl -s -o /dev/null -w "http=%{http_code}\n" "http://$TSIP:$DASH_PORT/api/summary" || echo "unreachable"
printf "  public :22 -> "; timeout 6 nc -zvw4 "$BIND_IP" 22 >/dev/null 2>&1 && echo "cowrie listening" || echo "NOT listening"
