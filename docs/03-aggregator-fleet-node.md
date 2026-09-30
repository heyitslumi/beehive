# Adding a fleet-wide "all" node to a fan-out aggregator

When several honeypot nodes are charted through one aggregator
(`<public-host>/api/node/<node>/<endpoint>`), users soon ask for a view that merges
everything. Implement it as a *node alias* on the aggregator — no Grafana surgery, no new
DNS: `fetch_node()` returns `fetch_fleet(...)` when the node is `all`/`fleet`/`*`.

## Merge rules per endpoint shape

| endpoint | rule |
| --- | --- |
| `summary` | sum every count; `unique_ips` needs a union (see below); min/max of first/last_seen |
| `flat/timeseries` | sum buckets by `ts`; `unique_ips` per hour can only be floored |
| `flat/attackers` | key by IP, sum counters, min first_seen / max last_seen, then re-limit |
| `flat/geo` | key by IP, sum counters, keep lat/lon/country from the first sighting |
| `flat/commands`, `flat/downloads` | concatenate, sort by ISO `ts` desc, take the limit |
| `flat/events` | round-robin, one row per node in turn |

Fan the per-node requests out with `ThreadPoolExecutor(max_workers=len(NODES))` — nine
sequential 20s relays turn every panel load into a stall. Cache the merged result briefly
(~15s) because several panels hit the same endpoint at once.

## Pitfalls that cost real debugging time

- **Wrapped vs bare shapes.** `/timeseries` returns `{"buckets": [...]}` while
  `/flat/timeseries` returns a *bare list* of the same rows. A merge that assumes one shape
  raises inside the handler and surfaces as an **empty body** — Grafana shows a blank panel
  and no error. Handle both (`buckets = p if isinstance(p, list) else p.get("buckets")`),
  and wrap the responder in try/except returning `{"error": ...}` so a merge bug is visible.
- **Counts cannot be unioned.** Per-node `unique_ips` counts must not be summed (the same
  botnet hits every node → inflation). A union needs per-IP data, and the node APIs clamp
  their attacker list to the top 200, so the fleet union is a **floor**: report it alongside
  the sum of per-node counts as a **ceiling** (`unique_ips_floor` / `unique_ips_ceiling`).
  Say so in the panel description instead of presenting a guess as a fact.
- **A node that fails shrinks the union silently.** Publishing the count of contributing
  nodes (`unique_ips_nodes: "8/9"` + `unique_ips_missing: ["sensor1"]`) turns an invisible
  data hole into a visible one — that is how the wobbling IP total was found. Retry the
  failures with a longer timeout: a cold read of a busy node's whole 8-day window can
  exceed the normal relay timeout, and that is precisely the case that drops nodes.
- **Verify the merge arithmetically.** Sum the per-node payloads yourself and assert
  equality with the merged payload for every additive field; "the endpoint returned 200"
  proves nothing about whether the numbers are right. Do it over the *public* path
  (`https://<host>/api/node/all/...`), not localhost — that is the path Grafana uses.

## The same trick for host vitals (no agent, no exporter)

Before reaching for Alloy/exporter infrastructure, check what the fleet already reports.
A `/api/system` endpoint added to each node's dashboard — reading `/proc/stat`, `/proc/meminfo`,
`/proc/loadavg`, `/proc/uptime` and `os.statvfs("/")` — gives CPU %, RAM used/total, disk
used/free and load per core with nothing installed. The aggregator's `all` view then returns
`{nodes: [one row per node], worst_disk_pct, worst_disk_node, total_disk_free_gb, ...}` for a
single Infinity table, sorted fullest-disk-first.

Include `load1_per_core`, not raw load: 0.26 on a 2-core box and 0.26 on a 24-core box are
completely different situations, and a fleet table is where that gets noticed.

`cpu_pct` needs two `/proc/stat` samples, so it costs one `time.sleep(0.25)` per request —
acceptable for a 1m refresh, and the only way to get a real percentage rather than a guess.

**Limit to state honestly:** this is a liveness-and-capacity view. Infinity stores no history,
so trends and alert rules still need a real time-series backend (Alloy + remote_write).

## Patching a fleet of dashboards whose route chains differ

The aggregator host has `/api/node` routes; plain node dashboards do not — their chain ends at
their last JSON endpoint and then the HTML fallback. A patcher anchored only on the aggregator's
route silently fails on most of the fleet. Anchor on the route that exists on both, or fall back:
try `elif route == "/api/node":`, else find the LAST `elif route == "/api/flat/..."` and insert
before the following `else:`. Log per-node success/failure and re-run only the failures —
"5 of 9 patched" is the signal that the anchor is wrong, not that the nodes are broken.

## Grafana side

If the node variable is `type: custom`, add the alias to `query` **and** `options`, then set
`current`. Use the Grafana MCP's patch mode (`update_dashboard` with `operations`) rather
than re-uploading the whole dashboard: `$.templating.list[0].query`,
`$.templating.list[0].options`, `$.panels[7].description`, etc. Verify with
`get_dashboard_summary` — its `panels[].description` values show whether each JSONPath
index resolved to the panel you meant (off-by-one indexing is the usual mistake).

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · <a href="./README.md">docs index</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
