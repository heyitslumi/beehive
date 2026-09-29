#!/bin/bash
# Restart the aggregator, then check the merged 'all' view against the sum of the parts.
set -uo pipefail
systemctl restart cowrie-dashboard
sleep 5
B=http://127.0.0.1:8099

echo "=== node list (\$all should be first) ==="
curl -s -m 10 "$B/api/node" | head -c 200; echo; echo

python3 - <<'PY'
import json, urllib.request, concurrent.futures as cf
B = "http://127.0.0.1:8099"
def get(path):
    with urllib.request.urlopen(B + path, timeout=40) as r:
        return json.loads(r.read().decode())
nodes = get("/api/node")["nodes"]
fleet = [n for n in nodes if n == "all"]
targets = [n for n in nodes if n != "all"]
print("nodes:", nodes)

with cf.ThreadPoolExecutor(max_workers=10) as ex:
    parts = list(ex.map(lambda n: (n, get("/api/node/%s/summary" % n)), targets))
ok = [(n, p) for n, p in parts if isinstance(p, dict) and "total_events" in p]
print("reporting nodes:", len(ok), "of", len(targets))

keys = ["total_events", "logins_success", "logins_failed", "commands_count", "downloads_count"]
sums = {k: sum(p.get(k, 0) for _, p in ok) for k in keys}
merged = get("/api/node/all/summary")

print("\n%-16s %12s %12s  %s" % ("field", "sum of nodes", "'all' merge", "match"))
bad = 0
for k in keys:
    m = merged.get(k, -1)
    good = (m == sums[k])
    bad += (not good)
    print("%-16s %12d %12d  %s" % (k, sums[k], m, "OK" if good else "MISMATCH"))
print("\nunique attackers (all):", merged.get("unique_ips_count"),
      "| nodes_reporting:", merged.get("nodes_reporting"), "/", merged.get("nodes_total"))
print("window:", merged.get("first_seen"), "->", merged.get("last_seen"))
print("\nRESULT:", "merge is exact" if not bad else "%d field(s) mismatched" % bad)
PY

echo
echo "=== other 'all' endpoints ==="
for ep in "flat/timeseries?hours=168" "flat/attackers?limit=5" "flat/events" "flat/commands" "flat/downloads" "flat/geo?limit=5" "stats" "timeseries"; do
  printf "  %-26s " "$ep"
  curl -s -m 45 "$B/api/node/all/$ep" | head -c 120; echo
done