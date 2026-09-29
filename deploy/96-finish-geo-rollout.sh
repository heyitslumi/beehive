#!/bin/bash
# Finish the geo rollout:
#  1. sensor2 -- it is a tailnet ssh host (it lives on prod-uk), NOT a local incus VM.
#     The earlier script tried the local incus CLI and failed.
#  2. push the ipinfo token + the newer server.py (enrichment budget) to all 9.
set -uo pipefail
STAGE=/root/hp/.geoip-stage
mkdir -p "$STAGE"
cp /root/cowrie-dashboard/ipinfo.token "$STAGE/ipinfo.token"

gen_server() {  # $1 = node name
  python3 - "$1" <<'PY'
import re, sys
node = sys.argv[1]
src = open('/root/cowrie-dashboard/server.py').read()
src = re.sub(r'^SERVER_NAME = "[^"]*"', 'SERVER_NAME = "%s"' % node, src, count=1, flags=re.M)
assert 'SERVER_NAME = "%s"' % node in src, 'SERVER_NAME not rewritten'
open('/root/hp/.geoip-stage/server-%s.py' % node, 'w').write(src)
PY
}

push() {  # $1 = node, $2 = dashboard port
  local n="$1" port="$2"
  printf "  %-9s " "$n"
  gen_server "$n" >/dev/null
  scp -q -o BatchMode=yes "$STAGE/ipinfo.token" root@"$n":/root/cowrie-dashboard/ipinfo.token && \
    ssh -o BatchMode=yes root@"$n" 'chmod 600 /root/cowrie-dashboard/ipinfo.token'
  scp -q -o BatchMode=yes "$STAGE/server-$n.py" root@"$n":/root/cowrie-dashboard/server.py && \
    ssh -o BatchMode=yes root@"$n" "python3 -c 'import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())' && systemctl restart cowrie-dashboard && sleep 4 && systemctl is-active cowrie-dashboard" | tr -d '\n'
  printf " | "
  ssh -o BatchMode=yes root@"$n" "curl -s -o /dev/null -w 'geo=%{time_total}s' --max-time 60 http://127.0.0.1:$port/api/flat/geo?limit=20; echo -n ' token='; test -s /root/cowrie-dashboard/ipinfo.token && echo -n yes || echo -n NO; echo -n ' maxmind='; python3 -c 'import maxminddb' 2>/dev/null && echo yes || echo NO" 2>/dev/null
  echo
}

for n in sensor1 sensor2 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do
  case "$n" in
    sensor1) push sensor1 8099 ;;
    sensor2)   push sensor2 8098 ;;
    *)      push "$n" 8099 ;;
  esac
done

rm -f "$STAGE/server-"*.py
echo
echo "=== node enrichment caches (should start growing within a minute) ==="
sleep 75
for n in sensor1 sensor2 sensor8 sensor9; do
  printf "  %-9s " "$n"
  ssh -o BatchMode=yes root@"$n" 'python3 -c "
import json,os
p=\"/root/cowrie-dashboard/geo-enrich.json\"
print(len(json.load(open(p))), \"IPs cached\") if os.path.exists(p) else print(\"no cache yet\")
"' 2>/dev/null
done
