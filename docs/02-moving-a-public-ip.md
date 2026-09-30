# Moving a public IPv4 to another host with source IPs preserved

When a host must gain a public IPv4 that currently lives on a sibling node, the naive
"just route it over the VPN" plan fails for reasons that are non-obvious. What actually works.

## Why VPNs (tailscale) cannot carry it

- Tailscale **validates the source** of packets entering its tunnel. An arbitrary internet
  client's IP is not a legitimate tailnet address, so the packet is **silently dropped** at the
  destination (counters stay at 0 — it looks exactly like a firewall, but no rule is matching).
  This is why subnet routers preserve source IPs only for their *own* LAN ranges.
- A one-way tailnet fault made it worse: the source node could not initiate **any** new flow to
  the destination (ICMP/TCP/UDP all failed, all 8 peers), while the destination could reach it.
  Remember: **replies inside an established flow still work**, which is the escape hatch below.

## The working design: TUN carried inside an SSH session the far side dials

1. The far host (which *can* dial the near host) runs an SSH session that opens a TUN pair:
   `ssh -w 0:0 -p <sshd-port> -i <key> root@<near> "<configure near end>; exec sleep infinity"`
   - `sleep infinity` keeps the session (and the tun) alive; the command configures the far end.
   - Use the host's **real sshd**, not tailscale's SSH: tailscale SSH is a restricted
     implementation that cannot do `-w` (TUN forwarding).
   - Requires `PermitTunnel point-to-point` in sshd_config on the near side (default is no).
2. Address both ends (e.g. /30) and set `mtu 1400` — SSH-over-TCP carrier needs headroom, plus
   `TCPMSS --clamp-mss-to-pmtu` on the forwarding host.
3. Near side (holds the IP's network segment):
   - stop the address being *local* — remove it from the NIC, or the kernel delivers locally and
     never forwards;
   - keep the upstream router finding it: `ip neigh replace proxy <ip> dev <nic>` +
     `net.ipv4.conf.<nic>.proxy_arp=1`;
   - `ip route replace <ip>/32 dev tun0` and a high-priority rule so this beats the VPN's own
     route table: `ip rule add priority 5200 to <ip>/32 lookup main`;
   - unconditional forwarding past a DROP policy + `rp_filter` relaxed on the tunnel.
4. Far side: hold the IP (`ip addr replace <ip>/32 dev lo`) and send its replies back over the
   tunnel: `ip rule add priority 5202 from <ip> lookup 77` +
   `ip route replace default dev tun0 table 77`.
5. Verify with `sshd`'s log on the far host: it must show the **client's** real IP. Threshold to
   watch (no SNAT):
   `journalctl -u ssh | grep "Connection closed by"`

## Traps that cost time

- **Do not leave the VPN route advertised** for the moved IP. Nodes that accept routes will divert
  the traffic down the VPN — where it dies — while nodes that ignore routes work. Symptom: the IP
  works from some hosts and silently times out from others (one host out of four failed). Withdraw
  the advertisement once the tunnel is the real path.
- `pkill -f "ssh -N -w ..."` **kills your own remote shell** (its command line contains the
  pattern). Find the PID via `ps` and kill by PID.
- A leftover manual carrier session holds the tun device, so the systemd unit flaps forever
  (`activating`, tun never appears). Kill the stale session before testing the unit.
- `sysctl -w net.ipv4.conf.<nic>.proxy_arp=1` alone is not enough — the kernel only answers ARP
  for addresses it has a route for; the explicit `ip neigh replace proxy` entry does not depend on
  that and is the reliable form.
- Routing a tunnel's replies via the far node's default VPN table sends them out the wrong link
  (out the VPN hub toward a CGNAT destination). Point `.ip rule from <ip>` at the tunnel table.

## Persistence shape

- Near side: oneshot unit (proxy ARP, FORWARD accepts, sysctls) — does not own the tun.
- Far side: the service runs the SSH carrier itself (`Restart=always`), with an ExecStartPost that
  addresses its own end, and the remote command that addresses the near end. One unit builds the
  whole link.
- Roll back by disabling the units and putting the address back on the near NIC.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · <a href="./README.md">docs index</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
