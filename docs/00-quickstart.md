# Quickstart: from a bare VPS to your first captured login

This walks one sensor, end to end, then shows how to add eight more and get them into
Grafana. It works on Debian 12/13 and Ubuntu 22.04+/24.04, assumes root, and is written in
the order that avoids locking yourself out. The distributions differ in exactly one place —
the name of the compose package — and step 2 gives the portable route plus both distro names.

Budget an evening for the first one. The second takes ten minutes.

> **Before anything else:** clear this with your hosting provider. A honeypot that
> attracts brute-force traffic will generate abuse complaints aimed at *you* at some
> point, and some providers will simply suspend the box. Also read the "If you run this"
> section of the README — you are about to record real people's IP addresses.

## 0. What you are building

```
internet --> :22  -->  docker: cowrie/cowrie  -->  json log  -->  dashboard  -->  you
                          (fake shell, fake files)
```

Cowrie answers as a real SSH server, accepts logins from a fake user database, then drops
the "attacker" into a sandboxed Python shell. Everything is logged as JSON. It never gives
anyone a real shell.

## 1. Move your real sshd **first** (the step that locks people out)

`:22` is about to belong to the honeypot, so your actual admin login has to move. Do this
before cowrie touches the port, and verify it properly.

```bash
# pick a port nobody scans, e.g. 2200 -- the deploy scripts call this __ADMIN_SSH_PORT__
cat >/etc/ssh/sshd_config.d/10-admin-port.conf <<'EOF'
Port 2200
EOF

sshd -t                     # MUST print nothing. a syntax error here is a lockout.
systemctl restart ssh
ss -tlnp | grep -E ':22|:2200'   # 2200 listening, 22 still yours until step 2
```

**Now open a second terminal and confirm you can still log in on 2200.** Do not skip this.
If you are on a cloud provider with a firewall or security group, open 2200 there too, and
keep your current session open until the new one is proven.

Only once 2200 works:

```bash
# free :22 for cowrie (and :23 if you want telnet traps)
ss -tlnp | grep ':22 ' || echo "22 is free"
```

`deploy/10-ssh-port.sh` in this repository does exactly this, with the verification built in.

## 2. Install cowrie

```bash
apt-get update && apt-get install -y git

# Docker. The official convenience script installs Docker Engine + the compose plugin
# on both Debian and Ubuntu with one command. It is the shortest reliable path.
curl -fsSL https://get.docker.com | sh

# Verify before going further. "docker: 'compose' is not a docker command" means
# the plugin was not installed, and this catches it immediately.
docker --version
docker compose version

mkdir -p /root/cowrie/etc /root/cowrie/var/log/cowrie /root/cowrie/var/lib/cowrie
mkdir -p /root/cowrie/var/lib/cowrie/tty /root/cowrie/var/lib/cowrie/state /root/cowrie/var/lib/cowrie/downloads
mkdir -p /root/cowrie/honeyfs/root

# IMPORTANT. The image runs as uid:gid 999:999 (its own `cowrie` user) and has no PUID
# support, but you just created these directories as root. Cowrie will not be able to
# write into them, and you will get:
#   PermissionError: [Errno 13] Permission denied: 'var/lib/cowrie/state'
#   Failed to load output engine: jsonlog
# Give the container's uid ownership of the two directories it writes to:
chown -R 999:999 /root/cowrie/var

cd /root/cowrie
```

Config files stay root-owned and mode 644 — they are mounted read-only, so that is correct.
Only `var/` needs the chown.

A minimal `docker-compose.yml` — note the two bind-mounts that matter, one config and one
for the patched Discord output module, so it survives container recreation:

```yaml
services:
  cowrie:
    image: cowrie/cowrie:latest
    container_name: cowrie
    restart: unless-stopped
    ports:
      - "22:2222"     # ssh trap
      - "23:2223"     # telnet trap
    volumes:
      - ./etc/cowrie.cfg:/cowrie/cowrie-git/etc/cowrie.cfg:ro
      - ./etc/userdb.txt:/cowrie/cowrie-git/etc/userdb.txt:ro
      - ./etc/discord.py:/cowrie/cowrie-git/src/cowrie/output/discord.py:ro
      - ./var/log/cowrie:/cowrie/cowrie-git/var/log/cowrie
      - ./var/lib/cowrie:/cowrie/cowrie-git/var/lib/cowrie
```

