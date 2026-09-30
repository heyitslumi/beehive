<div align="center">
  <img src="assets/banner.png" alt="beehive — a fleet of SSH honeypots reporting to one hub" width="100%">

  <h1 align="center">beehive</h1>

  <p align="center">
    <strong>A fleet of SSH/Telnet honeypots that report to one dashboard.</strong><br>
    Nine sensors, one hub, offline geo enrichment, and the operational scaffolding to keep it running.
  </p>

  <p align="center">
    <img src="https://shieldcn.dev/group/github/stars/heyitslumi/beehive+github/forks/heyitslumi/beehive+github/license/heyitslumi/beehive.svg?variant=outline" alt="stars, forks, license">
  </p>

  <p align="center">
    <img src="https://www.shieldcn.dev/github/last-commit/heyitslumi/beehive.svg?variant=secondary" alt="last commit">
    <img src="https://www.shieldcn.dev/github/issues/heyitslumi/beehive.svg?variant=secondary" alt="issues">
  </p>

  <br/>
</div>

## Features

beehive is not a fork of [Cowrie](https://github.com/cowrie/cowrie) — Cowrie does the actual
trapping, and you should read their project first. This repository is the part that is
tedious, undocumented and specific to running **more than one** of them.

- **Nine sensors, one hub** — one `/api/node/<name>/…` endpoint merges every sensor's JSON, plus an `all` alias that behaves like a node. Counts that cannot be unioned honestly are reported as floors, not as facts.
- **Source-IP-preserving transit** — move a public IPv4 onto a host in another datacenter without losing the attacker's real address. No SNAT.
- **Offline geo and ASN enrichment** — a local DB-IP MMDB for sub-millisecond lookups: no API, no rate limit, no key. Panels never wait on a remote service.
- **Grafana dashboards** — per-sensor attack views, a fleet-wide attacker world map, and host vitals read straight from each node's `/proc`. No agent, no exporter.
- **Discord alerting** — an event allowlist so the channel gets connections and logins, not every keystroke a bot types inside the sandbox.
- **Provider-agnostic** — bare Debian VPSes, Docker, systemd, any private network. Some nodes have no inbound HTTPS at all.
- **Battle-tested rather than demoed** — running in production long enough to have collected ~335,000 events, ~32,000 successful bot logins and ~3,200 malware payloads. Most of the design exists because of something in that data.

## How it works

Every sensor runs Cowrie on `:22` with the real `sshd` moved to an admin port, and serves its
own dashboard JSON on loopback or its tailnet address — **never `0.0.0.0`**. One node also acts
as the aggregator: it reaches the others over the private network and merges their JSON behind
a single `/api/node/<name>/…` surface, so dashboards and browsers never need per-node
certificates or cross-origin fetches.

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

## Getting started

### Prerequisites

- A VPS you are **allowed** to run a honeypot on, with a second access method before you touch SSH
- Docker and Docker Compose
- Optional: a private network (Tailscale or equivalent) once you go past one sensor
- Optional: a Grafana Cloud account, for dashboards and alerting

### Install

**Start with [`docs/00-quickstart.md`](docs/00-quickstart.md)** — bare VPS to first captured
login, written in the order that avoids locking yourself out.

```bash
git clone https://github.com/heyitslumi/beehive.git
cd beehive
```

The short version, so you know where you are going:

1. Move the real `sshd` off `:22` and **verify from a fresh connection** before freeing the port.
2. Install Cowrie, apply the hostname and banner, wire the log outputs.
3. Add the dashboard service on loopback, and verify the API contract.
4. Repeat 1–3 per sensor, then point the aggregator at them.

Do **not** skip step 1. It is the single most common way to lock yourself out of the box.

## What's actually interesting here

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

## Project structure

```bash
beehive/
├── docs/         # the operational write-ups — the reason to read this repo
│   └── 00-quickstart.md    # bare VPS to first captured login. start here
├── aggregator/   # the fleet merge service, its systemd unit, the fleet map
├── cowrie/       # scrubbed cowrie.cfg.example, patched Discord module, bait files
├── deploy/       # the ordered deploy recipes, scrubbed of credentials
├── grafana/      # dashboard JSON, panel-by-panel queries, alert rules
├── systemd/      # unit files, including the public-IP transit pair
├── assets/       # banner
└── scripts/      # scrub-check.sh — run before every push
```

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

## 🤝 Contributing

Issues and pull requests are welcome — [CONTRIBUTING.md](CONTRIBUTING.md) covers what makes a
change mergeable here, and [SECURITY.md](SECURITY.md) covers what counts as a vulnerability
in a honeypot (less than you would think, by design).

## 📄 License

Original work is MIT — see [LICENSE](LICENSE).

[Cowrie](https://github.com/cowrie/cowrie) (BSD-3-Clause) does the actual trapping.
`cowrie/discord.py` here is a modified copy of Cowrie's Discord output module and carries its
licence; see [NOTICE](NOTICE). Offline geo data from [DB-IP](https://db-ip.com).

---

<div align="center">
  <sub>pointed at the internet on purpose, since it works better that way 🐝</sub>
</div>
