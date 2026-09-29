import datetime
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

LOG_DIR = "/root/cowrie/var/log/cowrie"
LOG_FILE = os.path.join(LOG_DIR, "cowrie.json")   # live file (rotated at midnight)
# Cowrie rotates its event log at midnight (cowrie.json -> cowrie.json.YYYY-MM-DD).
# Reading only the live file resets every counter at 00:00 and turns the "7 day"
# graphs into a single day, so the whole window is read and merged instead.
RETENTION_DAYS = 8
GEO_CACHE = {}
# Local MMDB (DB-IP Lite city). Glob, so a refreshed monthly file is picked up by
# dropping it in and restarting -- no config change, no hardcoded version.
GEOIP_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geoip", "dbip-city-lite*.mmdb")
# ip -> timestamp before which we must NOT retry a failed lookup. Failures are
# never cached permanently: ip-api rate limits would otherwise pin an attacker
# to "Unknown" for the whole lifetime of the process.
GEO_FAIL = {}
_PRIVATE_PREFIX = ("10.", "192.168.", "127.", "172.", "100.64.", "::1")
SERVER_NAME = "sensor1"

# Fleet map for the aggregator: node -> host:port of its dashboard JSON API.
# Nodes without inbound HTTPS (most providers only forward 22) are reached over the
# tailnet instead of getting their own public hostname + certificate.
NODES = {
    "sensor1": "127.0.0.1:8099",
    "sensor2": "100.64.0.19:8098",
    "sensor5": "100.64.0.12:8099",
    "sensor6": "100.64.0.12:8099",
    "sensor7": "100.64.0.15:8099",
    "sensor3": "100.64.0.17:8099",
    "sensor4": "100.64.0.14:8099",
    "sensor8": "100.64.0.13:8099",
    "sensor9": "100.64.0.20:8099",
}

# --- caches ---------------------------------------------------------------
_STATS_MEMO = {"ts": 0.0, "val": None}
_AGG_CACHE = {"ts": 0.0, "val": None}
STATS_TTL = 5
AGG_TTL = 30

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

_GEOIP = {"reader": None, "checked": 0.0}


def _geo_db():
    """Local city database (DB-IP Lite, MMDB). No API, no rate limit, ~30us per lookup.

    ip-api capped at 45 lookups/min and every geo panel fanned out to it, so the map
    panels were the slowest thing on the dashboard and the first to time out. Returning
    None (rather than raising) keeps a node functional while its database is missing:
    callers fall back to the old HTTP path.
    """
    reader = _GEOIP["reader"]
    if reader is not None:
        return reader
    if time.time() - _GEOIP["checked"] < 60:
        return None
    _GEOIP["checked"] = time.time()
    try:
        import maxminddb
        import glob as _glob
        matches = sorted(_glob.glob(GEOIP_GLOB))
        if not matches:
            return None
        _GEOIP["reader"] = maxminddb.open_database(matches[-1])
        return _GEOIP["reader"]
    except Exception:
        return None


def _flag_for(code):
    if code and len(code) == 2 and code != "UN":
        return "".join(chr(127397 + ord(c)) for c in code.upper())
    return "🌐"


# --- ipinfo enrichment ------------------------------------------------------------
# The local MMDB gives country + coordinates instantly but no network owner, which is
# the field that actually says what an attacker is (hosting provider, VPN, Tor exit).
# ipinfo adds org/ASN and city. It is a quota-limited API, so every answer is written
# to a JSON file: an IP is looked up once in the life of the box, not once per restart
# and never once per page view. Without a token file it still works, at the lower
# unauthenticated quota.
IPINFO_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipinfo.token")
GEO_ENRICH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo-enrich.json")
GEO_ENRICH = {}
_ENRICH_LOADED = {"done": False}
_ENRICH_SAVED = {"at": 0.0}
# With a token (ipinfo lite measured at 60 lookups in 8s with zero 429s) the fleet's
# entire attacker set can be backfilled in minutes, and the steady-state cost is only
# genuinely new IPs -- a few thousand a month against a 50k quota. Without a token the
# worker drops to a trickle that stays inside the unauthenticated quota.
ENRICH_PER_CYCLE = 200
ENRICH_CYCLE_SECONDS = 60
ENRICH_NO_TOKEN_PER_CYCLE = 5      # ~720/day, the unauthenticated budget
ENRICH_DAILY_CAP = 3000            # belt and braces: a runaway list cannot drain the quota
_ENRICH_DAY = {"day": "", "n": 0}