Copy `cowrie/cowrie.cfg.example` to `etc/cowrie.cfg`, copy `cowrie/discord.py` to
`etc/discord.py`, then start it.

**Start with `jsonlog` only.** The example config ships with the abuseipdb, virustotal and
discord outputs *disabled* on purpose: every one of them makes an outbound HTTP call while
handling an event, and if your egress is filtered or slow, a session can stall while one of
them waits. Get the honeypot capturing to a log file first, confirm that works, and only then
enable the extras — one at a time, with a real key in each — so that when something goes
quiet you know which one did it.

```bash
docker compose config -q && echo "config ok"
docker compose up -d
docker compose logs -f | head -20     # you want: ready to accept connections
```

### Make it look like something worth attacking

`cowrie.cfg` sets the hostname and the SSH banner. Two rules:

- **match a real, current distro version.** A banner announcing a three-year-old OpenSSH
  is a tell. `SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3` is the kind of thing you want.
- **do not make it too interesting.** A box that looks like a juicy corporate database
  invites people who are actually good at this.

### How decoy files (honeyfs) actually work in Cowrie

A common trap: creating files under `honeyfs/root/` and expecting them to appear in `ls /root`
when you log in. **They will not show up.**

Cowrie's simulated filesystem has two distinct parts:
1. **The virtual tree (`fs.pickle`):** Cowrie does not scan your host disk for `ls`. Directory
   listings and file metadata (permissions, owner, size) are loaded entirely from an internal
   serialized filesystem (`fs.pickle`). By default, `/root` only contains `.bashrc` and `.profile`.
2. **File contents (`honeyfs`):** When an attacker reads a file (`cat /etc/passwd`), Cowrie
   checks `honeyfs` for custom content. If present, it serves that content; otherwise it falls
   back to the default pickle bytes.

**To customize existing files (like `/etc/motd` or `/etc/issue.net`):**
Mount `./honeyfs` into the container in `docker-compose.yml`:
```yaml
    volumes:
      ...
      - ./honeyfs:/cowrie/cowrie-git/honeyfs:ro
```
Set `contents_path = honeyfs` under `[honeypot]` in `cowrie.cfg`, and place your custom file at
`honeyfs/etc/motd`.

**To make brand-new files (like `/root/wallet_backup.txt`) appear in `ls`:**
Because `ls` reads `fs.pickle`, new file paths must be registered in the pickle itself using
Cowrie's `fsctl` tool (`fsctl fs.pickle` -> `touch /root/wallet_backup.txt` -> `load ...`). Without
a pickle entry, Cowrie does not know the file exists and `ls` will ignore it.

## 3. Watch your first event

From **a different machine** (a different IP — otherwise you are just watching yourself):

```bash
ssh -o StrictHostKeyChecking=no root@<your-vps-public-ip>
```

**You will see a password prompt and, when you type, nothing will appear.** No asterisks, no
movement, nothing. That is not a hang — ssh never echoes passwords, on Windows or anywhere
else. Type it blind and press Enter.

One of two things then happens, and **both mean the honeypot is working**:

- a fake shell prompt appears (usually `<hostname>:~#`) — you are inside cowrie's sandbox
- `Permission denied, please try again.` — also cowrie; the password you typed simply is not
  in its fake user database

To make your first login *clean*, add the credentials you are going to type to the fake
database before you test. `etc/userdb.txt` takes `username:x:password` lines:

```bash
printf 'root:x:whateverpasswordyoutype\n' >> /root/cowrie/etc/userdb.txt
docker compose restart cowrie
```

Cowrie accepts those, and nothing else. It is a fake database for a fake server — the values
do not matter at all.

