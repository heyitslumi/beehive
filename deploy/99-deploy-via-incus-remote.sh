#!/bin/bash
# sensor2 deploy via the prod-uk incus remote (no ssh: port 22 there is the cowrie trap,
# __ADMIN_SSH_PORT__ needs host-key trust, and the incus remote is already OIDC-authenticated).
set -uo pipefail
STAGE=/root/hp/.geoip-stage
INC="incus --project lumi"
mkdir -p "$STAGE"

# database: push the .gz and unpack inside the instance (60MB instead of 127MB)
[ -s "$STAGE/dbip-city-lite.mmdb.gz" ] || gzip -c /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb > "$STAGE/dbip-city-lite.mmdb.gz"

python3 - <<'PY'
import re
src = open('/root/cowrie-dashboard/server.py').read()
src = re.sub(r'^SERVER_NAME = "[^"]*"', 'SERVER_NAME = "sensor2"', src, count=1, flags=re.M)
assert 'SERVER_NAME = "sensor2"' in src
open('/root/hp/.geoip-stage/server-sensor2.py','w').write(src)
print("  staged server.py (SERVER_NAME=sensor2)")
PY

echo "  mkdir + push files"
$INC exec prod-uk:sensor2 -- mkdir -p /root/cowrie-dashboard/geoip >/dev/null 2>&1
$INC file push "$STAGE/dbip-city-lite.mmdb.gz" prod-uk:sensor2/root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz && echo "    db pushed"
$INC file push "$STAGE/server-sensor2.py"          prod-uk:sensor2/root/cowrie-dashboard/server.py && echo "    code pushed"
$INC file push "$STAGE/ipinfo.token"            prod-uk:sensor2/root/cowrie-dashboard/ipinfo.token && echo "    token pushed"

echo "  install + restart"
$INC exec prod-uk:sensor2 -- bash -c '
  chmod 600 /root/cowrie-dashboard/ipinfo.token
  gunzip -f /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz
  chmod 644 /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-maxminddb >/dev/null 2>&1
  python3 -c "import maxminddb" || echo "  WARNING: maxminddb missing"
  python3 -c "import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())" || exit 1
  systemctl restart cowrie-dashboard
  sleep 4
  systemctl is-active cowrie-dashboard
' 2>&1 | sed 's/^/    /'

echo "  verify (sensor2's dashboard listens on 8098)"
$INC exec prod-uk:sensor2 -- bash -c '
  printf "    db="; ls -la /root/cowrie-dashboard/geoip/*.mmdb | awk "{print \$5\" bytes\"}"
  printf "    geo="; curl -s -o /dev/null -w "%{time_total}s\n" --max-time 60 http://127.0.0.1:8098/api/flat/geo?limit=20
  printf "    cache="; python3 -c "import json,os;p=\"/root/cowrie-dashboard/geo-enrich.json\";print(len(json.load(open(p))) if os.path.exists(p) else 0)"
' 2>&1

rm -f "$STAGE/server-sensor2.py"
echo "  done"
