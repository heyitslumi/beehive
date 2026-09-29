#!/usr/bin/env python3
"""Turn on the Discord event filter and give this node a real sensor name.

- [output_discord] events = <allowlist>   (empty/missing = post everything)
- [honeypot] sensor_name = <node>         (without it cowrie reports the container hostname)
- docker-compose: bind-mount the patched output module over the one in the image, so the
  filter survives container recreation instead of living inside the image.

Validates with configparser before writing (duplicate keys are a hard cowrie error) and
backs the config up. Idempotent.

Usage: 70-cfg-events.py <node-name> [config] [compose]
"""
import configparser
import datetime
import os
import re
import shutil
import sys

NODE = sys.argv[1] if len(sys.argv) > 1 else "unknown"
CFG = sys.argv[2] if len(sys.argv) > 2 else "/root/cowrie/etc/cowrie.cfg"
COMPOSE = sys.argv[3] if len(sys.argv) > 3 else "/root/cowrie/docker-compose.yml"
EVENTS = ("cowrie.session.connect,cowrie.login.failed,cowrie.login.success,"
          "cowrie.session.file_download")
MOUNT = "      - ./etc/discord.py:/cowrie/cowrie-git/src/cowrie/output/discord.py:ro\n"

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
os.makedirs("/root/.hp-backups", exist_ok=True)

# --- config ---------------------------------------------------------------
text = open(CFG, encoding="utf-8").read()

m = re.search(r"(?ms)^\[output_discord\]\n(.*?)(?=^\[|\Z)", text)
if not m:
    print("FAIL: no [output_discord] section")
    sys.exit(2)
body = m.group(1)
if re.search(r"(?m)^\s*events\s*=", body):
    new_body = re.sub(r"(?m)^(\s*events\s*=\s*).*$", r"\g<1>" + EVENTS, body)
    ev_action = "updated"
else:
    new_body = body.rstrip("\n") + "\nevents = " + EVENTS + "\n"
    ev_action = "added"
text = text[:m.start(1)] + new_body + text[m.end(1):]

m = re.search(r"(?ms)^\[honeypot\]\n(.*?)(?=^\[|\Z)", text)
if not m:
    print("FAIL: no [honeypot] section")
    sys.exit(2)
body = m.group(1)
if re.search(r"(?m)^\s*sensor_name\s*=", body):
    new_body = re.sub(r"(?m)^(\s*sensor_name\s*=\s*).*$", r"\g<1>" + NODE, body)
    sn_action = "updated"
else:
    new_body = body.rstrip("\n") + "\nsensor_name = " + NODE + "\n"
    sn_action = "added"
text = text[:m.start(1)] + new_body + text[m.end(1):]

cp = configparser.ConfigParser(strict=True)
try:
    cp.read_string(text)
except configparser.Error as exc:
    print("FAIL: config would not parse (%s) - not writing" % exc)
    sys.exit(3)

shutil.copy2(CFG, "/root/.hp-backups/cowrie.cfg.events.%s" % stamp)
open(CFG, "w", encoding="utf-8").write(text)

# --- compose mount --------------------------------------------------------
comp = open(COMPOSE, encoding="utf-8").read()
mount_action = "already present"
if "output/discord.py" not in comp:
    anchor = "      - ./etc/userdb.txt:/cowrie/cowrie-git/etc/userdb.txt:ro\n"
    if anchor not in comp:
        print("FAIL: compose volume anchor missing")
        sys.exit(4)
    shutil.copy2(COMPOSE, "/root/.hp-backups/docker-compose.yml.%s" % stamp)
    comp = comp.replace(anchor, anchor + MOUNT, 1)
    open(COMPOSE, "w", encoding="utf-8").write(comp)
    mount_action = "added"

after = configparser.ConfigParser(strict=True)
after.read(CFG)
print("events %s | sensor_name %s (%s) | module mount %s" % (
    ev_action, sn_action, after.get("honeypot", "sensor_name", fallback="?"), mount_action))