### "It hung" when it did not: the terminal lies, the log does not

On Windows, cowrie's fake shell frequently renders **nothing at all** — no prompt, and
sometimes no echo of what you type. Cowrie's PTY does not map cleanly onto Windows' console
(conpty), so the session is genuinely open and working while your terminal looks dead.

If you think it has hung, check the log before you believe it:

```bash
grep -E 'login.success|login.failed|session.connect' /root/cowrie/var/log/cowrie/cowrie.json | tail -5
```

`cowrie.login.success` means the honeypot caught a login, which is the entire point of the
exercise — even if your screen never drew a prompt. An `ssh -vvv` output that ends with
`Authenticated to <host> using "password"` followed by `shell request accepted` is a
**working honeypot**, not a broken one.

Workarounds, in order of effort:

- press Enter, then type `ls` — cowrie prints nothing until you give it input, so a silent
  screen is normal
- connect from a Linux or macOS host, or use PuTTY instead of the built-in Windows client.
  A real PTY behaves correctly where conpty may not.
- test from the sensor itself (`ssh -o StrictHostKeyChecking=no root@127.0.0.1`) to prove the
  honeypot side is fine

Note that your own SSH keys will **never** work here: cowrie rejects them by design and
offers password authentication only. If `-vvv` shows your keys being refused one by one and
then a password prompt, that is the honeypot behaving exactly as intended.

### If nothing responds at all, work out who answered you

An SSH banner does not tell you whether it came from cowrie or from a real sshd. Two checks,
in this order:

```bash
# 1. who actually owns port 22 on the host?
ss -tlnp | grep -E ':22 |:2222 '

# 2. did cowrie log the connection? this is the authoritative answer
tail -n 5 /root/cowrie/var/log/cowrie/cowrie.json
```

- log shows your connection (`cowrie.session.connect`, your IP) → you are talking to cowrie.
  If the session then stalls, it is a client-side or session issue, not the honeypot.
- log shows **nothing** while you know you connected → that prompt came from your **real
  sshd**, which means step 1 did not actually free port 22, or the `22:2222` mapping failed to
  bind because something else already held it.

Note that if your real sshd never left port 22, `docker compose up` will usually refuse to
start with `port is already allocated` — but "usually" is doing work in that sentence, so
check rather than assume.

Then on the sensor:

```bash
tail -f /root/cowrie/var/log/cowrie/cowrie.json | python3 -m json.tool --json-lines 2>/dev/null || \
  tail -f /root/cowrie/var/log/cowrie/cowrie.json
```

You should see `cowrie.session.connect`, then `cowrie.login.failed`, then — because cowrie's
`userdb.txt` accepts common passwords — `cowrie.login.success`, then `cowrie.command.input`.

**That is the whole product.** Everything after this is about surviving volume.

> Log files rotate at midnight (`cowrie.json.YYYY-MM-DD`). Anything you write to read these
> must handle both the live file and the rotated ones, or your dashboard empties itself every
> night at 00:00 — see `docs/08-log-window-rotation.md`.

## 4. Give it a dashboard

`aggregator/server.py` reads those logs and serves JSON. Per-node values are **environment
variables, not hardcoded constants** — that distinction cost a fleet outage once
(`docs/01-fleet-deployment.md`):

```bash
install -d /root/cowrie-dashboard
cp aggregator/server.py /root/cowrie-dashboard/
install -m 644 systemd/cowrie-dashboard.service /etc/systemd/system/
install -d /etc/systemd/system/cowrie-dashboard.service.d
cat >/etc/systemd/system/cowrie-dashboard.service.d/10-node.conf <<'EOF'
[Service]
Environment=DASH_PORT=8099
Environment=DASH_HOST=127.0.0.1
EOF
systemctl daemon-reload && systemctl enable --now cowrie-dashboard
curl -s localhost:8099/api/summary | head -c 300
```

**Never bind `0.0.0.0`.** Put it on loopback, or on a tailnet address if another host has to
reach it. A publicly reachable honeypot dashboard is both an information leak and an
invitation.

