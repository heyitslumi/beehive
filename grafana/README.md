# Dashboards

Three dashboards ship with this project:

| dashboard | datasource | what it answers |
| --- | --- | --- |
| `cowrie-dashboard.json` | Infinity | one sensor: events, logins, attackers, world map |
| `fleet-health` | Infinity | host vitals for the whole fleet, straight from `/api/system` |
| `fleet-metrics` | Prometheus | the *history*: cpu/mem/disk/load/network over time |

`cowrie-dashboard.json` imports directly (**Dashboards → New → Import**). The other two are
documented here panel-by-panel with their exact queries, because a dashboard export is
mostly layout JSON and the part you actually want to copy is the queries below.

## Datasources you need first

1. **`yesoreyeram-infinity-datasource`** (Infinity) — for the aggregator's JSON API. Point it
   at your aggregator base URL and allow it, nothing else.
2. **Prometheus** — Grafana Cloud provides one, or point at your own. This is where Alloy
   remote-writes host metrics.

Every panel below is labelled with which of the two it uses.

## `fleet-metrics` — host history (Prometheus)

Template variable: `node`, a multi-select query variable over
`label_values(node_cpu_seconds_total, node)` with **All** enabled. Every query uses
`node=~"${node:regex}"` — the `:regex` formatter, not the plain one, because the plain form
produces an invalid matcher when All is selected.

- **cpu used %** — `100 * (1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle",node=~"${node:regex}"}[5m])))`
- **memory used %** — two targets, so swap shows dashed against RAM:
  - RAM: `100 * (1 - (sum by (node) (node_memory_MemAvailable_bytes{node=~"${node:regex}"}) / sum by (node) (node_memory_MemTotal_bytes{node=~"${node:regex}"})))`
  - swap: `100 * (1 - (sum by (node) (node_memory_SwapFree_bytes{node=~"${node:regex}"}) / sum by (node) (node_memory_SwapTotal_bytes{node=~"${node:regex}"} > 0)))`

    The `> 0` is not decoration. A node with swap disabled gives `0/0`, which is `NaN`, and a
    `NaN` series poisons the legend with a null entry. Filter the denominator.
- **root filesystem used %** — with threshold lines at 75 and 85:
  `100 * (1 - (sum by (node) (node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs",node=~"${node:regex}"}) / sum by (node) (node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs",node=~"${node:regex}"})))`

  Exclude `tmpfs|overlay|squashfs` or every container mount appears as a separate, alarming line.
- **disk free (GiB)** — `sum by (node) (node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs",node=~"${node:regex}"}) / 1024^3`
- **load per core (1m)** — `sum by (node) (node_load1{node=~"${node:regex}"}) / count by (node) (node_cpu_seconds_total{mode="idle",node=~"${node:regex}"})`

  Per core, not the raw load average. Load 8 is fine on 16 cores and fatal on one.
- **network throughput (MB/s)** — two targets:
  - in: `sum by (node) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*|tailscale.*",node=~"${node:regex}"}[5m])) / 1024 / 1024`
  - out: same with `node_network_transmit_bytes_total`

  The device filter matters: without it, container veth pairs count the same traffic repeatedly.

## `fleet-health` — fleet vitals (Infinity, no agent required)

Every panel reads one endpoint: `GET {aggregator}/api/node/all/system`. Because the
aggregator answers for a named node, the `all` alias, or any subset, these panels stay
correct as the fleet changes size. The aggregator reads `/proc` directly, so there is
**nothing to install on the hosts** — the aggregator just has to be able to reach them.

The table panel selects `root_selector: nodes` with columns `node`, `cpu_pct`,
`load1_per_core`, `mem_pct`, `mem_used_gb`, `disk_pct`, `disk_free_gb`, `disk_total_gb`,
`cpu_cores`, `uptime_days`, `checked`. The rest are single-value stats (worst disk, fullest
node, fleet RAM, fleet cores) reading the same JSON.

One thing to know: the stat panels showing **fleet totals** only return their fields when the
URL says `node=all`. Point them at a single node and they go blank. That is not a bug, and it
is why the fleet-level stats are wired to the `all` endpoint specifically.

## Notes that cost time to learn

- **Alert queries need `instant: true`.** Without it a rule reports health `ok` while erroring
  and never fires. See `docs/06-grafana-alerting.md`.
- **A graph with no data is not a graph with zero.** Before debugging a panel, curl the API it
  reads. Every "the dashboard is broken" incident here was the API returning empty, not the
  panel being wrong.
- **Fleet merges are slow when cold.** If many panels query a merged endpoint simultaneously,
  the first load can take seconds and Grafana quietly gives up on the slow panel. Cache the
  merge and pre-warm the cheap endpoints (`docs/04`).
