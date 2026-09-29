#!/usr/bin/env python3
"""Normalise the honeypot output-module blocks so every node actually reports.

Two failure modes seen in the fleet:

  1. A section is configured but has no `enabled = true` line, so the module is
     never switched on (startup log shows only "Loaded output engine: jsonlog").
  2. The block predates the plugin's current keys and omits ones it requires
     without a fallback (e.g. abuseipdb's `dump_path`), so the module raises while
     loading and silently disappears from the engine list.

Both look identical from the outside: config looks right, nothing reports.

This rewrites only [output_abuseipdb], [output_virustotal] and [output_discord] to the
canonical shape, carrying each node's OWN secret values across untouched. Secrets are
never printed. Idempotent; backs the config up before writing; refuses to save a file
that configparser cannot read (duplicate keys are a hard error for cowrie).

Usage: 40-outputs-normalize.py [path]
"""
import configparser
import datetime
import os
import re
import shutil
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/cowrie/etc/cowrie.cfg"

# section -> (canonical body lines, except the secret key which is preserved)
CANON = {
    "abuseipdb": ("api_key", [
        "enabled = true",
        "rereport_after = 24",
        "tolerance_window = 120",
        "tolerance_attempts = 1",
        "dump_path = ${honeypot:state_path}/abuseipdb",
    ]),
    "virustotal": ("api_key", [
        "enabled = true",
    ]),
    "discord": ("url", [
        "enabled = true",
        "default_delay = 0.3",
        "retry_delay = 2",
        "max_retries = 5",
    ]),
}

# the secret key can appear under an older name; accept either when preserving
ALIASES = {"discord": ["url", "webhook_url"]}


def split_sections(text):
    """[(header_or_None, body)] with headers kept verbatim."""
    parts = re.split(r"(?m)^(\[[^\]]+\]\n)", text)
    out = []
    if parts[0]:
        out.append((None, parts[0]))
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1]))
    return out


def main():
    with open(PATH, encoding="utf-8") as f:
        text = f.read()

    sections = split_sections(text)
    by_name = {h.strip().strip("[]"): i for i, (h, _) in enumerate(sections) if h}
    changes = []

    for name, (secret_key, body_keys) in CANON.items():
        sec = "output_" + name
        idx = by_name.get(sec)
        if idx is None:
            changes.append("%s: section missing, skipped" % sec)
            continue
        header, body = sections[idx]

        # carry the node's own secret over (never printed)
        secret = None
        for alias in ALIASES.get(name, [secret_key]):
            m = re.search(r"(?m)^%s\s*=\s*(.+)$" % re.escape(alias), body)
            if m and m.group(1).strip():
                secret = m.group(1).strip()
                break
        if not secret:
            changes.append("%s: NO SECRET FOUND, left alone" % sec)
            continue

        was_enabled = bool(re.search(r"(?m)^enabled\s*=\s*true\s*$", body))
        new_body = "\n%s = %s\n" % (secret_key, secret) + "".join(k + "\n" for k in body_keys)
        if new_body != body.lstrip("\n") and new_body != body:
            sections[idx] = (header, new_body)
            changes.append("%s: rewritten (was enabled=%s)" % (sec, "true" if was_enabled else "MISSING"))

    if not changes:
        print("nothing to do")
        return 0
    if all("skipped" in c or "NO SECRET" in c for c in changes) and not any("rewritten" in c for c in changes):
        print("; ".join(changes))
        return 0

    new_text = "".join((h or "") + b for h, b in sections)

    # cowrie dies on duplicate option names, so prove the file parses first
    cp = configparser.ConfigParser(strict=True)
    try:
        cp.read_string(new_text)
    except configparser.Error as exc:
        print("FAIL: config would not parse (%s) - not writing" % exc)
        return 2

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs("/root/.hp-backups", exist_ok=True)
    shutil.copy2(PATH, "/root/.hp-backups/cowrie.cfg.%s" % stamp)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new_text)

    for c in changes:
        print("  " + c)
    print("config written (backup: cowrie.cfg.%s)" % stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
