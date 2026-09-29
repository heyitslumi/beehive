#!/bin/bash
# Finish the 5 nodes the first pass missed, then verify all nine report vitals.
set -uo pipefail
S=/root/hp
PATCH="$S/80-system-endpoint-patch.py"
for n in sensor2 sensor3 sensor5 sensor6 sensor7; do
  printf "=== %-9s " "$n"
  case "$n" in
    sensor2)
      incus file push --project lumi "$PATCH" prod-uk:sensor2/root/hp/80-system-endpoint-patch.py >/dev/null 2>&1
      incus exec --project lumi prod-uk:sensor2 -- bash -c \
        "python3 /root/hp/80-system-endpoint-patch.py /root/cowrie-dashboard/server.py && systemctl restart cowrie-dashboard" 2>&1 | tail -1
      ;;
    *)
      scp -q -o BatchMode=yes "$PATCH" "root@$n:$S/80-system-endpoint-patch.py" 2>/dev/null
      ssh -o BatchMode=yes "root@$n" \
        "python3 $S/80-system-endpoint-patch.py /root/cowrie-dashboard/server.py && systemctl restart cowrie-dashboard" 2>&1 | tail -1
      ;;
  esac
done
sleep 8
echo
echo "=== all nine, through the aggregator ==="
for n in sensor1 sensor2 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do
  printf "  %-9s " "$n"
  curl -s -m 60 "http://127.0.0.1:8099/api/node/$n/system" | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print('FAILED (no json)'); raise SystemExit
if 'error' in d: print('ERROR', str(d.get('error'))[:70]); raise SystemExit
print('cpu=%-5s%% load1=%-5s mem=%s/%sGB %-5s%% disk=%s/%sGB %-5s%% free=%sGB up=%sd' % (
  d.get('cpu_pct'), d.get('load1'), d.get('mem_used_gb'), d.get('mem_total_gb'),
  d.get('mem_pct'), d.get('disk_used_gb'), d.get('disk_total_gb'), d.get('disk_pct'),
  d.get('disk_free_gb'), d.get('uptime_days')))
" 2>/dev/null || echo "FAILED"
done