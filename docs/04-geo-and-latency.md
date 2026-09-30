# Geo lookups and aggregator latency

Two failure modes that look identical from a Grafana dashboard (panels render empty
or crawl) and are both self-inflicted.

## Never call a rate-limited geo API from the render path

ip-api's single-lookup endpoint allows **45 requests/min**, so a 150-marker geomap
cannot be served on demand, and every panel that needs coordinates queues behind it.
The fix is a local MMDB: **DB-IP Lite city** (`https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz`,
no signup, refreshed monthly) read with `maxminddb` (Debian: `python3-maxminddb`).
Measured ~30us per lookup, ~135,000 lookups/sec -- so the on-demand path is free and
the API disappears from the critical path entirely.

Notes that cost time to learn:
- Ship it gzipped (60MB vs 127MB) and decompress on arrival; a 9-node fleet is otherwise
  ~1.1GB of tailnet transfer.
- Resolve the database path with a **glob** (`dbip-city-lite*.mmdb`) so the monthly
  refresh is "drop the new file in and restart", with no config edit.
- A node without the database silently falls back to HTTP: return None from the loader
  rather than raising, and keep the old path as a fallback.

## Enrich separately, cache forever

ipinfo.io adds `org` (ASN + owner: hosting provider, VPN, Tor exit) and city, which
DB-IP Lite cannot. It **works without a token** (lower quota) and its batch endpoint is
paid, so treat it as enrichment, not transport: a background worker enriches the
heaviest attackers a few per cycle, capped at ~30/hour (~720/day), writing every answer
to a persistent JSON file. An IP is then looked up once in the life of the box -- not
per restart, never per page view.

- Overlay enrichment **on read**, not only when a record is first cached. Otherwise a
  record cached before the enrichment ran keeps empty org/city until the process
  restarts (the in-memory and on-disk caches live on different clocks).
- Prefer the API's coordinates/country when it has them: DB-IP Lite labelled a
  UK-hosted range as United States while ipinfo returned London/Hilversum.

## Warm merged endpoints -- but only the cheap ones

The first request for a merged (`all`) endpoint pays the full fleet fan-out: measured
**7s cold vs 0.2-0.4s warm**. A dashboard fires ~10 endpoints within the same second,
so with a cold cache every panel waits on its own merge and slow ones get dropped by
Grafana -- panels render empty while every node is healthy.

Warm them in a background thread just inside the cache TTL, and keep the warm list to
cheap endpoints. Warming `stats` and the geo endpoint (both fan out to rate-limited
upstreams) starves the cheap ones: that mistake produced a **75-second hang on
`/summary`** while the endpoints it was blocking answered in milliseconds.

## Verify from the consumer's side

`curl` on the host proves the service is up, not that the panel works. Ask Grafana to
run the panel's own target:

```
POST /api/ds/query   {"from":"now-6h","to":"now","queries":[{refId, datasource, format,
                     parser:"backend", source:"url", type:"json", url, url_options}]}
jq: [.results | to_entries[] | {refId:.key, status:.value.status,
      frames:(.value.frames|length), error:.value.error,
      firstField:(.value.frames[0].schema.fields[0].name // null),
      rows:(.value.frames[0].data.values[0]|length // 0)}]
```

Batch several panel targets into one call. `status: 200` with the expected row count
(e.g. 150 geo markers, 170 timeseries buckets) is the only evidence that matters.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
