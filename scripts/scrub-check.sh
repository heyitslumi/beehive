#!/bin/bash
# Re-run before every push: fails if anything credential-shaped is in the tree.
# cowrie/honeyfs is bait by design -- it is *meant* to look like key material, so the
# private-key check is explicitly allowed there and nowhere else.
set -u
cd "$(dirname "$0")/.." || exit 1
HITS=$(grep -rInE 'discord(app)?\.com/api/webhooks/[0-9]+|glc_[A-Za-z0-9]{20,}|api_?key[" ]*[:=][" ]*[A-Za-z0-9_.-]{16,}' \
  --exclude-dir=.git . 2>/dev/null)
KEYS=$(grep -rIlnE 'BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' --exclude-dir=.git . 2>/dev/null \
  | grep -v '^./cowrie/honeyfs/' || true)
[ -n "$KEYS" ] && HITS="$HITS
$KEYS (key material outside the bait directory)"
if [ -n "$HITS" ]; then echo "FAIL -- credential-shaped strings found:"; echo "$HITS"; exit 1; fi
PUB=$(grep -rInE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' --exclude-dir=.git . 2>/dev/null \
  | grep -vE '127\.|10\.|192\.168\.|100\.64\.|198\.51\.100\.|203\.0\.113\.|0\.0\.0\.0|255\.' || true)
if [ -n "$PUB" ]; then echo "WARN -- non-placeholder IPs:"; echo "$PUB"; fi
echo "OK -- no credentials found"
