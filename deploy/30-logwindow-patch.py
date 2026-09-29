#!/usr/bin/env python3
"""Patch a honeypot dashboard to read its whole log window, not just today's file.

Cowrie rotates cowrie.json -> cowrie.json.YYYY-MM-DD at midnight. A dashboard that
reads only the live file resets every counter at 00:00 and makes the "7 day" graphs
show a single day. This replaces exactly three regions (module constants, build_aggregate,
get_stats_uncached) plus inserts the window helpers, leaving every per-node value
(SERVER_NAME, the fleet NODES map, the listen address/port in __main__) untouched.

Idempotent: exits 0 if already patched. Usage: 30-logwindow-patch.py [path]
"""
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/cowrie-dashboard/server.py"

NEW_CONST = '''LOG_DIR = "/root/cowrie/var/log/cowrie"
LOG_FILE = os.path.join(LOG_DIR, "cowrie.json")   # live file (rotated at midnight)
# Cowrie rotates its event log at midnight (cowrie.json -> cowrie.json.YYYY-MM-DD).
# Reading only the live file resets every counter at 00:00 and turns the "7 day"
# graphs into a single day, so the whole window is read and merged instead.
RETENTION_DAYS = 8'''

HELPERS = r'''
# --- event-log window -----------------------------------------------------
# path -> {"off": bytes already folded in, "size": size at that time, "agg": per-file rollup}
_LOG_FILES = {}


def _empty_agg():
    return {"hourly": {}, "per_ip": {}, "totals": Counter(),
            "first_seen": None, "last_seen": None}


def _fold_event(agg, data):
    """Fold one parsed JSON event into an aggregate. Shared by every file."""
    ev = (data.get("eventid") or "").replace("cowrie.", "")
    ip = data.get("src_ip") or ""
    ts = data.get("timestamp") or ""
    if not ts:
        return
    agg["totals"]["events"] += 1

    bucket = agg["hourly"].get(ts[:13])
    if bucket is None:
        bucket = agg["hourly"][ts[:13]] = {
            "events": 0, "connects": 0, "logins_failed": 0, "logins_success": 0,
            "commands": 0, "downloads": 0, "ips": set(),
        }
    bucket["events"] += 1
    if ip:
        bucket["ips"].add(ip)

    if ev == "session.connect":
        bucket["connects"] += 1
    elif ev == "login.failed":
        bucket["logins_failed"] += 1
        agg["totals"]["logins_failed"] += 1
    elif ev == "login.success":
        bucket["logins_success"] += 1
        agg["totals"]["logins_success"] += 1
    elif ev == "command.input":
        bucket["commands"] += 1
        agg["totals"]["commands"] += 1
    elif ev == "session.file_download":
        bucket["downloads"] += 1
        agg["totals"]["downloads"] += 1
    elif ev == "session.file_upload":
        agg["totals"]["uploads"] += 1

    if not ip:
        return
    rec = agg["per_ip"].get(ip)
    if rec is None:
        rec = agg["per_ip"][ip] = {
            "ip": ip, "count": 0, "logins_failed": 0, "logins_success": 0,
            "commands": 0, "downloads": 0, "first_seen": ts, "last_seen": ts,
        }
    rec["count"] += 1
    if ts > rec["last_seen"]:
        rec["last_seen"] = ts
    if ts < rec["first_seen"]:
        rec["first_seen"] = ts
    if ev == "login.failed":
        rec["logins_failed"] += 1
    elif ev == "login.success":
        rec["logins_success"] += 1
    elif ev == "command.input":
        rec["commands"] += 1
    elif ev == "session.file_download":
        rec["downloads"] += 1

    if agg["first_seen"] is None or ts < agg["first_seen"]:
        agg["first_seen"] = ts
    if agg["last_seen"] is None or ts > agg["last_seen"]:
        agg["last_seen"] = ts


def _merge_into(dst, src):
    """Fold one file's rollup into the running window rollup."""
    for key, b in src["hourly"].items():
        db = dst["hourly"].get(key)
        if db is None:
            db = dst["hourly"][key] = {k: (set() if k == "ips" else 0) for k in b}
        for k, v in b.items():
            if k == "ips":
                db["ips"].update(v)
            else:
                db[k] += v
    dst["totals"].update(src["totals"])
    for ip, rec in src["per_ip"].items():
        cur = dst["per_ip"].get(ip)
        if cur is None:
            dst["per_ip"][ip] = dict(rec)
            continue
        cur["count"] += rec["count"]
        for k in ("logins_failed", "logins_success", "commands", "downloads"):
            cur[k] += rec[k]
        if rec["first_seen"] < cur["first_seen"]:
            cur["first_seen"] = rec["first_seen"]
        if rec["last_seen"] > cur["last_seen"]:
            cur["last_seen"] = rec["last_seen"]
    if src["first_seen"] and (dst["first_seen"] is None or src["first_seen"] < dst["first_seen"]):
        dst["first_seen"] = src["first_seen"]
    if src["last_seen"] and (dst["last_seen"] is None or src["last_seen"] > dst["last_seen"]):
        dst["last_seen"] = src["last_seen"]


def _log_paths(retention_days=RETENTION_DAYS):
    """Every log file inside the retention window, oldest name first."""
    try:
        names = [n for n in os.listdir(LOG_DIR) if n.startswith("cowrie.json")]
    except OSError:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=retention_days)).isoformat()
    out = []
    for n in names:
        if n == "cowrie.json":
            out.append(os.path.join(LOG_DIR, n))   # live file is always in-window
            continue
        stamp = n.rsplit(".", 1)[-1]               # cowrie.json.2026-09-28
        if len(stamp) == 10 and stamp >= cutoff:
            out.append(os.path.join(LOG_DIR, n))
    return sorted(out)


def _parse_log_file(path):
    """Incrementally fold one log file into its own rollup, cached between requests.

    Only bytes that arrived since the last poll are read, so a 12 MB rotated file
    costs a single pass and the live file costs only its new tail. A file that
    shrank (rotation/truncation) is re-read from the start.
    """
    st = _LOG_FILES.get(path)
    if st is None:
        st = _LOG_FILES[path] = {"off": 0, "size": 0, "agg": _empty_agg()}
    try:
        size = os.path.getsize(path)
    except OSError:
        return st["agg"]
    if size < st["off"]:
        st["off"], st["size"], st["agg"] = 0, 0, _empty_agg()
    if size == st["off"]:
        return st["agg"]
    try:
        with open(path, "rb") as f:
            f.seek(st["off"])
            chunk = f.read()
    except OSError:
        return st["agg"]
    end = chunk.rfind(b"\n")
    if end == -1:
        return st["agg"]                 # only a partial line written so far
    complete = chunk[:end + 1]
    for raw in complete.split(b"\n"):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _fold_event(st["agg"], data)
    st["off"] += len(complete)
    st["size"] = size
    return st["agg"]


def _tail_lines(path, n=80):
    """Last n complete lines of the newest file, for the 'recent activity' panels."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    return [l for l in lines[-n - 1:] if l.strip()]


'''