Verify the contract before trusting any graph: pass the aggregator URL to
`deploy/verify-dashboard-contract.py` (e.g. `python3 deploy/verify-dashboard-contract.py http://localhost:8099/api/node`
or via `BEEHIVE_API_URL`). It checks that every field the panels ask for actually exists,
which is faster than staring at empty panels and guessing.

## 5. Add more sensors

The interesting part, and the reason `docs/03-aggregator-fleet-node.md` exists. On each new
sensor: repeat 1–4, then add it to the fleet map the aggregator reads. Give every node a
distinct `DASH_PORT`/`DASH_HOST` drop-in and reach it over a private network, never the
public one.

Then `curl localhost:8099/api/node/<name>/summary` on the aggregator, and
`curl localhost:8099/api/node/all/summary` for the merged view. The `all` alias reports
counts that cannot be honestly summed as *floors*, not as facts — read that doc before you
trust a total.

If one node is in another datacenter and needs a public IPv4 that currently lives on a
sibling, `docs/02-moving-a-public-ip.md` is the whole recipe, including why the obvious VPN
approach silently fails.

## 6. Metrics, dashboards, alerts (optional)

*Note: If you just want a honeypot capturing logs on your VPS, you can stop at step 4! Step 6 is only needed if you want Grafana dashboards and Discord alerts.*

### 1. Host Metrics with Grafana Alloy
Grafana Alloy collects CPU, RAM, disk, and system stats and pushes them to Prometheus / Grafana Cloud.

1. In Grafana Cloud, open **Connections → Hosted Prometheus Metrics** and copy your **Remote Write Endpoint**, **Username / Instance ID**, and an **Access Token**.
2. Save your token to an `alloy-token` file (chmod 600) or specify its path via `ALLOY_TOKEN_FILE`:
   ```bash
   echo "glc_your_token_here" > deploy/alloy-token
   chmod 600 deploy/alloy-token
   ```
3. Run `deploy/90-deploy-alloy.sh` (or set `GRAFANA_USER_ID`, `GRAFANA_PUSH_URL`, and `NODES` to target your sensor(s)):
   ```bash
   GRAFANA_USER_ID=123456 \
   GRAFANA_PUSH_URL=https://prometheus-prod-.../api/prom/push \
   NODES="sensor1" \
   ALLOY_TOKEN_FILE=deploy/alloy-token \
   bash deploy/90-deploy-alloy.sh
   ```

### 2. Dashboards

Import the JSON files from `grafana/` into your Grafana instance:

- **`grafana/cowrie-dashboard.json`:** the honeypot itself — a `stat` row, hourly attack
  volume, a live event stream, the command/payload tables, and a **Geomap** of attacker
  locations.
- **`grafana/fleet-health.json`:** host vitals for every node, read from each node's `/proc`
  through the aggregator. No agent, no exporter — this is the "which box is about to fill its
  disk" view.

Both need the free **Grafana Infinity datasource**, and both address the API through a
dashboard **textbox variable called `api`**, so you set the base URL once instead of editing
every panel:

| where the API lives | set `api` to |
|---|---|
| same host as Grafana (self-hosted) | `http://127.0.0.1:8099` |
| another node, over the tailnet | `http://100.64.0.11:8099` (the aggregator) |
| Grafana Cloud, PDC agent on the node | `http://127.0.0.1:8099` + `--network host`, see below |

Both files ship with `"uid": "grafanacloud-infinity"` inside their panel datasource blocks.
That is a *UID*, not a name — if yours differs, it is the one thing the import dialog cannot
guess. Either create your datasource with that UID, or find-and-replace it after import.

**Infinity will happily double your URL.** If the datasource settings have a Base URL set
*and* the panel query carries a full `http://host:port/...`, you get
`http://127.0.0.1:8099http://127.0.0.1:8099/api/summary` and a parse error. Leave Base URL
**empty** and put the whole URL in the panel query, which is what these dashboards do.

