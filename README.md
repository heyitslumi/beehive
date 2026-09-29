# beehive

A fleet of SSH/Telnet honeypots that report to one dashboard, plus the operational
scaffolding to run it: aggregation, offline geo/ASN enrichment, Grafana dashboards,
Discord alerting, and a technique for moving a public IPv4 onto a host in another
datacenter **without losing the attacker's source IP**.

Nine sensors. One hub. It has been running in production long enough to have collected
~335,000 events, ~32,000 successful bot logins and ~3,200 malware payloads — which is
the real reason this repository exists in the shape it does.

## What's actually interesting here

Cowrie itself is a well-known, mature honeypot. This repository is not a fork of it; it
is the part that is tedious, undocumented and specific to running **more than one** of
them:

- **[Moving a public IP to another host with source IPs preserved](docs/02-moving-a-public-ip.md)**
  — the headline trick. A VPN cannot carry an arbitrary internet client's source address
  (Tailscale validates sources and silently drops the packet, which looks exactly like a
  firewall but matches no rule). What works is a TUN pair nested inside an SSH session
  dialled the direction the network *actually* allows, with the far end configuring *both*
  ends on every start. No SNAT, so the honeypot logs the attacker's real address.
- **[The fleet aggregator](docs/03-aggregator-fleet-node.md)** — one `/api/node/<name>/…`
  endpoint that merges per-sensor JSON, plus an `all` alias that behaves like a node.
  Counts that cannot be unioned honestly are reported as floors, not as facts.
- **[Geo and latency](docs/04-geo-and-latency.md)** — a local DB-IP MMDB for sub-millisecond
  lookups (no API, no rate limit) with ipinfo layered on top behind a persistent disk cache.
  Panels never wait on a remote API.
- **[Grafana alerting](docs/06-grafana-alerting.md)** — including the two rules that cost
  real debugging time: alert queries need `instant: true` or they report healthy while
  erroring, and "node stopped reporting" has to fire on *absence* or a dead box never pages.
- **[Discord alerts and event filtering](docs/07-discord-alerts.md)** — an allowlist so the
  channel gets connections and logins, not every keystroke a bot types inside the sandbox.
- **[Fleet deployment](docs/01-fleet-deployment.md)** — and, in "pushing a code change to a
  fleet that has per-node constants", the outage this repository's design exists to prevent.

## Architecture

```
                     internet
                        |
        +---------------+----------------+
        |  :22 cowrie (one per sensor)    |   <- real sshd moved to __ADMIN_SSH_PORT__
        +---------------+----------------+
                        |
   sensor1 .... sensor8  (cowrie + own dashboard, bound to tailnet only)
        |                             \
        | tailnet                      \  sensor9 held a public IPv4 belonging to a
        |                               \  sibling's network segment, carried over a
        +-----> sensor1 aggregator <-----+  TUN inside SSH (docs/02)
                     |
        /api/node/<name>/summary|stats|geo|timeseries|system
                     |
        +------------+-------------+
        |  Grafana Cloud dashboards |
        |  fleet-health  (host)     |
        |  fleet-metrics (history)  |
        |  cowrie-x      (per node) |
        +------------+-------------+
                     |
                 Discord  (alerts + per-sensor event channels)
```

Every sensor dashboard binds either loopback or its **tailnet address — never `0.0.0.0`**.
A honeypot dashboard publicly reachable is an oxymoron, and one of the bugs documented in
`docs/01` is exactly that.

## Layout

| path | what it is |
| --- | --- |
| `docs/00-quickstart.md` | **bare VPS to first captured login** — start here |
| `aggregator/` | the fleet merge service, its systemd unit, the fleet map |
| `cowrie/` | a scrubbed `cowrie.cfg.example`, the patched Discord output module, bait files |
| `deploy/` | the actual ordered deploy recipes, scrubbed of credentials |
| `docs/` | the operational write-ups — the reason to read this repo |
| `grafana/` | dashboard JSON, plus panel-by-panel queries and the alert rules |
| `systemd/` | unit files, including the public-IP transit pair |
| `scripts/scrub-check.sh` | run before every push: fails on credential-shaped strings |
| `SECURITY.md` | what a "vulnerability" even means for a honeypot |

## Standing up your own

**Start with [`docs/00-quickstart.md`](docs/00-quickstart.md)** — bare VPS to first captured
login, written in the order that avoids locking yourself out.

The recipes in `deploy/` are numbered in the order they were run:

1. `10-ssh-port.sh` — move the real sshd to a non-standard port and **verify with a fresh
   connection** before freeing `:22`. Getting this wrong locks you out of the box.
2. `20-deploy.sh` — install cowrie, apply the hostname/banner, wire outputs.
3. `70/71` — install the Discord event filter; `50/51` — wire per-sensor webhooks.
4. `30/40` — log-window rotation and output normalisation.
5. `90-deploy-alloy.sh` — host metrics to Grafana Cloud.
6. `95/96/97` — offline geo database, rollout, and its monthly refresh timer.
7. `verify-*.sh` / `verify-dashboard-contract.py` — the checks that tell you the panels
   will actually have data in them, before you go looking at Grafana and guess.

`deploy/101-restore-node-binds.sh` exists because pushing a dev copy of the aggregator to
the fleet silently changed a **hardcoded port and bind address** on every sensor and took
eight of nine dashboards down. Per-node values now come from `DASH_PORT` / `DASH_HOST`
environment variables set in a systemd drop-in, and `systemctl is-active` is not considered
proof of anything (`NRestarts` is).

## Notes on what is *not* in this repository

Deliberately, and permanently:

- **no credentials** — webhook URLs, API tokens and keys are placeholders
- **no captured payloads or logs** — that is other people's malware and other people's IP
  addresses; it does not belong in git and would get the repository taken down
- **no geo database** — 127MB, third-party, with its own attribution terms. Download it.
- host names here are `sensor1..sensor9` and addresses come from the documentation ranges
  (`198.51.100.0/24`) and CGNAT space (`100.64.0.11/10`). The mapping is cosmetic; the
  design is unchanged.

## If you run this

A honeypot records real people's IP addresses, and some of those people are not attackers
(a compromised host, a scanner, a university proxy). Decide up front what you log, how long
you keep it, and whether you forward to services like AbuseIPDB — and check what your
jurisdiction expects in the way of retention and disclosure. Running a honeypot on a host
whose provider you have not cleared it with is a fast way to have a bad week.

## Credit

[Cowrie](https://github.com/cowrie/cowrie) (BSD-3-Clause) does the actual trapping —
read their project first. `cowrie/discord.py` here is a modified copy of Cowrie's Discord
output module, carrying its licence; see `NOTICE`. Offline geo data from
[DB-IP](https://db-ip.com).

MIT for everything original. See `LICENSE`.
