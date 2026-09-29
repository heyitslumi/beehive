# Quickstart: from a bare VPS to your first captured login

This walks one sensor, end to end, then shows how to add eight more and get them into
Grafana. It assumes Debian 12/13 and root, and it is written in the order that avoids
locking yourself out.

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
apt-get update && apt-get install -y docker.io docker-compose-v2 git
mkdir -p /root/cowrie/etc /root/cowrie/var/log/cowrie /root/cowrie/var/lib/cowrie
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
`etc/discord.py`, then start it:

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

Also create the bait files (see `cowrie/honeyfs/root/README.md`) — fake key material and a
fake wallet seed are exfiltrated within minutes and cost you nothing:

```bash
cat >/root/cowrie/honeyfs/root/wallet_backup.txt <<'EOF'
wallet seed phrase backup
abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about
EOF
```

## 3. Watch your first event

From **a different machine** (a different IP — otherwise you are just watching yourself):

```bash
ssh -o StrictHostKeyChecking=no whatever@<your-vps-public-ip> -p 22
# type any password twice; cowrie will accept it and give you a fake shell
```

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

Verify the contract before trusting any graph — `deploy/verify-dashboard-contract.py` checks
that every field the panels ask for actually exists, which is faster than staring at empty
panels and guessing.

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

## 6. Metrics, dashboards, alerts

1. Install Grafana Alloy on every sensor (`deploy/90-deploy-alloy.sh`) and remote-write host
   metrics to Grafana Cloud. Label each series with the node name.
2. Import `grafana/*.json` as dashboards. They use an Infinity datasource for the
   aggregator's JSON and a Prometheus datasource for host metrics.
3. Create the alert rules. Three are worth having immediately: disk > 85%, memory > 90%,
   and **node stopped reporting**.

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