### Reaching a loopback-bound API from Grafana Cloud

Step 4 tells you to bind the dashboard to `127.0.0.1`, and that is right — but Grafana Cloud
cannot route to your loopback, and neither can a Docker container in bridge mode. Grafana
Cloud's **Private Data Source Connect (PDC)** agent bridges the gap (Administration →
Connections → Private data source connect). Then:

```bash
docker run -d --restart unless-stopped --name pdc-agent --network host \
  grafana/pdc-agent:latest -token <your-pdc-token> \
  -cluster <your-cluster> -gcloud-hosted-grafana-id <your-id>
```

**`--network host` is the whole trick.** On Linux, a bridge-mode container's `127.0.0.1` is
the *container's* loopback, not the host's, and `host.docker.internal` is not populated by
default — so the agent reports `socks connect → 127.0.0.1:8099: unknown error host
unreachable` while the API is up and healthy. `--network host` puts the agent in the host's
network namespace and the connection just works. Do not solve this by binding the dashboard
to `0.0.0.0`.

### The Geomap panel

The map reads `GET {api}/api/flat/geo?limit=250`, which resolves city/country/lat/lon from the
**local mmdb** (`docs/04-geo-and-latency.md`) — no rate-limited public geo API, and no API key.
If you have not deployed the geo database yet the map will be empty rather than wrong.

What matters in the panel config:

- Infinity query: `Type: JSON`, `Source: URL`, `Format: Table`, **`Parser: Backend`**, and leave
  `Columns` **empty**. Empty means auto-detect, and `lat`/`lon` arrive from the API as real JSON
  numbers, so they type correctly. If you *do* add columns by hand you must set their types, and
  a `lat` typed as `String` plots nothing while reporting no error at all — an empty map,
  forever, with a green panel.
- Geomap layer: `Location: Coords`, `Latitude field: lat`, `Longitude field: lon`,
  `Size: hits`. Sizing markers by `hits` is what makes the map legible: one dot per IP is a
  cloud, one dot per IP sized by volume is a map.
- Basemap: leave it on **`default`**. It needs no API key, unlike most tile providers, and the
  ones that do need keys have a habit of starting to.

### 3. Alert Rules
Import or recreate the alert rules from `grafana/fleet-alert-rules.json`. Three are worth having immediately: disk > 85%, memory > 90%, and **node stopped reporting**.

### The two alerting mistakes everybody makes

- **`instant: true` is mandatory** on the query. Without it the rule reports health `ok`
  while actually erroring, and the alert simply never fires. Check
  `/api/prometheus/grafana/api/v1/rules` for `health: ok` on every rule, not just the state.
- **"node unreachable" must fire on absence.** `up == 0` never fires when the machine is
  gone, because there is no series to evaluate. Use no-data alerts, or you have built an
  alarm that is silent in exactly the case you care about.

## 7. Things that will bite you

Collected so you do not have to rediscover them:

- **`:22` before your admin port works.** The classic lockout. Verify in a second session.
- **`PermissionError: [Errno 13] ... 'var/lib/cowrie/state'` on first start.** You created the
  mount directories as root and the image runs as uid 999. `chown -R 999:999 /root/cowrie/var`
  and restart. This is the single most common first-run failure and it is fixed in step 2.
- **A dashboard bound to `0.0.0.0`.** Happened here twice.
- **Pushing a new copy of a service file to every node.** If it hardcodes a port or bind
  address that differs per node, you have just taken the fleet down. Environment variables
  and `NRestarts`, never a bare `is-active`. See `docs/01`.
- **Committing captured payloads or logs.** That is real malware in your git history, and it
  is a repository takedown. `.gitignore` them, and run `scripts/scrub-check.sh` before pushes.
- **Trusting a total.** Merged counts are hard; where a number cannot be unioned honestly,
  the aggregator reports a floor. Read the docs before quoting a figure.
- **Forgetting retention.** You are accumulating other people's IP addresses. Decide a
  retention period on purpose, and configure it.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · <a href="./README.md">docs index</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