NEW_BUILD_AGG = r'''def build_aggregate():
    """Merge every log file in the retention window into one rollup.

    Each file is folded incrementally and cached, so a request costs only the
    live file's new tail - not a full re-read of the window. Parsing the whole
    window (rather than a tail slice) is deliberate: a flood of failed logins
    pushes older successes out of any tail and the counters then look frozen.
    """
    now = time.time()
    if _AGG_CACHE["val"] is not None and now - _AGG_CACHE["ts"] < AGG_TTL:
        return _AGG_CACHE["val"]

    window = _empty_agg()
    paths = _log_paths()
    for path in sorted(paths):
        agg = _parse_log_file(path)
        if agg["totals"].get("events"):
            _merge_into(window, agg)
    for gone in [p for p in _LOG_FILES if p not in paths]:
        del _LOG_FILES[gone]             # rotated past the window: free it

    val = {
        "hourly": window["hourly"],
        "per_ip": window["per_ip"],
        "totals": dict(window["totals"]),
        "first_seen": window["first_seen"],
        "last_seen": window["last_seen"],
        "unique_ips": len(window["per_ip"]),
        "files": [os.path.basename(p) for p in paths],
    }
    _AGG_CACHE["ts"] = now
    _AGG_CACHE["val"] = val
    return val


'''

