#!/bin/bash
# Fix sensor2: its dashboard binds 8098 (8099 there is an unrelated docker container), so the
# port is now DASH_PORT from the environment instead of a hardcoded constant.
# Rolls the same env-aware code to every node for consistency.
set -uo pipefail
STAGE=/root/hp/.geoip-stage
INC="incus --project lumi"
mkdir -p "$STAGE"

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

echo "=== sensor2 (incus, DASH_PORT=8098) ==="
gen_server sensor2
$INC file push "$STAGE/server-sensor2.py" prod-uk:sensor2/root/cowrie-dashboard/server.py >/dev/null
$INC exec prod-uk:sensor2 -- bash -c '
  systemctl stop cowrie-dashboard
  mkdir -p /etc/systemd/system/cowrie-dashboard.service.d
  printf "[Service]\nEnvironment=DASH_PORT=8098\n" > /etc/systemd/system/cowrie-dashboard.service.d/dash-port.conf
  systemctl daemon-reload
  systemctl start cowrie-dashboard
  sleep 5
  printf "  active: "; systemctl is-active cowrie-dashboard
  printf "  bind:   "; ss -tulpn | grep 8098 | head -1
  printf "  geo:    "; curl -s -o /dev/null -w "%{time_total}s\n" --max-time 90 http://127.0.0.1:8098/api/flat/geo?limit=20
  printf "  summary:"; curl -s --max-time 90 http://127.0.0.1:8098/api/summary | head -c 160; echo
' 2>&1

echo "=== rolling the env-aware code to the other 8 ==="
for n in sensor1 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do
  printf "  %-9s " "$n"
  gen_server "$n"
  if [ "$n" = "sensor1" ]; then
    cp "$STAGE/server-sensor1.py" /root/cowrie-dashboard/server.py && systemctl restart cowrie-dashboard && sleep 4 && systemctl is-active cowrie-dashboard
  else
    scp -q -o BatchMode=yes -o ConnectTimeout=15 "$STAGE/server-$n.py" root@"$n":/root/cowrie-dashboard/server.py 2>/dev/null && \
      ssh -o BatchMode=yes root@"$n" 'python3 -c "import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())" && systemctl restart cowrie-dashboard && sleep 4 && systemctl is-active cowrie-dashboard' 2>/dev/null || echo "FAILED"
  fi
done

rm -f "$STAGE/server-"*.py
echo
echo "=== fleet map sanity: each node answers on the port the aggregator expects ==="
python3 - <<'PY'
import json, urllib.request
NODES = {"sensor1":"127.0.0.1:8099","sensor2":"100.64.0.19:8098","sensor5":"100.64.0.12:8099",
         "sensor6":"100.64.0.12:8099","sensor7":"100.64.0.15:8099","sensor3":"100.64.0.17:8099",
         "sensor4":"100.64.0.14:8099","sensor8":"100.64.0.13:8099","sensor9":"100.64.0.20:8099"}
for name, host in NODES.items():
    try:
        with urllib.request.urlopen("http://%s/api/server-name" % host, timeout=15) as r:
            print("  %-9s %s" % (name, r.read().decode().strip()))
    except Exception:
        try:
            with urllib.request.urlopen("http://%s/api/summary" % host, timeout=20) as r:
                d = json.loads(r.read().decode())
                print("  %-9s answering, total_events=%s" % (name, d.get("total_events")))
        except Exception as e:
            print("  %-9s UNREACHABLE (%s)" % (name, type(e).__name__))
PY
