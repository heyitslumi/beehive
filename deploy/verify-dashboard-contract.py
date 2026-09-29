#!/usr/bin/env python3
"""Check the dashboard's field contract: every field/selector a panel reads must exist in
the API response it points at. That is what Infinity resolves at render time, so a missing
key = "No data" in the panel.
"""
import json
import urllib.request

import os
import sys

BASE = os.environ.get("BEEHIVE_API_URL")
if not BASE:
    if len(sys.argv) > 1:
        BASE = sys.argv[1].rstrip("/")
    else:
        print("Usage: python3 deploy/verify-dashboard-contract.py <AGGREGATOR_URL>")
        print("   or: BEEHIVE_API_URL=http://localhost:8000/api/node python3 deploy/verify-dashboard-contract.py")
        print("")
        print("Error: No aggregator URL provided. Set BEEHIVE_API_URL or pass URL as argument 1.")
        sys.exit(1)
PANELS = {
    1: ("all", ["nodes_reporting"]),                       # stat: nodes reporting
    2: ("all", ["worst_disk_pct"]),                        # stat: worst disk
    3: ("all", ["worst_disk_node"]),                       # stat: fullest node
    4: ("all", ["fleet_mem_pct"]),                         # stat: fleet RAM
    5: ("all", ["total_disk_free_gb"]),                    # stat: fleet disk free
    7: ("all", ["max_load1_per_core"]),                    # stat: worst load per core
    8: ("all", ["total_cores"]),                           # stat: fleet cores
}
TABLE_FIELDS = ["node", "cpu_pct", "load1_per_core", "mem_pct", "mem_used_gb", "disk_pct",
                "disk_free_gb", "disk_total_gb", "cpu_cores", "uptime_days", "checked"]


def get(node, ep="system"):
    url = "%s/%s/%s" % (BASE, node, ep)
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.loads(r.read().decode())


fail = 0
cache = {}
for pid, (node, fields) in PANELS.items():
    d = cache.setdefault(node, get(node))
    for f in fields:
        ok = f in d
        fail += not ok
        print("  panel %-2s /%s/  ->  %s" % (pid, f, "present" if ok else "MISSING"))

rows = cache["all"].get("nodes") or []
print("\n  table (panel 6): %d rows from root_selector 'nodes'" % len(rows))
missing_cols = set()
for r in rows:
    for c in TABLE_FIELDS:
        if c not in r:
            missing_cols.add(c)
fail += len(missing_cols)
print("  table columns -> %s" % ("all present" if not missing_cols
                                 else "MISSING: %s" % sorted(missing_cols)))

# the dropdown must work for a single node too, on the panels that keep ${node}
print("\n  per-node variant (dropdown set to a single node):")
for n in ("sensor1", "sensor9", "sensor8"):
    d = get(n)
    print("    %-8s node=%-8s cpu=%s%% mem=%s%% disk=%s%%" % (
        n, d.get("node"), d.get("cpu_pct"), d.get("mem_pct"), d.get("disk_pct")))

print("\n  RESULT:", "panel contract satisfied" if not fail else "%d problem(s)" % fail)
