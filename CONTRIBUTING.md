# Contributing

Issues and pull requests are welcome. This is a small, opinionated repository, so a little
context up front saves everyone a round trip.

## How this repository is maintained

Worth knowing before your first edit, because it is not the usual flow: **parts of this tree
are generated, and editing them directly does not stick.**

`docs/01`–`docs/09` are published copies of a private operational skill's reference notes.
They are rebuilt from those sources, so an edit made directly to a file under `docs/` (on
GitHub, or on disk) is reverted the next time the tree is regenerated. The same applies to
the footer every doc page carries — that is applied by the build, not typed into the files.

In practice:

- **Prose and structure** (`README.md`, `docs/00-quickstart.md`, `docs/README.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, the dashboards, `cowrie/cowrie.cfg.example`) are authored
  files and are safe to edit in place.
- **`docs/01`–`docs/09`** should be changed by opening a PR against the generated file *and
  saying so* — the maintainer will apply it to the source and regenerate. A PR that only
  edits the generated copy cannot be merged as-is.
- **Addresses and credentials are placeholders on purpose.** The build rewrites real host
  names, public IPs and secrets into documentation ranges and tokens. Do not "fix" them back.

## Before you open a PR

**Read the doc for the area you are touching.** Nearly every design decision here is the
result of something that broke in production, and the reasoning is usually in `docs/` — a
"simplification" that removes it tends to reintroduce the outage. If you think a rule is
wrong, say which incident it was for and why it no longer applies.

**Run the scrub check.** This repository is committed *scrubbed on purpose*, and that is not
cosmetic:

```bash
bash scripts/scrub-check.sh
```

It must print `OK -- no credentials found`. It fails on webhook URLs, `glc_`/`glsa_` tokens,
API keys and private key material — and the key-material check deliberately exempts
`cowrie/honeyfs/`, which is bait and is *meant* to look like key material.

## Never commit

- **credentials** of any kind — tokens, webhook URLs, API keys, passwords
- **captured payloads, logs, or the sqlite database** — that is real malware and real people's
  IP addresses. It does not belong in git, and committing it is a repository takedown.
- **real hostnames or IP addresses.** Use the placeholders: `sensor1..sensor9`, documentation
  ranges (`198.51.100.0/24`, `203.0.113.0/24`) and CGNAT space (`100.64.0.11/10`).
- **the geo database** — 127MB, third-party, with its own attribution terms. The installer
  downloads it.

If you are adding a deploy script, take secrets from a file path given by an environment
variable with a documented default, and stage generated configs in `$TMPDIR` rather than a
hardcoded scratch directory. `deploy/90-deploy-alloy.sh` is the reference implementation.

## What makes a change mergeable

- **It works on a fresh single host.** The quickstart is the front door; a change that only
  works on an existing nine-node fleet is not shippable.
- **It fails loudly.** A silent wrong answer is worse than a crash. If something cannot be
  computed honestly (merged counts across sensors, for instance) report a floor and say so,
  rather than a confident number.
- **It does not hardcode per-node facts.** Ports, bind addresses and names are environment
  variables. Pushing a dev copy of the aggregator with a hardcoded bind took eight of nine
  dashboards down once; `systemctl is-active` reported `active` the whole time.
- **It keeps the dashboard contract honest.** If you change an API response, update
  `grafana/` and `deploy/verify-dashboard-contract.py` in the same PR — panels that query a
  field that no longer exists fail silently and look like Grafana's fault.

## Reporting a problem

Include the node's log line, the exact command, and what you expected. For anything where the
honeypot is involved, say whether it was a **captured** session or your own test login — the
two look identical in the logs and it changes the diagnosis completely.

See `SECURITY.md` before filing anything that looks like a vulnerability.
