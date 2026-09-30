# Grafana Alloy: host metrics into Grafana Cloud, fleet-wide

Goal: real time series (history, trends, alert rules) for a mixed fleet, with no separate
node_exporter per box. One Alloy per node, `prometheus.exporter.unix` built in.

## What you need first

A Grafana Cloud **access-policy token with the `metrics:write` scope** (stack-scoped:
Grafana → Administration → Cloud access policies → create policy → Add token). Pair it with:

- **push URL** = the stack's prometheus datasource URL + `/push` — read it from the datasource
  (`get_datasource` on `grafanacloud-prom`): `https://prometheus-prod-<n>-prod-<region>.grafana.net/api/prom` → `/api/prom/push`
- **username** = the datasource's `basicAuthUser` — the numeric **stack/instance id**, NOT the
  org id inside the token. These differ and the wrong one gives 401s.

Store the token in a file (`chmod 600`) and read it with `tr -d '\n\r '` — never as a command
argument. Tell the user to paste it with `read -rs` so it skips shell history.

## Config (river)

```river
prometheus.exporter.unix "host" { }

discovery.relabel "host" {
  targets = prometheus.exporter.unix.host.targets
  rule {
    source_labels = ["__address__"]
    target_label  = "node"
    replacement   = "<node-name>"
  }
}

prometheus.scrape "host" {
  targets         = discovery.relabel.host.output
  forward_to      = [prometheus.remote_write.cloud.receiver]
  scrape_interval = "30s"
  job_name        = "node"
}

prometheus.remote_write "cloud" {
  endpoint {
    url = "<push url>"
    basic_auth { username = "<stack id>"  password = "<token>" }
  }
}
```

Install from Grafana's apt repo (keyring + `deb [signed-by=...] https://apt.grafana.com stable main`
→ `apt-get install -y alloy`), config at `/etc/alloy/config.alloy` owned `root:alloy` mode 640,
`systemctl enable --now alloy`.

## Deployment gotchas

- **Generate each config locally and scp it (or stage locally for single-host).** Shipping a heredoc over ssh (e.g. via
  `declare -f` + indent) breaks the `EOF` terminator once indented, and a malformed alloy
  config fails *silently*. Per-node staging file → `install -o root -g alloy -m 640` → delete
  the staged copy afterwards so the token is not left lying around. For standalone / single-box
  deployments, support `NODES="local"` directly to avoid self-SSH name resolution and host key failures.
- **Avoid hardcoded repository or host staging directories.** Stage generated configs in portable
  locations like `/tmp/.alloy-<node>.alloy` rather than hardcoded scratch paths that may not exist on a fresh server.
- **`job_name` does not win over a target's own labels:** the unix exporter ships
  `job="integrations/unix"`, so query by the `node` label you added, not by job.
- **Prove arrival from the backend, not from the service status.** `systemctl is-active alloy`
  and the local UI mean nothing. Query prometheus for `count(up) by (node)` and expect one
  series per node. Cross-check Alloy's own counters:
  `curl -s localhost:12345/metrics | grep -E 'prometheus_remote_storage_(samples|samples_failed)'`
  — samples_total with samples_failed 0 means the writes are landing.
- **A first query right after install can legitimately be empty** (backend indexing lags a
  minute or two). Re-query before hunting a fault, but do not stop at "probably propagation".
- `POST` to the push URL with no auth returning **401** is the expected probe result: it means
  the endpoint resolves and wants credentials.
- Instance labels often come out as `hostname.example.com` (whatever the hostname is set
  to) — the added `node` label is the canonical identifier; keep both.

## Known divergence to expect

A `/proc`-scraping JSON endpoint and `node_exporter` can disagree on free space: sampling
minutes apart while something is actively writing (logs, a package install) shows up as a
several-point difference on a small disk. The time series is the truth; a single JSON snapshot
is a moment.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
