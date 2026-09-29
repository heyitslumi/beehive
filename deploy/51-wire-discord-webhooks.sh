#!/bin/bash
# Wire each node's cowrie Discord alerts to its own channel webhook, then restart the trap.
# Secrets live in per-node url files that are deleted at the end; only webhook IDs print.
set -uo pipefail
STAGE=/root/hp

declare -A URLS=(
  [sensor3]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor4]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor5]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor6]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor7]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor8]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
  [sensor9]="https://discord.com/api/webhooks/__WEBHOOK_ID__/__WEBHOOK_TOKEN__"
)

for n in "${!URLS[@]}"; do
  printf "%s" "${URLS[$n]}" > "$STAGE/discord-url.$n.txt"
  printf "  %-9s " "$n"
  scp -q -o BatchMode=yes -o ConnectTimeout=10 "$STAGE/50-set-discord-webhook.py" "root@$n:$STAGE/" 2>/dev/null
  scp -q -o BatchMode=yes "$STAGE/discord-url.$n.txt" "root@$n:$STAGE/discord-url.txt" 2>/dev/null
  ssh -o BatchMode=yes -o ConnectTimeout=15 "root@$n" \
    "python3 $STAGE/50-set-discord-webhook.py $STAGE/discord-url.txt 2>&1 | tail -1; docker restart cowrie >/dev/null 2>&1 && echo '  (cowrie restarted)'" 2>&1 | tail -2
  rm -f "$STAGE/discord-url.$n.txt"
done

echo
echo "=== sensor1 + sensor2 keep their existing webhooks; confirming both still point somewhere valid ==="
for t in "sensor1:local" "sensor2:incus"; do
  n="${t%%:*}"; how="${t##*:}"
  printf "  %-9s " "$n"
  if [ "$how" = local ]; then
    grep -A6 '^\[output_discord\]' /root/cowrie/etc/cowrie.cfg | grep -oE 'webhooks/[0-9]+' | head -1
  else
    incus exec prod-uk:sensor2 --project lumi -- bash -c "grep -A6 '^\[output_discord\]' /root/cowrie/etc/cowrie.cfg | grep -oE 'webhooks/[0-9]+' | head -1" 2>&1 | tail -1
  fi
done
