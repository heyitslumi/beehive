# Documentation

Ten pages, in the order you will actually need them. Each one exists because something broke,
and most of them say so.

## Start here

| page | what it covers |
| --- | --- |
| **[00 — Quickstart](00-quickstart.md)** | a bare VPS to your first captured login, in the order that avoids locking yourself out. **Read this first.** |
| [01 — Fleet deployment](01-fleet-deployment.md) | deploying to nodes 2..N, the per-node traps, and the outage that made per-node values environment variables |

## The interesting bits

| page | what it covers |
| --- | --- |
| [02 — Moving a public IP](02-moving-a-public-ip.md) | the headline trick: carrying a public IPv4 to a host in another datacenter **without losing the attacker's source IP**. Includes why the obvious VPN approach silently fails |
| [03 — Aggregator and the `all` node](03-aggregator-fleet-node.md) | one `/api/node/<name>/…` surface for the whole fleet, and why counts that cannot be unioned are reported as floors rather than facts |
| [04 — Geo and latency](04-geo-and-latency.md) | two failure modes that look identical from a Grafana panel, offline MMDB enrichment, and keeping panels off rate-limited APIs |

## Running it

| page | what it covers |
| --- | --- |
| [05 — Alloy fleet metrics](05-alloy-fleet-metrics.md) | real time series for a mixed fleet: installing Alloy, shipping host metrics to Grafana Cloud, and the staging mistakes that fail *silently* |
| [06 — Grafana alerting](06-grafana-alerting.md) | writing alert rules and contact points with a limited API token — including the two mistakes everybody makes (`instant: true`, and alerts that must fire on absence) |
| [07 — Discord alerts](07-discord-alerts.md) | one channel per node, and an event allowlist so the channel gets logins, not every keystroke a bot types in the sandbox |
| [08 — Log-window rotation](08-log-window-rotation.md) | Cowrie rotates its log at midnight; this is why your dashboard empties itself at 00:00, and how to read the whole window instead |
| [09 — Dashboards](09-dashboards.md) | the dashboard server's shape, enrichment, and aggregation |

## Conventions used throughout

- **Addresses are placeholders.** Nodes are `sensor1..sensor9`; public addresses come from the
  documentation ranges (`198.51.100.0/24`, `203.0.113.0/24`) and private ones from CGNAT space
  (`100.64.0.11/10`). The mapping is cosmetic — the design is unchanged.
- **Credentials are placeholders too.** Never commit a real one; `scripts/scrub-check.sh` is the
  gate, and `CONTRIBUTING.md` says what it looks for.
- **`active` is not proof.** Several pages say this because it cost real hours: `systemctl
  is-active` can report `active` for a service that is crash-looping. Check `NRestarts`, the
  actual bind (`ss -tlnp`), and a real request.

---

<div align="center">
  <sub>ten pages, one honeypot fleet 🐝</sub>
</div>
