#!/bin/bash
# Restore every node's dashboard: bind address AND port are per-node configuration.
#
# What went wrong twice: pushing the dev copy of server.py silently changed (a) the port
# sensor2 needs (8098, because 8099 there is an unrelated container) and (b) the bind address
# every remote node needs (its tailnet IP, so the aggregator can reach it). Both are now
# environment variables set from a systemd drop-in, so this class of push is safe.
set -uo pipefail
STAGE=/root/hp/.geoip-stage
INC="incus --project lumi"
mkdir -p "$STAGE"

# node -> "tailnet IP:port"
declare -A BIND=(
  [sensor1]="127.0.0.1:8099"
  [sensor2]="100.64.0.18:8098"
  [sensor3]="100.64.0.16:8099"
  [sensor4]="100.64.0.14:8099"
  [sensor5]="100.64.0.11:8099"
  [sensor6]="100.64.0.12:8099"
  [sensor7]="100.64.0.15:8099"
  [sensor8]="100.64.0.13:8099"
  [sensor9]="100.64.0.19:8099"
)

gen_server() {  # $1 = node
  python3 - "$1" <<'PY'
import re, sys
node = sys.argv[1]
src = open('/root/cowrie-dashboard/server.py').read()
src = re.sub(r'^SERVER_NAME = "[^"]*"', 'SERVER_NAME = "%s"' % node, src, count=1, flags=re.M)
assert 'SERVER_NAME = "%s"' % node in src
open('/root/hp/.geoip-stage/server-%s.py' % node, 'w').write(src)
PY
}

dropin() {  # $1 = node, $2 = "ip:port"  -> echo the drop-in body
  local ip="${2%%:*}" port="${2##*:}"
  # bind loopback too: harmless, and it keeps anything local (nginx, curl on the box) working
  if [ "$ip" = "127.0.0.1" ]; then
    printf '[Service]\nEnvironment=DASH_PORT=%s\nEnvironment=DASH_HOST=127.0.0.1\n' "$port"
  else
    printf '[Service]\nEnvironment=DASH_PORT=%s\nEnvironment=DASH_HOST=127.0.0.1,%s\n' "$port" "$ip"
  fi
}

echo "=== rolling bind-corrected code to all 9 ==="
for n in sensor1 sensor2 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do
  printf "  %-9s " "$n"
  gen_server "$n"
  spec="${BIND[$n]}"
  if [ "$n" = "sensor1" ]; then
    cp "$STAGE/server-sensor1.py" /root/cowrie-dashboard/server.py
    mkdir -p /etc/systemd/system/cowrie-dashboard.service.d
    dropin sensor1 "$spec" > /etc/systemd/system/cowrie-dashboard.service.d/bind.conf
    systemctl daemon-reload && systemctl restart cowrie-dashboard && sleep 4
    systemctl is-active cowrie-dashboard; continue
  fi
  if [ "$n" = "sensor2" ]; then
    $INC file push "$STAGE/server-sensor2.py" prod-uk:sensor2/root/cowrie-dashboard/server.py >/dev/null 2>&1
    $INC exec prod-uk:sensor2 -- bash -c "mkdir -p /etc/systemd/system/cowrie-dashboard.service.d && printf '[Service]\nEnvironment=DASH_PORT=${spec##*:}\nEnvironment=DASH_HOST=127.0.0.1,${spec%%:*}\n' > /etc/systemd/system/cowrie-dashboard.service.d/bind.conf && systemctl daemon-reload && systemctl restart cowrie-dashboard && sleep 4 && systemctl is-active cowrie-dashboard"
    continue
  fi
  scp -q -o BatchMode=yes -o ConnectTimeout=15 "$STAGE/server-$n.py" root@"$n":/root/cowrie-dashboard/server.py 2>/dev/null \
    || { echo "SCP-FAILED"; continue; }
  dropin "$n" "$spec" > "$STAGE/bind-$n.conf"
  scp -q -o BatchMode=yes "$STAGE/bind-$n.conf" root@"$n":/etc/systemd/system/cowrie-dashboard.service.d/bind.conf.tmp 2>/dev/null || \
    ssh -o BatchMode=yes root@"$n" 'mkdir -p /etc/systemd/system/cowrie-dashboard.service.d' 2>/dev/null
  scp -q -o BatchMode=yes "$STAGE/bind-$n.conf" root@"$n":/etc/systemd/system/cowrie-dashboard.service.d/bind.conf 2>/dev/null
  ssh -o BatchMode=yes root@"$n" 'python3 -c "import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())" && systemctl daemon-reload && systemctl restart cowrie-dashboard && sleep 4 && systemctl is-active cowrie-dashboard' 2>/dev/null || echo "FAILED"
done

echo
echo "=== verify: every node answers the aggregator's exact port ==="
python3 - <<'PY'
import json, urllib.request, time
BIND = {"sensor1":"127.0.0.1:8099","sensor2":"100.64.0.18:8098","sensor3":"100.64.0.16:8099",
        "sensor4":"100.64.0.14:8099","sensor5":"100.64.0.11:8099","sensor6":"100.64.0.12:8099",
        "sensor7":"100.64.0.15:8099","sensor8":"100.64.0.13:8099","sensor9":"100.64.0.19:8099"}
bad = []
for name, host in BIND.items():
    t0 = time.time()
    try:
        with urllib.request.urlopen("http://%s/api/summary" % host, timeout=120) as r:
            d = json.loads(r.read().decode())
        print("  %-9s OK   %5.2fs  events=%s" % (name, time.time()-t0, d.get("total_events")))
    except Exception as e:
        bad.append(name)
        print("  %-9s FAIL %.2fs %s" % (name, time.time()-t0, type(e).__name__))
print("\n  down:", bad or "none")
PY

rm -f "$STAGE/server-"*.py "$STAGE/bind-"*.conf