NEW_STATS = r'''def get_stats_uncached():
    """Browser payload: totals/rollups from the whole log window, recent activity
    from the tail of the newest file."""
    agg = build_aggregate()
    totals = agg["totals"]
    ip_activity = Counter({ip: rec["count"] for ip, rec in agg["per_ip"].items()})

    commands = []
    downloads = []
    recent_events = []
    for raw in reversed(_tail_lines(LOG_FILE, 80)):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        ev = data.get("eventid", "")
        ip = data.get("src_ip", "")
        ts = data.get("timestamp", "")
        if len(recent_events) < 25:
            recent_events.append({
                "ts": ts[11:19],
                "ip": ip or "local",
                "ev": ev.replace("cowrie.", ""),
                "msg": data.get("message", "") or data.get("input", "") or data.get("destfile", "") or data.get("data", "")
            })
        if ev == "cowrie.command.input":
            commands.append({"ip": ip, "cmd": data.get("input", ""), "ts": ts, "session": data.get("session", "")})
        elif ev == "cowrie.session.file_download":
            downloads.append({
                "ip": ip,
                "url": data.get("url", ""),
                "file": data.get("destfile", ""),
                "sha": data.get("shasum", ""),
                "ts": ts
            })

    country_activity = Counter()
    geo_markers = []

    top_ips_formatted = []
    for ip, count in ip_activity.most_common(12):
        geo = get_country(ip)
        country_activity[geo["country"]] += count
        top_ips_formatted.append({
            "ip": ip,
            "count": count,
            "country": geo["country"],
            "flag": geo["flag"],
            "lat": geo["lat"],
            "lon": geo["lon"]
        })
        if geo["lat"] != 0 and geo["lon"] != 0:
            geo_markers.append({
                "ip": ip,
                "count": count,
                "lat": geo["lat"],
                "lon": geo["lon"],
                "country": geo["country"],
                "flag": geo["flag"]
            })

    return {
        "server": SERVER_NAME,
        "total_events": totals.get("events", 0),
        "unique_ips_count": agg["unique_ips"],
        "logins_success": totals.get("logins_success", 0),
        "logins_failed": totals.get("logins_failed", 0),
        "commands_count": totals.get("commands", 0),
        "downloads_count": totals.get("downloads", 0),
        "window_days": RETENTION_DAYS,
        "log_files": agg.get("files") or [],
        "first_seen": agg["first_seen"],
        "last_seen": agg["last_seen"],
        "top_ips": top_ips_formatted,
        "top_countries": country_activity.most_common(6),
        "geo_markers": geo_markers,
        "recent_commands": commands[:15],
        "recent_downloads": downloads[:8],
        "recent_events": recent_events[:25]
    }


'''


def replace_func(text, name, new_text):
    """Swap a top-level function body, ending at the next top-level def/class/marker."""
    m = re.search(
        r"^def %s\(.*?(?=^def |^class |^HTML_TEMPLATE|^# ---|\Z)" % re.escape(name),
        text, re.S | re.M)
    if not m:
        return None
    return text[:m.start()] + new_text + text[m.end():]


def main():
    with open(PATH, encoding="utf-8") as f:
        text = f.read()

    if "_LOG_FILES" in text:
        print("already patched")
        return 0

    # 1. constants
    new_text, n = re.subn(r'^LOG_FILE = .*$', NEW_CONST, text, count=1, flags=re.M)
    if n != 1:
        print("FAIL: could not find the LOG_FILE constant")
        return 2
    text = new_text

    # 2. helpers, inserted just before the geo helpers
    anchor = "def get_country(ip):"
    if anchor not in text:
        print("FAIL: anchor %r missing" % anchor)
        return 2
    text = text.replace(anchor, HELPERS.lstrip("\n") + anchor, 1)

    # 3. the two functions
    for name, block in (("build_aggregate", NEW_BUILD_AGG), ("get_stats_uncached", NEW_STATS)):
        out = replace_func(text, name, block)
        if out is None:
            print("FAIL: function %s not found" % name)
            return 2
        text = out

    # 4. summary should advertise the window too (harmless if the line is absent)
    text = text.replace(
        '"downloads_count": t.get("downloads", 0),\n',
        '"downloads_count": t.get("downloads", 0),\n        "window_days": RETENTION_DAYS,\n', 1)

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(text)

    # the file must still compile, or the dashboard will not come back up
    import ast
    try:
        ast.parse(text)
    except SyntaxError as exc:
        print("FAIL: patched file does not parse: %s" % exc)
        return 3
    print("patched ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
