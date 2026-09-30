# Cowrie dashboards, enrichment, and aggregation

## Dashboard server shape

A single stdlib `http.server` in one file, run under systemd, listening on `127.0.0.1` only and proxied by nginx. Two routes:

- `/api/stats` — JSON. Send `Access-Control-Allow-Origin: *` (plus `Access-Control-Allow-Methods: GET, OPTIONS`). Required as soon as any second host aggregates this node.
- everything else — the HTML, with the data fetched client-side on an interval.

Parse the JSON event log (`/root/cowrie/var/log/cowrie/cowrie.json`) in one pass:

| Purpose | Rule |
|---|---|
| totals (events, IPs, logins ok/failed, commands, downloads) | count over the **whole file** |
| recent commands / downloads / event stream | slice a tail (last few hundred lines) then take the newest N |
| per-IP activity | `Counter` over all lines, then `most_common(n)` |

Event IDs that carry the interesting data: `cowrie.command.input` (`input`), `cowrie.session.file_download` (`url`, `destfile`, `shasum`), `cowrie.login.success` / `cowrie.login.failed` (`username`, `password`), plus `cowrie.session.connect` / `session.closed` for the livestream.

Keep one local port per node and check it is free (`ss -tlnp`) — a busy port makes the service fail silently behind a proxy that still answers 200 from something else.

## GeoIP enrichment

Resolve attacker IPs to country + lat/lon for flags and map markers. Cache per IP in-process; the lookup is on the request path, so give it a short timeout and fall back to `Unknown` rather than stalling the endpoint.

- Skip private prefixes (`10.`, `172.`, `192.168.`, `127.`) before calling out.
- Emit a `geo_markers` array (ip, count, lat, lon, country, flag) alongside `top_ips` so the map layer can be driven from the same payload.
- Size markers by `log(count)` with a floor, not linearly — one botnet IP with thousands of hits would otherwise dwarf the map.

## Aggregate view

A "fleet" page fetches every node's `/api/stats` cross-origin and sums the totals, merges `recent_*` arrays newest-first, and concatenates `geo_markers`. Two failure modes to guard:

1. Missing CORS header on any node → that node's fetch throws, and a `try/catch` that swallows it produces a page that quietly shows only the local node. Log the failure rather than swallowing it silently.
2. A switcher built from anchors instead of buttons reloads and always renders the same view.

## Honeyfiles

Decoy credential files in the virtual filesystem (e.g. `/root/cowrie/honeyfs/root/`) that bots grep for and try to exfiltrate: a `.env` with fake DB/AWS/Stripe keys, an `id_rsa` with a fake PRIVATE KEY block, a wallet seed-phrase file. They only get touched on nodes whose `userdb.txt` accepts credentials — on a reject-everything node they are inert.

## Retention

A daily cron that prunes downloads/tty recordings older than a couple of weeks and truncates the JSON log. Without it the log the dashboard re-parses every few seconds grows unbounded and the endpoint gets slower over time.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · <a href="./README.md">docs index</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