def _enrich_load():
    if _ENRICH_LOADED["done"]:
        return
    _ENRICH_LOADED["done"] = True
    try:
        with open(GEO_ENRICH_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            GEO_ENRICH.update(data)
    except Exception:
        pass


def _enrich_save(force=False):
    now = time.time()
    if not force and now - _ENRICH_SAVED["at"] < 300:
        return
    _ENRICH_SAVED["at"] = now
    try:
        tmp = GEO_ENRICH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(GEO_ENRICH, fh)
        os.replace(tmp, GEO_ENRICH_FILE)
    except Exception:
        pass


def _ipinfo_token():
    try:
        with open(IPINFO_TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def enrich_one(ip):
    """One ipinfo lookup, cached forever on disk. Returns the record or None."""
    _enrich_load()
    if ip in GEO_ENRICH:
        return GEO_ENRICH[ip]
    if not ip or ip.startswith(_PRIVATE_PREFIX):
        return None
    url = "https://ipinfo.io/%s/json" % ip
    token = _ipinfo_token()
    if token:
        url += "?token=" + urllib.parse.quote(token)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "honeypot-dashboard"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rec = {
            "city": data.get("city", "") or "",
            "region": data.get("region", "") or "",
            "org": data.get("org", "") or "",
            "country": data.get("country", "") or "",
            "loc": data.get("loc", "") or "",
            "hostname": data.get("hostname", "") or "",
        }
        GEO_ENRICH[ip] = rec
        _enrich_save()
        return rec
    except Exception:
        return None


def _geo_enricher():
    """Fill in org/city for the heaviest attackers, forever cached to disk.

    Ranked by hits so the addresses a human actually looks at get named first. The pace
    adapts to credentials: with a token it backfills the whole list, without one it
    trickles to stay inside the unauthenticated quota. Either way a runaway list cannot
    drain the monthly budget, and every answer is written down.
    """
    _enrich_load()
    while True:
        try:
            today = time.strftime("%Y-%m-%d")
            if _ENRICH_DAY["day"] != today:
                _ENRICH_DAY["day"] = today
                _ENRICH_DAY["n"] = 0
            room = ENRICH_DAILY_CAP - _ENRICH_DAY["n"]
            if room > 0:
                per_cycle = ENRICH_PER_CYCLE if _ipinfo_token() else ENRICH_NO_TOKEN_PER_CYCLE
                agg = build_aggregate()
                todo = [r["ip"] for r in sorted(agg["per_ip"].values(),
                                                key=lambda r: r["count"], reverse=True)
                        if r["ip"] not in GEO_ENRICH and not r["ip"].startswith(_PRIVATE_PREFIX)]
                for ip in todo[:min(per_cycle, room)]:
                    if enrich_one(ip):
                        _ENRICH_DAY["n"] += 1
                _enrich_save(force=True)
        except Exception:
            pass
        time.sleep(ENRICH_CYCLE_SECONDS)


def get_country(ip):
    if not ip or ip.startswith(_PRIVATE_PREFIX):
        return {"country": "Private", "code": "UN", "flag": "🌐", "lat": 0, "lon": 0}
    if ip in GEO_CACHE:
        return GEO_CACHE[ip]

    reader = _geo_db()
    if reader is not None:
        try:
            rec = reader.get(ip) or {}
            country = (rec.get("country") or {}).get("names", {}).get("en", "") or "Unknown"
            code = (rec.get("country") or {}).get("iso_code", "UN") or "UN"
            loc = (rec.get("location") or {})
            city = (rec.get("city") or {}).get("names", {}).get("en", "") or ""
            res = {
                "country": country,
                "code": code,
                "flag": _flag_for(code),
                "lat": loc.get("latitude") or 0,
                "lon": loc.get("longitude") or 0,
                "city": city,
                "isp": "",
            }
            # ipinfo's org (ASN + owner) only if it is already cached: never block a
            # page render on a quota-limited API for a cosmetic field.
            _enrich_load()
            extra = GEO_ENRICH.get(ip)
            if extra:
                res["city"] = extra.get("city") or res["city"]
                res["isp"] = extra.get("org") or ""
            GEO_CACHE[ip] = res
            return res
        except Exception:
            pass

    extra = enrich_one(ip)
    if extra:
        code = extra.get("country") or "UN"
        lat = lon = 0
        try:
            lat, lon = [float(x) for x in (extra.get("loc") or "").split(",")]
        except Exception:
            pass
        res = {
            "country": code,
            "code": code,
            "flag": _flag_for(code),
            "lat": lat,
            "lon": lon,
            "city": extra.get("city", ""),
            "isp": extra.get("org", ""),
        }
        GEO_CACHE[ip] = res
        return res
    fallback = {"country": "Unknown", "code": "UN", "flag": "🌐", "lat": 0, "lon": 0,
                "city": "", "isp": ""}
    GEO_FAIL[ip] = time.time() + 600
    return fallback


def geo_resolve(ips):
    """Resolve many IPs at once. Local MMDB first: no rate limit, so a 500-IP map is free.

    The HTTP fallback (ip-api /batch, 100 IPs per request) only runs on a node without
    the database, where the single-lookup cap of 45 req/min would otherwise make the map
    take minutes and trip the limit.
    """
    now = time.time()
    reader = _geo_db()
    if reader is not None:
        _enrich_load()
        for ip in ips:
            if not ip or ip in GEO_CACHE or ip.startswith(_PRIVATE_PREFIX):
                continue
            try:
                rec = reader.get(ip) or {}
                country = (rec.get("country") or {}).get("names", {}).get("en", "") or "Unknown"
                code = (rec.get("country") or {}).get("iso_code", "UN") or "UN"
                city = (rec.get("city") or {}).get("names", {}).get("en", "") or ""
                loc = (rec.get("location") or {})
                extra = GEO_ENRICH.get(ip) or {}
                GEO_CACHE[ip] = {
                    "country": country,
                    "code": code,
                    "flag": _flag_for(code),
                    "lat": loc.get("latitude") or 0,
                    "lon": loc.get("longitude") or 0,
                    "city": extra.get("city") or city,
                    "isp": extra.get("org", ""),
                }
            except Exception:
                GEO_FAIL[ip] = now + 900
        return GEO_CACHE

    pending = []
    for ip in ips:
        if not ip or ip in GEO_CACHE or ip in _PRIVATE_PREFIX:
            continue
        if GEO_FAIL.get(ip, 0) > now:
            continue
        pending.append(ip)

    for i in range(0, len(pending), 100):
        chunk = pending[i:i + 100]
        try:
            body = json.dumps([{"query": ip} for ip in chunk]).encode("utf-8")
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=status,message,country,countryCode,lat,lon,city,isp",
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Cowrie-Dashboard"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for ip, item in zip(chunk, data):
                if isinstance(item, dict) and item.get("status") == "success":
                    code = item.get("countryCode", "UN")
                    flag = "".join(chr(127397 + ord(c)) for c in code.upper()) if len(code) == 2 else "🌐"
                    GEO_CACHE[ip] = {
                        "country": item.get("country", "Unknown"),
                        "code": code,
                        "flag": flag,
                        "lat": item.get("lat", 0),
                        "lon": item.get("lon", 0),
                        "city": item.get("city", ""),
                        "isp": item.get("isp", ""),
                    }
                else:
                    GEO_FAIL[ip] = now + 900
        except Exception:
            for ip in chunk:
                GEO_FAIL[ip] = now + 120
    return GEO_CACHE


def get_geo(limit=150):
    """Flat marker rows for Grafana's Geomap: top attackers with coordinates."""
    agg = build_aggregate()
    try:
        limit = int(limit)
    except Exception:
        limit = 150
    limit = max(1, min(limit, 500))

    top = sorted(agg["per_ip"].values(), key=lambda r: r["count"], reverse=True)[:limit]
    geo_resolve([r["ip"] for r in top])

    rows = []
    _enrich_load()
    for r in top:
        g = dict(GEO_CACHE.get(r["ip"]) or {})
        # ipinfo lands asynchronously (it is quota-limited), so a record cached before
        # the enrichment ran must still pick up org/city on read rather than staying
        # blank until the process restarts.
        extra = GEO_ENRICH.get(r["ip"]) or {}
        if extra:
            g["city"] = extra.get("city") or g.get("city", "")
            g["isp"] = extra.get("org") or g.get("isp", "")
            # ipinfo is the more accurate source when it knows the IP, and it costs
            # nothing to prefer it here: the data is already cached.
            if extra.get("country"):
                g["code"] = extra["country"]
            try:
                if extra.get("loc"):
                    e_lat, e_lon = [float(x) for x in extra["loc"].split(",")]
                    g["lat"], g["lon"] = e_lat, e_lon
            except Exception:
                pass
        lat, lon = g.get("lat") or 0, g.get("lon") or 0
        if not lat and not lon:
            continue  # unmappable (private, or the lookup is still rate-limited)
        rows.append({
            "ip": r["ip"],
            "lat": lat,
            "lon": lon,
            "hits": r["count"],
            "country": g.get("country", "Unknown"),
            "code": g.get("code", "UN"),
            "city": g.get("city", ""),
            "isp": g.get("isp", ""),
            "logins_success": r["logins_success"],
            "commands": r["commands"],
            "downloads": r["downloads"],
            "last_seen": r["last_seen"],
        })
    return rows

def get_stats_uncached():
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


def get_stats():
    """get_stats() output memoised for a few seconds (dashboard + Grafana panels all hit it)."""
    now = time.time()
    if _STATS_MEMO["val"] is not None and now - _STATS_MEMO["ts"] < STATS_TTL:
        return _STATS_MEMO["val"]
    val = get_stats_uncached()
    _STATS_MEMO["ts"] = now
    _STATS_MEMO["val"] = val
    return val


def build_aggregate():
    """Merge every log file in the retention window into one rollup.

    Each file is folded incrementally and cached, so a request costs only the
    live file's new tail — not a full re-read of the window. Parsing the whole
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


def get_timeseries(hours=168):
    """Dense hourly buckets, gap-filled with zeros so panels never break the line."""
    agg = build_aggregate()
    try:
        hours = int(hours)
    except Exception:
        hours = 168
    hours = max(1, min(hours, 24 * 90))

    # Anchor the window on the log's own clock, not the wall clock: cowrie writes
    # Europe/Paris local time but stamps it with a 'Z', so its "20:28Z" is really
    # 18:28 UTC. Anchoring on wall-clock UTC silently drops the newest buckets.
    end = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    last = agg["last_seen"]
    if last:
        try:
            last_hour = datetime.datetime.strptime(last[:13], "%Y-%m-%dT%H").replace(
                tzinfo=datetime.timezone.utc
            )
            if last_hour > end:
                end = last_hour
        except Exception:
            pass
    buckets = []
    for i in range(hours - 1, -1, -1):
        t = end - datetime.timedelta(hours=i)
        key = t.strftime("%Y-%m-%dT%H")
        b = agg["hourly"].get(key) or {}
        buckets.append({
            "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_ms": int(t.timestamp() * 1000),
            "events": b.get("events", 0),
            "connects": b.get("connects", 0),
            "logins_failed": b.get("logins_failed", 0),
            "logins_success": b.get("logins_success", 0),
            "commands": b.get("commands", 0),
            "downloads": b.get("downloads", 0),
            "unique_ips": len(b.get("ips") or ()),
        })
    return {
        "server": SERVER_NAME,
        "window_hours": hours,
        "first_seen": agg["first_seen"],
        "last_seen": agg["last_seen"],
        "buckets": buckets,
    }


def get_attackers(limit=25):
    """Per-attacker table for Grafana: hits, logins, commands, payloads, country."""
    agg = build_aggregate()
    try:
        limit = int(limit)
    except Exception:
        limit = 25
    limit = max(1, min(limit, 200))

    rows = []
    for rec in sorted(agg["per_ip"].values(), key=lambda r: r["count"], reverse=True):
        if len(rows) >= limit:
            break
        geo = get_country(rec["ip"])
        rows.append({
            "ip": rec["ip"],
            "hits": rec["count"],
            "logins_failed": rec["logins_failed"],
            "logins_success": rec["logins_success"],
            "commands": rec["commands"],
            "downloads": rec["downloads"],
            "country": geo["country"],
            "code": geo["code"],
            "first_seen": rec["first_seen"],
            "last_seen": rec["last_seen"],
        })
    return {"server": SERVER_NAME, "count": len(rows), "rows": rows}


def get_summary():
    """Small scalar payload — cheap for stat panels."""
    agg = build_aggregate()
    t = agg["totals"]
    return {
        "server": SERVER_NAME,
        "total_events": t.get("events", 0),
        "unique_ips_count": agg["unique_ips"],
        "logins_success": t.get("logins_success", 0),
        "logins_failed": t.get("logins_failed", 0),
        "commands_count": t.get("commands", 0),
        "downloads_count": t.get("downloads", 0),
        "window_days": RETENTION_DAYS,
        "first_seen": agg["first_seen"],
        "last_seen": agg["last_seen"],
    }


_FLEET_ALIASES = ("all", "fleet", "*")
_FLEET_CACHE = {}
_FLEET_TTL = 15.0

# The first 'all' request for an endpoint pays a full fleet fan-out (measured 7s cold,
# 0.2-0.4s warm). A dashboard asks for ~10 endpoints within the same second, so with a
# cold cache they all wait on their own merge and anything slow enough gets dropped by
# Grafana -- panels render empty even though every node is healthy. Warm the endpoints
# the dashboard actually uses, in the background, just inside the TTL.
_FLEET_WARM = (
    ("summary", ""),
    ("timeseries", "hours=168"),
    ("flat/timeseries", "hours=168"),
    ("flat/attackers", "limit=25"),
    ("flat/events", ""),
    ("flat/commands", ""),
    ("flat/downloads", ""),
)
# Deliberately NOT warmed: 'stats' and 'flat/geo'. Both fan out to the per-node geo
# lookups, which are rate-limited upstream (ip-api) and slow enough that warming them
# on a short cycle starves the cheap endpoints instead of helping.
_FLEET_WARM_INTERVAL = 20.0


def _fleet_prewarmer():
    """Keep the fleet merges hot so a panel never waits on a cold fan-out."""
    while True:
        for rest, query in _FLEET_WARM:
            try:
                fetch_fleet(rest, query)
            except Exception:
                pass
        time.sleep(_FLEET_WARM_INTERVAL)


def _relay(node, rest, query="", timeout=20):
    """One node's endpoint. Returns {"__error": ...} so a dead node is visible to the merge."""
    target = NODES.get(node)
    if not target:
        return {"__error": "unknown node", "node": node}
    url = "http://%s/api/%s" % (target, rest)
    if query:
        url += "?" + query
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "honeypot-aggregator"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"__error": str(exc), "node": node}


def _qlimit(query, default):
    for part in (query or "").split("&"):
        if part.startswith("limit="):
            try:
                return max(1, int(part.split("=", 1)[1]))
            except Exception:
                pass
    return default


def _sum_field(parts, key):
    return sum(p.get(key, 0) or 0 for p in parts)


def _merge_timeseries(parts, query=""):
    """Hourly buckets are additive except unique_ips, which cannot be unioned from here.

    A per-hour fleet-wide distinct count would need per-IP-per-hour data the nodes do
    not expose, so unique_ips reports the busiest single node in that hour: a floor,
    never an overstatement.
    """
    by_ts = {}
    for p in parts:
        # /timeseries wraps buckets in an object; /flat/timeseries returns a bare list.
        buckets = p if isinstance(p, list) else (p.get("buckets") or [])
        for b in buckets:
            ts = b.get("ts")
            if not ts:
                continue
            row = by_ts.setdefault(ts, {
                "ts": ts, "ts_ms": b.get("ts_ms", 0), "events": 0, "connects": 0,
                "logins_failed": 0, "logins_success": 0, "commands": 0,
                "downloads": 0, "unique_ips": 0,
            })
            for k in ("events", "connects", "logins_failed", "logins_success",
                      "commands", "downloads"):
                row[k] += b.get(k, 0) or 0
            row["unique_ips"] = max(row["unique_ips"], b.get("unique_ips", 0) or 0)
    return [by_ts[k] for k in sorted(by_ts)]


def _merge_attackers(parts, query=""):
    """Same IP on several nodes is one attacker with the summed activity."""
    merged = {}
    for p in parts:
        rows = p.get("rows") if isinstance(p, dict) else p
        for r in rows or []:
            ip = r.get("ip")
            if not ip:
                continue
            m = merged.setdefault(ip, {
                "ip": ip, "hits": 0, "logins_failed": 0, "logins_success": 0,
                "commands": 0, "downloads": 0, "country": r.get("country", ""),
                "code": r.get("code", ""), "first_seen": r.get("first_seen", ""),
                "last_seen": r.get("last_seen", ""), "nodes": 0,
            })
            for k in ("hits", "logins_failed", "logins_success", "commands", "downloads"):
                m[k] += r.get(k, 0) or 0
            m["nodes"] += 1
            fs, ls = r.get("first_seen", ""), r.get("last_seen", "")
            if fs and (not m["first_seen"] or fs < m["first_seen"]):
                m["first_seen"] = fs
            if ls and ls > m["last_seen"]:
                m["last_seen"] = ls
            if not m["country"] and r.get("country"):
                m["country"], m["code"] = r["country"], r.get("code", "")
    rows = sorted(merged.values(), key=lambda r: r["hits"], reverse=True)
    return rows[:_qlimit(query, 25)]


def _merge_geo(parts, query=""):
    merged = {}
    for p in parts:
        for r in p or []:
            ip = r.get("ip")
            if not ip:
                continue
            m = merged.setdefault(ip, dict(r))
            if m is not r:
                # flat/geo counts hits; the dashboard HTML's markers use count.
                for k in ("hits", "count", "logins_success", "commands", "downloads"):
                    m[k] = (m.get(k, 0) or 0) + (r.get(k, 0) or 0)
                m["nodes"] = (m.get("nodes", 0) or 0) + 1
                if r.get("last_seen", "") > m.get("last_seen", ""):
                    m["last_seen"] = r["last_seen"]
            else:
                m["nodes"] = 1
            if not m.get("country") and r.get("country"):
                m["country"], m["code"] = r["country"], r.get("code", "")
    rows = sorted(merged.values(), key=lambda r: r.get("hits", 0), reverse=True)
    return rows[:_qlimit(query, 150)]


def _merge_events(parts, limit=25):
    """Round-robin across nodes: each node's stream is already newest-first, and the
    per-event timestamps carry no date, so cross-node sorting would be guesswork."""
    out, idx = [], 0
    lists = [p for p in parts if isinstance(p, list) and p]
    while len(out) < limit and any(idx < len(lst) for lst in lists):
        for lst in lists:
            if idx < len(lst) and len(out) < limit:
                out.append(lst[idx])
        idx += 1
    return out


def _merge_recent(parts, limit, key_ts="ts"):
    rows = [r for p in parts if isinstance(p, list) for r in p]
    rows.sort(key=lambda r: r.get(key_ts, ""), reverse=True)
    return rows[:limit]


def _merge_summary(parts, ok):
    unique_ips, unique_sources, unique_missing = _fleet_unique_ips()
    # Counts cannot be unioned, so the fleet total is bracketed: the union of each node's
    # top 200 is a floor, the sum of the per-node distinct counts is a ceiling.
    ceiling = _sum_field(ok, "unique_ips_count") or unique_ips
    return {
        "server": "all",
        "nodes_reporting": len(ok),
        "nodes_total": len(NODES),
        "total_events": _sum_field(ok, "total_events"),
        "unique_ips_count": unique_ips,
        "unique_ips_floor": unique_ips,
        "unique_ips_ceiling": ceiling,
        "unique_ips_nodes": "%d/%d" % (unique_sources, len(NODES)),
        "unique_ips_missing": unique_missing,
        "logins_success": _sum_field(ok, "logins_success"),
        "logins_failed": _sum_field(ok, "logins_failed"),
        "commands_count": _sum_field(ok, "commands_count"),
        "downloads_count": _sum_field(ok, "downloads_count"),
        "window_days": RETENTION_DAYS,
        "first_seen": min([p.get("first_seen", "") for p in ok if p.get("first_seen")] or [""]),
        "last_seen": max([p.get("last_seen", "") for p in ok if p.get("last_seen")] or [""]),
    }


def _fleet_unique_ips():
    """Distinct attacker IPs across the fleet: union of each node's full attacker list.

    limit=1000 per node, but each node clamps its attacker list to its own top 200, so the
    union is a FLOOR, not the true distinct count (the sum of per-node counts is the
    ceiling — see _merge_summary). The count of contributing nodes is reported alongside
    it: a node that fails to answer silently shrinks the union, and an unexplained drop
    would otherwise look like attackers going away.
    """
    def one(node):
        return node, _relay(node, "flat/attackers", "limit=1000")
    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        results = list(pool.map(one, sorted(NODES)))
    # One slow node silently shrinks the union, so a failure is retried on its own
    # rather than reported as attackers going away.
    failed = [n for n, r in results if not isinstance(r, list)]
    retried = []
    for node in failed:
        # A cold read of a busy node's whole window can outrun the normal relay timeout,
        # which is exactly the case that used to drop nodes out of the union.
        r = _relay(node, "flat/attackers", "limit=1000", timeout=90)
        retried.append((node, r))
    results = [(n, r) for n, r in results if isinstance(r, list)] + retried
    seen = set()
    answering = 0
    missing = []
    for node, lst in results:
        if not isinstance(lst, list):
            missing.append(node)
            continue
        answering += 1
        for r in lst:
            if r.get("ip"):
                seen.add(r["ip"])
    return len(seen), answering, missing


def fetch_fleet(rest, query=""):
    """Build a node called 'all' by merging the same endpoint from every node."""
    key = (rest, query)
    hit = _FLEET_CACHE.get(key)
    now = time.time()
    if hit and now - hit["ts"] < _FLEET_TTL:
        return hit["val"]

    def one(node):
        return _relay(node, rest, query)

    with ThreadPoolExecutor(max_workers=len(NODES)) as pool:
        results = list(pool.map(one, sorted(NODES)))
    errors = [r.get("node") for r in results if isinstance(r, dict) and "__error" in r]
    ok = [r for r in results if not (isinstance(r, dict) and "__error" in r)]

    if rest == "summary":
        val = _merge_summary(ok, ok)
    elif rest == "stats":
        s = _merge_summary(ok, ok)
        s["top_ips"] = _merge_attackers([p.get("top_ips", []) for p in ok], "limit=12")
        s["geo_markers"] = _merge_geo([p.get("geo_markers", []) for p in ok], "limit=12")
        s["top_countries"] = Counter(
            {c: n for p in ok for c, n in (p.get("top_countries") or [])}
        ).most_common(6)
        s["log_files"] = sorted({f for p in ok for f in (p.get("log_files") or [])})
        s["recent_events"] = _merge_events([p.get("recent_events", []) for p in ok], 25)
        s["recent_commands"] = _merge_recent([p.get("recent_commands", []) for p in ok], 15)
        s["recent_downloads"] = _merge_recent([p.get("recent_downloads", []) for p in ok], 8)
        val = s
    elif rest == "system":
        # One row per node, most-at-risk first: a fleet table is read top-down.
        rows = [p for p in ok if isinstance(p, dict)]
        rows.sort(key=lambda r: r.get("disk_pct", 0), reverse=True)
        mem_total = sum(r.get("mem_total_gb", 0) or 0 for r in rows)
        mem_used = sum(r.get("mem_used_gb", 0) or 0 for r in rows)
        worst = rows[0] if rows else {}
        val = {
            "server": "all",
            "nodes_reporting": len(rows),
            "nodes_total": len(NODES),
            "worst_disk_pct": worst.get("disk_pct", 0),
            "worst_disk_node": worst.get("node", ""),
            "total_disk_free_gb": round(sum(r.get("disk_free_gb", 0) or 0 for r in rows), 1),
            "total_mem_gb": round(mem_total, 1),
            "total_mem_used_gb": round(mem_used, 1),
            "fleet_mem_pct": round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0,
            "total_cores": sum(r.get("cpu_cores", 0) or 0 for r in rows),
            "max_load1_per_core": max([r.get("load1_per_core", 0) or 0 for r in rows] or [0]),
            "nodes": rows,
        }
    elif rest == "timeseries":
        val = {"server": "all", "window_hours": 168,
               "first_seen": _merge_summary(ok, ok)["first_seen"],
               "last_seen": _merge_summary(ok, ok)["last_seen"],
               "buckets": _merge_timeseries(ok, query)}
    elif rest == "flat/timeseries":
        val = _merge_timeseries(ok, query)
    elif rest in ("attackers", "flat/attackers"):
        rows = _merge_attackers(ok if rest == "flat/attackers" else
                               [p.get("rows", []) for p in ok], query)
        val = rows if rest == "flat/attackers" else {
            "server": "all", "count": len(rows), "rows": rows}
    elif rest == "flat/events":
        val = _merge_events(ok, _qlimit(query, 25))
    elif rest == "flat/commands":
        val = _merge_recent(ok, _qlimit(query, 15))
    elif rest == "flat/downloads":
        val = _merge_recent(ok, _qlimit(query, 8))
    elif rest == "flat/geo":
        val = _merge_geo(ok, query)
    else:
        return {"error": "unsupported fleet endpoint", "endpoint": rest,
                "known": ["summary", "stats", "timeseries", "attackers", "flat/timeseries",
                          "flat/attackers", "flat/events", "flat/commands",
                          "flat/downloads", "flat/geo"]}

    if isinstance(val, dict):
        val["nodes_reporting"] = len(ok)
        val["nodes_total"] = len(NODES)
        if errors:
            val["nodes_erroring"] = errors
    _FLEET_CACHE[key] = {"ts": now, "val": val}
    return val


def fetch_node(node, rest, query=""):
    """Aggregator: relay <node>/<endpoint> from the fleet map over the tailnet.

    Nodes on providers that only forward 22 have no inbound HTTPS, so they cannot
    serve a public hostname + certificate. One public hostname (this one) fans out
    to their private APIs instead. node='all' merges every node's response.
    """
    if node in _FLEET_ALIASES:
        return fetch_fleet(rest, query)
    target = NODES.get(node)
    if not target:
        return {"error": "unknown node", "node": node, "known": sorted(NODES)}
    return _relay(node, rest, query)



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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>honeypot fleet // live</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Plus+Jakarta+Sans:wght@400;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        :root {
            --bg: #0b0d13;
            --surface: #12151e;
            --border: #1f2533;
            --accent: #f472b6;
            --accent-glow: rgba(244, 114, 182, 0.15);
            --cyan: #38bdf8;
            --green: #4ade80;
            --amber: #fbbf24;
            --rose: #f43f5e;
            --text: #e2e8f0;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg);
            color: var(--text);
            font-family: 'Plus Jakarta Sans', sans-serif;
            padding: 24px;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
            flex-wrap: wrap;
            gap: 12px;
        }
        .title-group { display: flex; align-items: center; gap: 12px; }
        .node-pills { display: flex; gap: 8px; }
        .node-pill {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text-muted);
            cursor: pointer;
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 12px;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            transition: all 0.2s;
            user-select: none;
        }
        .node-pill:hover, .node-pill.active {
            color: var(--accent);
            border-color: var(--accent);
            background: var(--accent-glow);
        }
        .pulse-dot {
            width: 8px;
            height: 8px;
            background: var(--green);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 10px var(--green);
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.3); opacity: 0.6; }
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 18px 20px;
            position: relative;
            overflow: hidden;
        }
        .stat-card::after {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, var(--accent), transparent);
        }
        .stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 700; letter-spacing: 0.05em; }
        .stat-val { font-size: 28px; font-weight: 700; margin-top: 6px; font-family: 'JetBrains Mono', monospace; }

        #map-container {
            height: 340px;
            width: 100%;
            border-radius: 12px;
            border: 1px solid var(--border);
            margin-bottom: 24px;
            overflow: hidden;
            background: #08090e;
        }
        .map-tiles-dark {
            filter: brightness(0.6) invert(1) contrast(3) hue-rotate(200deg) saturate(0.3) brightness(0.7);
        }

        .main-grid {
            display: grid;
            grid-template-columns: 2fr 1.1fr;
            gap: 20px;
        }
        @media (max-width: 950px) { .main-grid { grid-template-columns: 1fr; } }

        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .card-title {
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .table { width: 100%; border-collapse: collapse; font-family: 'JetBrains Mono', monospace; font-size: 13px; }
        .table th { text-align: left; color: var(--text-muted); padding: 8px 10px; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border); }
        .table td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03); word-break: break-all; }
        .table tr:hover td { background: rgba(255,255,255,0.02); }

        .chip {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }
        .chip-ip { background: rgba(56, 189, 248, 0.1); color: var(--cyan); border: 1px solid rgba(56, 189, 248, 0.2); }
        .chip-cmd { color: #f1f5f9; background: #1a202c; padding: 4px 8px; border-radius: 6px; display: inline-block; }

        .stream-list { list-style: none; font-family: 'JetBrains Mono', monospace; font-size: 12px; max-height: 440px; overflow-y: auto; }
        .stream-item { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.03); display: flex; gap: 10px; align-items: flex-start; }
        .stream-ts { color: var(--text-muted); flex-shrink: 0; }
        .stream-ev { color: var(--accent); font-weight: 600; flex-shrink: 0; }
        .stream-msg { color: var(--text); flex-grow: 1; word-break: break-all; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="title-group">
                <span class="pulse-dot"></span>
                <h1 style="font-size: 20px; font-weight: 700;" id="page-title">honeypot live</h1>
                <div class="node-pills">
                    <button class="node-pill active" id="btn-fleet" onclick="switchView('fleet')">🌐 Fleet</button>
                    <button class="node-pill" id="btn-sensor1" onclick="switchView('sensor1')">⚡ sensor1</button>
                    <button class="node-pill" id="btn-sensor2" onclick="switchView('sensor2')">🌸 sensor2</button>
                </div>
            </div>
            <div style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-muted);" id="refreshed-at">
                syncing...
            </div>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Events</div>
                <div class="stat-val" style="color: var(--cyan);" id="stat-total">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Unique Attackers</div>
                <div class="stat-val" style="color: var(--accent);" id="stat-ips">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Bot Logins Trapped</div>
                <div class="stat-val" style="color: var(--green);" id="stat-logins">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Reported Brute-Forces</div>
                <div class="stat-val" style="color: var(--rose);" id="stat-failed">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Commands Run</div>
                <div class="stat-val" style="color: var(--amber);" id="stat-cmds">--</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Malware Binaries</div>
                <div class="stat-val" style="color: var(--rose);" id="stat-downloads">--</div>
            </div>
        </div>

        <div id="map-container"></div>

        <div class="main-grid">
            <div>
                <div class="card">
                    <div class="card-title">
                        <span>⚡ Live Bot Command Execution</span>
                        <span class="node-pill" style="font-size: 11px; padding: 2px 8px; color: var(--green); border-color: rgba(74, 222, 128, 0.3);">interactive shell</span>
                    </div>
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Source IP & Country</th>
                                <th>Executed Command</th>
                            </tr>
                        </thead>
                        <tbody id="commands-body"></tbody>
                    </table>
                </div>

                <div class="card">
                    <div class="card-title">
                        <span>📦 Captured Malware Payloads (VirusTotal Scanned)</span>
                    </div>
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Source IP</th>
                                <th>Payload File</th>
                                <th>SHA256 Hash</th>
                            </tr>
                        </thead>
                        <tbody id="downloads-body"></tbody>
                    </table>
                </div>
            </div>

            <div>
                <div class="card">
                    <div class="card-title">
                        <span>🎯 Top Attacking Origins</span>
                    </div>
                    <table class="table">
                        <thead>
                            <tr><th>IP & Flag</th><th>Activity Count</th></tr>
                        </thead>
                        <tbody id="top-ips-body"></tbody>
                    </table>
                </div>

                <div class="card">
                    <div class="card-title">
                        <span>📡 Live Event Stream</span>
                    </div>
                    <ul class="stream-list" id="stream-list"></ul>
                </div>
            </div>
        </div>
    </div>

    <script>
        let map, markersLayer;
        let currentView = 'fleet';
        if (window.location.hostname.startsWith('sensor1.')) currentView = 'sensor1';
        else if (window.location.hostname.startsWith('sensor2.')) currentView = 'sensor2';

        function updateButtonStyles() {
            document.querySelectorAll('.node-pill').forEach(el => el.classList.remove('active'));
            const activeBtn = document.getElementById('btn-' + currentView);
            if (activeBtn) activeBtn.classList.add('active');
            
            const titleEl = document.getElementById('page-title');
            if (currentView === 'fleet') titleEl.textContent = 'honeypot fleet live';
            else if (currentView === 'sensor1') titleEl.textContent = 'sensor1 honeypot live';
            else if (currentView === 'sensor2') titleEl.textContent = 'sensor2 honeypot live';
        }

        function switchView(view) {
            currentView = view;
            updateButtonStyles();
            fetchStats();
        }

        function initMap() {
            map = L.map('map-container', {
                zoomControl: false,
                attributionControl: false
            }).setView([20, 0], 2);

            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 18,
                className: 'map-tiles-dark'
            }).addTo(map);

            markersLayer = L.layerGroup().addTo(map);
        }

        async function fetchStats() {
            try {
                let noctraData = null;
                let ariaData = null;

                // Fetch sensor1
                try {
                    const res = await fetch('https://sensor1.example.com/api/stats');
                    if (res.ok) noctraData = await res.json();
                } catch(e) {
                    try {
                        const res = await fetch('/api/stats');
                        if (res.ok) noctraData = await res.json();
                    } catch(e2) {}
                }

                // Fetch sensor2
                try {
                    const res = await fetch('https://sensor2.example.com/api/stats');
                    if (res.ok) ariaData = await res.json();
                } catch(e) {}

                let activeData = {};
                if (currentView === 'sensor1') {
                    activeData = noctraData || {};
                } else if (currentView === 'sensor2') {
                    activeData = ariaData || {};
                } else {
                    // Fleet aggregation (sensor1 + sensor2)
                    const n = noctraData || { total_events: 0, unique_ips_count: 0, logins_success: 0, logins_failed: 0, commands_count: 0, downloads_count: 0, top_ips: [], recent_commands: [], recent_downloads: [], recent_events: [], geo_markers: [] };
                    const a = ariaData || { total_events: 0, unique_ips_count: 0, logins_success: 0, logins_failed: 0, commands_count: 0, downloads_count: 0, top_ips: [], recent_commands: [], recent_downloads: [], recent_events: [], geo_markers: [] };
                    
                    activeData = {
                        total_events: (n.total_events || 0) + (a.total_events || 0),
                        unique_ips_count: (n.unique_ips_count || 0) + (a.unique_ips_count || 0),
                        logins_success: (n.logins_success || 0) + (a.logins_success || 0),
                        logins_failed: (n.logins_failed || 0) + (a.logins_failed || 0),
                        commands_count: (n.commands_count || 0) + (a.commands_count || 0),
                        downloads_count: (n.downloads_count || 0) + (a.downloads_count || 0),
                        top_ips: [...(n.top_ips || []), ...(a.top_ips || [])].sort((x, y) => y.count - x.count).slice(0, 12),
                        recent_commands: [...(a.recent_commands || []), ...(n.recent_commands || [])].slice(0, 15),
                        recent_downloads: [...(a.recent_downloads || []), ...(n.recent_downloads || [])].slice(0, 8),
                        recent_events: [...(a.recent_events || []), ...(n.recent_events || [])].slice(0, 25),
                        geo_markers: [...(n.geo_markers || []), ...(a.geo_markers || [])]
                    };
                }

                document.getElementById('stat-total').textContent = (activeData.total_events || 0).toLocaleString();
                document.getElementById('stat-ips').textContent = (activeData.unique_ips_count || 0).toLocaleString();
                document.getElementById('stat-logins').textContent = (activeData.logins_success || 0).toLocaleString();
                document.getElementById('stat-failed').textContent = (activeData.logins_failed || 0).toLocaleString();
                document.getElementById('stat-cmds').textContent = (activeData.commands_count || 0).toLocaleString();
                document.getElementById('stat-downloads').textContent = (activeData.downloads_count || 0).toLocaleString();
                document.getElementById('refreshed-at').textContent = 'live synced: ' + new Date().toLocaleTimeString();

                if (markersLayer) {
                    markersLayer.clearLayers();
                    (activeData.geo_markers || []).forEach(m => {
                        const marker = L.circleMarker([m.lat, m.lon], {
                            radius: Math.min(18, Math.max(6, Math.log(m.count) * 2.5)),
                            fillColor: '#f43f5e',
                            color: '#f472b6',
                            weight: 1.5,
                            opacity: 0.9,
                            fillOpacity: 0.6
                        });
                        marker.bindPopup(`<b>${m.flag || '🌐'} ${m.ip}</b><br>${m.country || 'Unknown'}<br><b>${m.count}</b> attacks`);
                        markersLayer.addLayer(marker);
                    });
                }

                const cmdsBody = document.getElementById('commands-body');
                cmdsBody.innerHTML = (activeData.recent_commands || []).map(c => `
                    <tr>
                        <td style="width: 170px;"><span class="chip chip-ip">${c.ip}</span></td>
                        <td><code class="chip-cmd">${escapeHtml(c.cmd)}</code></td>
                    </tr>
                `).join('') || '<tr><td colspan="2" style="color:var(--text-muted);text-align:center;">No commands yet</td></tr>';

                const dlBody = document.getElementById('downloads-body');
                dlBody.innerHTML = (activeData.recent_downloads || []).map(d => `
                    <tr>
                        <td><span class="chip chip-ip">${d.ip}</span></td>
                        <td><b style="color:var(--accent);">${escapeHtml(d.file || 'payload')}</b></td>
                        <td style="font-size:11px;color:var(--text-muted);">${(d.sha || '').substring(0, 16)}...</td>
                    </tr>
                `).join('') || '<tr><td colspan="3" style="color:var(--text-muted);text-align:center;">No payload downloads yet</td></tr>';

                const topIps = document.getElementById('top-ips-body');
                topIps.innerHTML = (activeData.top_ips || []).map(item => `
                    <tr>
                        <td><span class="chip chip-ip">${item.flag || '🌐'} ${item.ip}</span></td>
                        <td><b>${item.count}</b> hits</td>
                    </tr>
                `).join('');

                const streamList = document.getElementById('stream-list');
                streamList.innerHTML = (activeData.recent_events || []).map(e => `
                    <li class="stream-item">
                        <span class="stream-ts">${e.ts}</span>
                        <span class="stream-ev">${e.ev}</span>
                        <span class="stream-msg">${escapeHtml(e.msg || e.ip)}</span>
                    </li>
                `).join('');

            } catch (err) {
                console.error(err);
            }
        }

        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        }

        updateButtonStyles();
        initMap();
        fetchStats();
        setInterval(fetchStats, 3000);
    </script>
