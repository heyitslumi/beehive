# Security policy

This is a defensive tool that deliberately attracts hostile traffic. Two things follow
from that, and they are the only interesting parts of this document.

## Reporting a vulnerability in this code

Open a private security advisory on this repository (Security → Report a vulnerability), or
email hi@example.com. Please do not open a public issue for anything exploitable.

The realistic risks here are:

- **escape from the sandbox.** Cowrie's fake shell is not a jail — it runs in the cowrie
  container as a normal user. If you find a way to make it execute real commands on the host,
  that is the bug worth reporting.
- **the dashboard leaking data.** The aggregator serves attacker IP addresses and command
  history. If a configuration lets it be reached publicly, that is a vulnerability in how the
  project documents itself, and worth reporting.
- **credential exposure in this repository.** See below; this one is different.

## If you find a credential in a public fork of this repository

Report it the same way, and assume the operator's credentials are already compromised. The
upstream repository is scrubbed mechanically before every push (`scripts/scrub-check.sh`),
including a check that no non-placeholder IP address survives. If something got through, that
is a bug in the scrubber and I want to know.

## For people running this

- **Do not commit what your honeypot captures.** Payloads, logs and databases contain real
  malware and real people's addresses. Keep them out of git; `.gitignore` already does.
- **Do not bind the dashboard publicly.** Loopback or a private network only.
- **Clear it with your hosting provider first.** Abuse complaints are normal; suspensions
  are avoidable annoyance.
- **Decide retention on purpose.** You are processing personal data by design. Pick a window,
  document it, and enforce it.
