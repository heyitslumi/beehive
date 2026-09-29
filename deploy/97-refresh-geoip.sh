#!/bin/bash
# Refresh the local geo database and push it to every node.
#
# DB-IP publishes monthly. The dashboard resolves its database with a glob, so the file
# must keep the SAME name across refreshes: a dated name like dbip-city-lite-2026-10.mmdb
# sorts BEFORE dbip-city-lite.mmdb ('-' < '.'), so a dated file would be silently ignored
# and the box would keep serving last month's data forever.
set -uo pipefail
DIR=/root/cowrie-dashboard/geoip
MONTH=$(date +%Y-%m)
TMP="$DIR/.download-$$.gz"
DST="$DIR/dbip-city-lite.mmdb"
URL="https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz"

echo "[$(date -Is)] refreshing geo database for $MONTH"
if ! curl -fsSL --retry 3 --max-time 600 -o "$TMP" "$URL"; then
  echo "  download failed -- keeping the existing database (a stale map beats no map)"
  rm -f "$TMP"
  exit 1
fi
gzip -t "$TMP" || { echo "  corrupt download, aborting"; rm -f "$TMP"; exit 1; }

BEFORE=$(stat -c %s "$DST" 2>/dev/null || echo 0)
gzip -dc "$TMP" > "$DST.new" && mv "$DST.new" "$DST" && chmod 644 "$DST" && rm -f "$TMP"
AFTER=$(stat -c %s "$DST")
echo "  database replaced ($BEFORE -> $AFTER bytes)"
systemctl restart cowrie-dashboard
sleep 3
systemctl is-active cowrie-dashboard | sed 's/^/  dashboard: /'

echo "  pushing to the fleet"
for n in sensor2 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do
  printf "    %-9s " "$n"
  if gzip -c "$DST" | ssh -o BatchMode=yes -o ConnectTimeout=15 root@"$n" \
       'cat > /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.new && mv /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb.new /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb && chmod 644 /root/cowrie-dashboard/geoip/dbip-city-lite.mmdb && systemctl restart cowrie-dashboard && sleep 3 && systemctl is-active cowrie-dashboard' 2>/dev/null; then
    echo
  else
    echo "FAILED (node keeps its previous database)"
  fi
done
echo "[$(date -Is)] done"
