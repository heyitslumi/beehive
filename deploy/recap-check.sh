#!/bin/bash
# What changed while you were away?
set -uo pipefail
echo "=== alloy token dropped off yet? ==="
if [ -s /root/hp/alloy-token ]; then echo "  /root/hp/alloy-token exists ($(wc -c < /root/hp/alloy-token) bytes) -> alloy can be deployed now"; else echo "  /root/hp/alloy-token: not there yet"; fi
command -v alloy >/dev/null && echo "  alloy installed on sensor1: yes" || echo "  alloy installed on sensor1: no (nothing deployed yet)"

echo
echo "=== fleet vitals right now ==="
curl -s -m 90 "http://127.0.0.1:8099/api/node/all/system" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('  reporting %s/%s | worst disk %s%% on %s | fleet mem %s/%sGB (%s%%) | free disk %sGB' % (
  d.get('nodes_reporting'), d.get('nodes_total'), d.get('worst_disk_pct'), d.get('worst_disk_node'),
  d.get('total_mem_used_gb'), d.get('total_mem_gb'), d.get('fleet_mem_pct'), d.get('total_disk_free_gb')))
print()
print('  %-9s %6s %6s %8s %8s' % ('node','cpu%','mem%','disk%','free GB'))
for r in d.get('nodes', []):
    flag = '  <-- tight' if (r.get('disk_pct') or 0) >= 75 else ''
    print('  %-9s %6s %6s %7s%% %8s%s' % (r.get('node'), r.get('cpu_pct'), r.get('mem_pct'),
          r.get('disk_pct'), r.get('disk_free_gb'), flag))
"

echo
echo "=== honeypot since the last count (~08:12 it was 265,865) ==="
curl -s -m 90 "http://127.0.0.1:8099/api/node/all/summary" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('  events=%s logins_accepted=%s logins_failed=%s commands=%s payloads=%s attackers=%s-%s' % (
  d.get('total_events'), d.get('logins_success'), d.get('logins_failed'),
  d.get('commands_count'), d.get('downloads_count'), d.get('unique_ips_floor'), d.get('unique_ips_ceiling')))
"

echo
echo "=== services still healthy ==="
systemctl is-active cowrie-dashboard cowrie 2>/dev/null | tr '\n' ' '; echo
docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -E "^(cowrie|beszel) " | head -4