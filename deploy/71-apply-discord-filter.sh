#!/bin/bash
# Install the event-filtered Discord module + sensor names on every node, then prove the
# patch is live inside each container.
set -uo pipefail
S=/root/hp
NODES="sensor1 sensor2 sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9"

for n in $NODES; do
  printf "=== %s ===\n" "$n"
  case "$n" in
    sensor1)
      cp "$S/discord-filtered.py" /root/cowrie/etc/discord.py
      python3 "$S/70-cfg-events.py" sensor1
      (cd /root/cowrie && docker compose up -d 2>&1 | tail -2)
      ;;
    sensor2)
      incus file push --project lumi "$S/discord-filtered.py" prod-uk:sensor2/root/cowrie/etc/discord.py >/dev/null 2>&1
      incus file push --project lumi "$S/70-cfg-events.py" prod-uk:sensor2/root/hp/70-cfg-events.py >/dev/null 2>&1
      incus exec --project lumi prod-uk:sensor2 -- bash -c \
        "python3 /root/hp/70-cfg-events.py sensor2 && cd /root/cowrie && docker compose up -d 2>&1 | tail -2" 2>&1 | tail -3
      ;;
    *)
      scp -q -o BatchMode=yes "$S/discord-filtered.py" "root@$n:$S/discord.py" 2>/dev/null
      scp -q -o BatchMode=yes "$S/discord-filtered.py" "root@$n:/root/cowrie/etc/discord.py" 2>/dev/null
      scp -q -o BatchMode=yes "$S/70-cfg-events.py" "root@$n:$S/70-cfg-events.py" 2>/dev/null
      ssh -o BatchMode=yes "root@$n" \
        "python3 $S/70-cfg-events.py $n && cd /root/cowrie && docker compose up -d 2>&1 | tail -2" 2>&1 | tail -3
      ;;
  esac
  sleep 6
  # prove the container is importing the patched file, not the image's original
  case "$n" in
    sensor2) PROBE="incus exec --project lumi prod-uk:sensor2 -- bash -c" ;;
    sensor1) PROBE="" ;;
    *) PROBE="ssh -o BatchMode=yes root@$n" ;;
  esac
  CMD='docker exec cowrie python3 -c "import inspect,cowrie.output.discord as m;print(\"filter_live=\",\"_events_allow\" in inspect.getsource(m.Output))" 2>&1 | tail -1'
  if [ "$n" = sensor1 ]; then eval "$CMD"; else $PROBE "$CMD" 2>&1 | tail -1; fi
  if [ "$n" = sensor1 ]; then docker logs --tail 40 cowrie 2>&1 | grep -oE 'Loaded output engine.*' | tail -1; else $PROBE 'docker logs --tail 40 cowrie 2>&1 | grep -oE "Loaded output engine.*" | tail -1' 2>&1 | tail -1; fi
done
echo
echo "=== sensor names now set (should be the node name, not a container hash) ==="
printf "  %-9s %s\n" sensor1 "$(sed -n '/^\[honeypot\]/,/^\[/p' /root/cowrie/etc/cowrie.cfg | grep -oE 'sensor_name.*')"
for n in sensor3 sensor4 sensor5 sensor6 sensor7 sensor8 sensor9; do printf "  %-9s %s\n" $n "$(timeout 40 ssh -o BatchMode=yes root@$n 'sed -n "/^\[honeypot\]/,/^\[/p" /root/cowrie/etc/cowrie.cfg | grep -oE "sensor_name.*"' 2>&1 | tail -1)"; done
printf "  %-9s %s\n" sensor2 "$(timeout 60 incus exec --project lumi prod-uk:sensor2 -- bash -c 'sed -n "/^\[honeypot\]/,/^\[/p" /root/cowrie/etc/cowrie.cfg | grep -oE "sensor_name.*"' 2>&1 | tail -1)"