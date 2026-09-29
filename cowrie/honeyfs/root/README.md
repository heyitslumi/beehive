# Bait files

These live at `/root/cowrie/honeyfs/root/` on each sensor. Cowrie serves them to anyone
who gets a shell, and bots exfiltrate them within minutes.

**Use fake values only, never real ones.** Everything here must be worthless: no real
keys, no working credentials, no actual wallet phrases. Assume any file in this directory
is public the moment it is deployed.
