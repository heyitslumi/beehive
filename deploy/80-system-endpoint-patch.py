#!/usr/bin/env python3
"""Add a /api/system host-vitals endpoint to a cowrie dashboard's server.py.

Reads /proc directly, so every node reports CPU/RAM/disk/load with no agent to install.
Surgical and idempotent: only inserts the helper and one route, leaving each node's
SERVER_NAME / port / bind exactly as deployed.

Usage: 80-system-endpoint-patch.py <path-to-server.py>
"""
import os
import re
import shutil
import sys
import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/cowrie-dashboard/server.py"
src = open(PATH, encoding="utf-8").read()

if "def get_system(" in src:
    print("already patched -> %s" % PATH)
    sys.exit(0)

HELPER = '''
def get_system():
    """Host vitals for the fleet view. No agent, no exporter: straight from /proc.

    cpu_pct needs two samples, so it costs a 0.25s sleep per request; everything else is
    a single read. Counters that only make sense as a rate (network bytes) are reported as
    cumulative totals and are not charted, because this endpoint keeps no history.
    """
    def cpu_times():
        with open("/proc/stat") as fh:
            vals = [int(v) for v in fh.readline().split()[1:9]]
        return sum(vals), vals[3] + vals[4]          # total, idle+iowait

    t1, i1 = cpu_times()
    time.sleep(0.25)
    t2, i2 = cpu_times()
    dt, di = t2 - t1, i2 - i1
    cpu_pct = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0

    mem = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, val = line.partition(":")
            mem[key.strip()] = int(val.split()[0])    # kB
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    mem_used = max(0, mem_total - mem_avail)

    st = os.statvfs("/")
    disk_total = st.f_blocks * st.f_frsize
    disk_free = st.f_bavail * st.f_frsize
    disk_used = max(0, disk_total - disk_free)

    load1, load5, load15 = (float(x) for x in open("/proc/loadavg").read().split()[:3])
    uptime = float(open("/proc/uptime").read().split()[0])
    gib = 1024.0 ** 3

    return {
        "server": SERVER_NAME, "node": SERVER_NAME,
        "cpu_pct": cpu_pct, "cpu_cores": os.cpu_count(),
        "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
        "load1_per_core": round(load1 / (os.cpu_count() or 1), 2),
        "mem_total_gb": round(mem_total / 1048576, 1),
        "mem_used_gb": round(mem_used / 1048576, 1),
        "mem_pct": round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0,
        "disk_total_gb": round(disk_total / gib, 1),
        "disk_used_gb": round(disk_used / gib, 1),
        "disk_free_gb": round(disk_free / gib, 1),
        "disk_pct": round(100.0 * disk_used / disk_total, 1) if disk_total else 0.0,
        "uptime_days": round(uptime / 86400, 1),
        "checked": datetime.datetime.now(datetime.timezone.utc)
                   .isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


'''

ROUTE = '''        elif route == "/api/system":
            self._send_json(get_system())
        elif route == "/api/node":'''

block = re.search(r"\nHTML_TEMPLATE = \"\"\"", src)
if not block:
    print("FAIL: HTML_TEMPLATE anchor not found")
    sys.exit(2)
src = src[:block.start()] + "\n" + HELPER + src[block.start() + 1:]

if 'elif route == "/api/system":' not in src:
    if '        elif route == "/api/node":' in src:
        # aggregator host: it has the fleet routes
        src = src.replace('        elif route == "/api/node":', ROUTE, 1)
    else:
        # plain node dashboard: its route chain ends at the last json endpoint, then the
        # HTML fallback. Insert before that final else.
        anchor = re.search(r'\n        elif route == "/api/flat/geo":', src)
        tail = src.find("\n        else:", anchor.start()) if anchor else -1
        if tail == -1:
            print("FAIL: no route anchor found (neither /api/node nor /api/flat/geo)")
            sys.exit(3)
        src = (src[:tail + 1] +
               '        elif route == "/api/system":\n'
               '            self._send_json(get_system())\n' + src[tail + 1:])

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
os.makedirs("/root/.hp-backups", exist_ok=True)
shutil.copy2(PATH, "/root/.hp-backups/server.py.system.%s" % stamp)
open(PATH, "w", encoding="utf-8").write(src)

import ast
try:
    ast.parse(open(PATH, encoding="utf-8").read())
except SyntaxError as exc:
    shutil.copy2("/root/.hp-backups/server.py.system.%s" % stamp, PATH)
    print("FAIL: patched file does not parse (%s) - rolled back" % exc)
    sys.exit(4)
print("patched -> %s (+%d bytes)" % (PATH, len(src) - len(open(
    "/root/.hp-backups/server.py.system.%s" % stamp, encoding="utf-8").read())))
