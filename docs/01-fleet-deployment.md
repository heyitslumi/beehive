# Deploying cowrie to additional nodes (fleet recipe)

Order of work per node, and the traps that cost real time. Two phases, each idempotent:
`10-ssh-port.sh` (free port 22) then `20-deploy.sh` (cowrie + dashboard).

## Phase 1 — free port 22 for the trap

1. Move the real sshd to a custom port (__ADMIN_SSH_PORT__) via a drop-in, restart, then **verify with a
   fresh connection before continuing**.
2. `tailscale ssh` is served by tailscaled, not sshd, so it stays up through the move — it is
   the safety net that makes this safe to do remotely.
3. Remove/comment any `Port` line in the *main* `sshd_config`: sshd listens on **every** Port it
   finds, so a leftover `Port 22` in the drop-in's parent file keeps 22 occupied and cowrie
   cannot bind.

### Pitfalls that bit

- `ssh host bash -s -- "$A" "$KEY"` **mangles argv**: ssh concatenates argv into one remote
  command string, so the key arrives split across `$2`/`$3`. A pubkey shipped that way becomes a
  one-word `KBLOB`, the `grep -qF ""` guard matches everything, and the append silently no-ops.
  Ship data as a **file** (scp) and read it remotely; only pass simple scalars as argv.
- An `authorized_keys` whose last line lacks a trailing newline **glues** the appended key onto
  that line, producing one unusable line. `printf '\n' >> file` first, then append.
- Do not trust `nc -z` / `/dev/tcp` reads for "is it serving": a successful TCP connect with no
  payload looks identical to a dead backend. Verify with a full banner read **and** from an
  independent network path (a second host), not just the deploying host.
- A provider forwarding only 22 is a *guess* until something listens elsewhere: empty ports read
  as "blocked". Check with a real listener before designing around assumed restrictions.

## Phase 2 — cowrie + dashboard

- `var/log/cowrie` and `var/lib/cowrie` must be owned by **999:999** (the image runs as uid 999).
  Root-owned dirs make the SSH factory die with PermissionError: the port *accepts* connections
  and then never answers, which looks exactly like a firewall problem.
- Publish with the node's own interface IP (`"<ip>:22:2222"`), never `0.0.0.0`.
- Bind the **dashboard to the node's tailnet IP**, not 0.0.0.0: a published 0.0.0.0 dashboard on
  port 8099 was reachable from the public internet even though 80/443 were not in use.
  Bind failure at boot races tailscaled — order the unit `After=/Wants=tailscaled.service`.
- `systemctl restart` returning success is not proof the new binary is live: check
  `systemctl show -p MainPID -p NRestarts` and the actual bind address (`ss -tlnp`). A stale
  long-running process can keep serving the old config while restarts sit at 0.
- **Duplicate key in cowrie.cfg** (e.g. two `enabled` lines in `[telnet]`) makes configparser
  raise DuplicateOptionError, and twistd reports it as `twistd -n: Unknown command: cowrie` —
  which looks nothing like a config error.

## Pushing a code change to a fleet that has per-node constants

A node's copy of a service file can legitimately differ from the one you develop on -- sensor2's
dashboard binds **8098** because 8099 there belongs to an unrelated docker container, every other
node uses 8099. Overwriting a node's copy with the dev copy silently changes that constant and puts
the service in a **crash loop**: `systemctl is-active` still answers `active` (each restart briefly
succeeds), while the port the aggregator expects answers nothing.

**The port is not the only per-node constant -- the bind address is one too.** A node the aggregator
reaches over the tailnet must listen on its **tailnet IP**; a node with local nginx wants
`127.0.0.1`. Overwrite either and the node looks alive locally while being unreachable from the
aggregator.

- Make node-specific values **environment variables with sane defaults** (`DASH_PORT`, `DASH_HOST`) and
  set the per-node values in a systemd drop-in (`/etc/systemd/system/<unit>.service.d/*.conf`). Then
  one file is deployable everywhere and the constants stop being invisible state.
- Let the bind address be a **comma-separated list** (`DASH_HOST=127.0.0.1,100.64.0.14`) so a node can
  serve loopback *and* its tailnet IP, and start one listener per address. Never `0.0.0.0` as the easy
  way out: that is what exposes a honeypot dashboard to the internet. One unusable address (no tailnet
  yet at boot) must not kill the others -- catch per-address and keep serving.
- Before believing a push landed: `systemctl show -p NRestarts` (`NRestarts=47` plus `active` is a
  crash loop, not a deployment), the real bind (`ss -tlnp | grep <port>`), and one live API request.
- `Address already in use` in the journal means something else owns that port -- identify the owner
  (`ss -tulpn`) before touching the service; the loop may be self-perpetuating after one first
  failure rather than a port actually being held.
- **Read the failure shape before guessing which constant broke.** *Connection refused, fast, from
  every remote node at once* is a bind address (nothing is listening where the caller looks). *Slow
  timeouts* are a hang or a saturated box. *`active` with a rising `NRestarts`* is a crash loop. The
  port being wrong shows up as refused on one node, not all of them.

### Reaching sensor2 specifically

It lives on prod-uk, so the *local* incus CLI finds no daemon. Use the remote with an
remote-qualified instance:

```
incus --project lumi exec prod-uk:sensor2 -- <cmd>
incus --project lumi file push <local> prod-uk:sensor2/root/path
```

The `--remote` flag does not exist on this client build (6.0.0); qualify the instance instead. Do
not plan on ssh: port 22 there is the **cowrie trap** and sshd is on __ADMIN_SSH_PORT__ behind host-key trust.
In a fleet loop that includes the local node, `scp: Connection closed` from scp-to-itself is
harmless noise, not a failure.

## Aggregator pattern (nodes with no inbound HTTPS)

Most cheap VPS providers route their public IPs such that an inbound HTTPS/certbot setup is
fragile or impossible, and each extra public hostname is another exposed surface. Instead serve
the JSON APIs over the tailnet and fan them out from **one** public host:

- Add a fleet map `NODES = {node: "host:port"}` to the aggregator's `server.py` and a route
  `/api/node/<node>/<endpoint>` that relays `http://<target>/api/<endpoint>`.
- Grafana then points every panel at `https://<aggregator>/api/node/${node}/...`, so the node
  dropdown works with **zero per-node DNS, certificates or web servers**.
- Use `ThreadingHTTPServer`, not `HTTPServer`: the aggregator is asked for its *own* node too, and
  a single-threaded server deadlocks against itself.
- A node that also serves nginx locally needs **both** listeners: `127.0.0.1:<port>` for nginx and
  its tailnet IP for the aggregator. Binding 0.0.0.0 leaks it publicly; binding only localhost
  hides it from the aggregator.

## Per-node quirks seen in the wild

- Nodes whose public IPs are routed over WireGuard (a panel/hosting node) **break docker port
  publishing**: the reply is SNAT-ed to a private address, so the client sees the SYN but never a
  SYN/ACK. Fix: `network_mode: "host"` and have cowrie listen on `tcp:22` directly.
- Binding a privileged port as uid 999 in host mode then fails with `Permission denied` even with
  `cap_add: NET_BIND_SERVICE`. `sysctl net.ipv4.ip_unprivileged_port_start=0` (persist in
  `/etc/sysctl.d/`) fixes it.
- Boxes behind provider NAT (e.g. Oracle) only forward the ports their console forwards — moving
  sshd to __ADMIN_SSH_PORT__ can make real SSH reachable *only* over the tailnet. Confirm before assuming.
- A node at 100% disk cannot even pull the cowrie image: check `df -h /` before planning a deploy.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
