#!/bin/bash
# Push the local geo database + the patched dashboard to every node, so no node ever
# calls a rate-limited geo API again.
#
# Why the database ships as .gz: 60MB over the tailnet instead of 127MB, decompressed
# on arrival. Why server.py ships per node: SERVER_NAME is baked into the file.
set -uo pipefail
SRC=/root/cowrie-dashboard
DB="$SRC/geoip/dbip-city-lite.mmdb"
STAGE=/root/hp/.geoip-stage
mkdir -p "$STAGE"
gzip -c "$DB" > "$STAGE/dbip-city-lite.mmdb.gz"
echo "staged $(du -h "$STAGE/dbip-city-lite.mmdb.gz" | cut -f1) database"

NODES="sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9"

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

deploy_remote() {  # $1 = node
  local n="$1"
  printf "  %-9s " "$n"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=15 root@"$n" true 2>/dev/null; then
    echo "SSH-FAILED"; return
  fi
  # maxminddb is a distro package; skipping it silently degrades to the HTTP fallback.
  ssh -o BatchMode=yes root@"$n" 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-maxminddb >/dev/null 2>&1; python3 -c "import maxminddb" && echo pkg-ok || echo PKG-MISSING' | sed 's/^/['"$n"'] /'
  ssh -o BatchMode=yes root@"$n" 'mkdir -p /root/cowrie-dashboard/geoip' 2>/dev/null
  scp -q -o BatchMode=yes "$STAGE/dbip-city-lite.mmdb.gz" root@"$n":/root/cowrie-dashboard/geoip/ && \
    ssh -o BatchMode=yes root@"$n" 'gunzip -f /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz && chmod 644 /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb'
  gen_server "$n" >/dev/null
  scp -q -o BatchMode=yes "$STAGE/server-$n.py" root@"$n":/root/cowrie-dashboard/server.py && \
    ssh -o BatchMode=yes root@"$n" 'python3 -c "import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())" && systemctl restart cowrie-dashboard && sleep 3 && systemctl is-active cowrie-dashboard'
  ssh -o BatchMode=yes root@"$n" 'curl -s -o /dev/null -w "geo=%{time_total}s " --max-time 60 http://127.0.0.1:8099/api/flat/geo?limit=20; ls -la /root/cowrie-dashboard/geoip/*.mmdb | awk "{print \$5\" bytes\"}"' 2>/dev/null
  echo
}

echo "=== sensor1 (this node) ==="
systemctl restart cowrie-dashboard; sleep 3; printf "  sensor1    "; systemctl is-active cowrie-dashboard

echo "=== sensor2 (incus) ==="
printf "  sensor2      "
incus file push "$STAGE/dbip-city-lite.mmdb.gz" sensor2/root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz 2>/dev/null || incus exec sensor2 -- mkdir -p /root/cowrie-dashboard/geoip
incus file push "$STAGE/dbip-city-lite.mmdb.gz" sensor2/root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz
gen_server sensor2 >/dev/null
incus file push "$STAGE/server-sensor2.py" sensor2/root/cowrie-dashboard/server.py
incus exec sensor2 -- bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-maxminddb >/dev/null 2>&1; gunzip -f /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.gz; python3 -c "import ast;ast.parse(open(\"/root/cowrie-dashboard/server.py\").read())" && systemctl restart cowrie-dashboard && sleep 3 && systemctl is-active cowrie-dashboard'

echo "=== remote nodes ==="
for n in $NODES; do deploy_remote "$n"; done

rm -f "$STAGE/server-"*.py
echo
echo "=== cleanup: staged per-node code removed (database kept for future pushes) ==="
