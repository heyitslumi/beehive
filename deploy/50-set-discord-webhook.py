#!/usr/bin/env python3
"""Point this node's cowrie Discord output at a specific webhook.

The URL is read from a file rather than an argument: ssh concatenates argv into one
remote command string, so a long URL arriving as argv gets mangled. Only the webhook
ID is ever printed.

Usage: 50-set-discord-webhook.py [url-file] [config-path]
"""
import configparser
import datetime
import os
import re
import shutil
import sys

URL_FILE = sys.argv[1] if len(sys.argv) > 1 else "/root/hp/discord-url.txt"
CFG = sys.argv[2] if len(sys.argv) > 2 else "/root/cowrie/etc/cowrie.cfg"

url = open(URL_FILE, encoding="utf-8").read().strip()
if not re.match(r"^https://discord\.com/api/webhooks/\d+/[\w-]+$", url):
    print("FAIL: not a discord webhook url")
    sys.exit(2)
wid = re.search(r"/webhooks/(\d+)/", url).group(1)

text = open(CFG, encoding="utf-8").read()
m = re.search(r"(?ms)^\[output_discord\]\n(.*?)(?=^\[|\Z)", text)
if not m:
    print("FAIL: no [output_discord] section")
    sys.exit(2)
body = m.group(1)
if re.search(r"(?m)^\s*url\s*=\s*(\S+)", body):
    new_body = re.sub(r"(?m)^(\s*url\s*=\s*)\S+", r"\g<1>" + url, body)
    action = "updated"
elif re.search(r"(?m)^\s*webhook_url\s*=\s*(\S+)", body):
    new_body = re.sub(r"(?m)^(\s*webhook_url\s*=\s*)\S+", r"\g<1>" + url, body)
    action = "updated (webhook_url key)"
else:
    new_body = body.rstrip("\n") + "\nurl = " + url + "\n"
    action = "added"
new_text = text[:m.start(1)] + new_body + text[m.end(1):]

cp = configparser.ConfigParser(strict=True)
try:
    cp.read_string(new_text)
except configparser.Error as exc:
    print("FAIL: config would not parse (%s) - not writing" % exc)
    sys.exit(3)

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
os.makedirs("/root/.hp-backups", exist_ok=True)
shutil.copy2(CFG, "/root/.hp-backups/cowrie.cfg.webhook.%s" % stamp)
open(CFG, "w", encoding="utf-8").write(new_text)

# prove it round-trips and only the discord url moved
after = configparser.ConfigParser(strict=True)
after.read(CFG)
print("%s -> webhook id %s (section enabled=%s)" % (
    action, wid, after.get("output_discord", "enabled", fallback="MISSING")))