</body>
</html>
"""

class Handler(SimpleHTTPRequestHandler):
    def _send_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def _qint(self, query, key, default):
        try:
            return int((query.get(key) or [default])[0])
        except Exception:
            return default

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/api/stats":
            self._send_json(get_stats())
        elif route == "/api/summary":
            self._send_json(get_summary())
        elif route == "/api/timeseries":
            self._send_json(get_timeseries(self._qint(query, "hours", 168)))
        elif route == "/api/attackers":
            self._send_json(get_attackers(self._qint(query, "limit", 25)))
        # Flat, root-level arrays: Grafana's Infinity datasource here returns an
        # empty frame for nested arrays reached via root_selector, so panels must
        # not need one. These endpoints exist purely so panels can stay selector-free.
        elif route == "/api/flat/timeseries":
            self._send_json(get_timeseries(self._qint(query, "hours", 168))["buckets"])
        elif route == "/api/flat/attackers":
            self._send_json(get_attackers(self._qint(query, "limit", 25))["rows"])
        elif route == "/api/flat/events":
            self._send_json(get_stats().get("recent_events") or [])
        elif route == "/api/flat/commands":
            self._send_json(get_stats().get("recent_commands") or [])
        elif route == "/api/flat/downloads":
            self._send_json(get_stats().get("recent_downloads") or [])
        elif route == "/api/flat/geo":
            self._send_json(get_geo(self._qint(query, "limit", 150)))
        elif route == "/api/system":
            self._send_json(get_system())
        elif route == "/api/node":
            # "all" first: it is the fleet-wide merged view, not a relay target.
            self._send_json({"server": SERVER_NAME, "nodes": ["all"] + sorted(NODES)})
        elif route.startswith("/api/node/"):
            parts = route[len("/api/node/"):].split("/", 1)
            node = parts[0]
            rest = parts[1] if len(parts) > 1 else "summary"
            # A merge bug must never surface as an empty body: Grafana would then show a
            # silently blank panel instead of an error worth reading.
            try:
                self._send_json(fetch_node(node, rest, parsed.query))
            except Exception as exc:
                self._send_json({"error": "%s: %s" % (type(exc).__name__, exc),
                                 "node": node, "endpoint": rest})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

if __name__ == "__main__":
    # Threading, not HTTPServer: the aggregator relays other nodes AND can be asked
    # for this node, which on a single-threaded server deadlocks against itself.
    # Both the port AND the bind address are per-node configuration. A node the aggregator
    # reaches over the tailnet must listen on its tailnet IP; a node with a local nginx
    # wants 127.0.0.1 too. DASH_HOST takes a comma-separated list and binds each, which
    # satisfies both without ever binding 0.0.0.0 (that leaks the dashboard publicly).
    dash_port = int(os.environ.get("DASH_PORT", "8099"))
    hosts = [h.strip() for h in os.environ.get("DASH_HOST", "127.0.0.1").split(",") if h.strip()]
    servers = []
    for host in hosts:
        try:
            servers.append(ThreadingHTTPServer((host, dash_port), Handler))
            print("Serving on http://%s:%d (node=%s)" % (host, dash_port, SERVER_NAME))
        except OSError as exc:
            # One unusable address (e.g. no tailnet yet at boot) must not kill the node's
            # dashboard entirely; report the failure and keep the others serving.
            print("bind failed on %s:%d: %s" % (host, dash_port, exc))
    if not servers:
        raise SystemExit("no listen address available (DASH_HOST=%r port=%d)" % (hosts, dash_port))
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    threading.Thread(target=_fleet_prewarmer, daemon=True).start()
    threading.Thread(target=_geo_enricher, daemon=True).start()
    servers[0].serve_forever()
