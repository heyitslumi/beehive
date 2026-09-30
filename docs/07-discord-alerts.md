# Discord alert routing: one channel per node, and filtering the noise

## Per-node channels via the Composio Discord bot

Goal: every node posts its alerts into its own channel instead of one shared firehose.

1. Confirm the bot is a member and check its role bits. `DISCORDBOT_GET_GUILD` works even when the
   bot's role lacks perms, so read the role permissions and decode: `MANAGE_CHANNELS` = 1<<4 (16),
   `MANAGE_WEBHOOKS` = 1<<29 (536870912), `ADMINISTRATOR` = 1<<3 (8). A role of `274877974536`
   is admin, so no permission fiddling is needed.
2. Invite link for a bot: the bot *user id is the application id*, so
   `https://discord.com/oauth2/authorize?client_id=<bot_user_id>&scope=bot&permissions=<bits>`.
   Manage Channels + Manage Webhooks + View Channels + Send Messages = `536874000`.
3. Create the category (`type: 4`), then one text channel (`type: 0`) per node with `parent_id`
   set, then `DISCORDBOT_CREATE_WEBHOOK` in each. Move pre-existing channels into the category
   with `DISCORDBOT_UPDATE_CHANNEL` + `parent_id`.
4. Write the URL into the node's config **from a file, not argv**: ssh joins argv into one remote
   command string and mangles long URLs. Push `discord-url.txt`, then run a small remote python
   that reads it, regex-replaces the `url =` line inside `[output_discord]`, validates with
   `configparser(strict=True)`, backs up, and prints only the webhook ID.
5. The two Composio accounts (`discordbot_*`) are separate connections — test which one can see
   the target guild rather than assuming the default.

## Verifying alert delivery (do not trust a 200 from the webhook call)

Poke the trap and read the channel back:

- `ssh -o PreferredAuthentications=publickey -o BatchMode=yes -i <key> root@<public-ip>` produces a
  `cowrie.login.failed` without needing a password (no sshpass required).
- A bare TCP connect (`exec 3<>/dev/tcp/<ip>/22`) produces `session.connect` + `session.closed`.
- Then `DISCORDBOT_LIST_MESSAGES` on the channel: the embed must carry the expected `src_ip` and
  `webhook_id`, which proves the node posts to *that* channel.

## cowrie's discord output has NO event filter

Config keys are only `url`, `default_delay`, `retry_delay`, `max_retries`. It posts every event
(session.connect, session.closed, command.input, command.failed), so a busy trap floods a channel.
The source lives at `/cowrie/cowrie-git/src/cowrie/output/discord.py` in the image (verify with
`docker exec cowrie python3 -c "import inspect,cowrie.output.discord as m;print(m.__file__)"`).

Patch it by bind-mounting a modified copy over the image file, which survives container recreation:

```yaml
volumes:
  - ./etc/discord.py:/cowrie/cowrie-git/src/cowrie/output/discord.py:ro
```

Two edits in the module: read `events` in `start()`
(`frozenset(p.strip() for p in CowrieConfig.get("output_discord","events",fallback="").split(",") if p.strip())`)
and early-return in `write()` when the allowlist is non-empty and
`event.get("eventid")` is not in it. Empty allowlist = stock behaviour. Working values:
`cowrie.session.connect,cowrie.login.failed,cowrie.login.success,cowrie.session.file_download`
(deliberately excludes `command.input`, which is the bulk of the traffic and is visible in the
dashboard anyway).

Verify the running container imports the patched file, not the image's original:
`docker exec cowrie python3 -c "import inspect,cowrie.output.discord as m;print('_events_allow' in inspect.getsource(m.Output))"`.

## sensor_name: without it alerts show a container hash

`[honeypot] sensor_name = <node>` sets the `sensor` field on every alert. Left unset, cowrie reports
the container hostname, so alerts read `9e792646da7b` instead of `sensor1`.

## Pitfalls

- **A config change that does not take effect means the container was never recreated.** sensor1's
  trap was a plain `docker run` container, so `docker compose up -d` failed with
  `Conflict. The container name "/cowrie" is already in use` and the old process kept running with
  the old config. Compare `docker inspect` (ports, network mode, binds, restart policy) against the
  compose file, then `docker rm -f cowrie && docker compose up -d` to hand it to compose.
  A `docker restart` reloads the config but does NOT apply a new volume mount.
- **`docker cp <container>:<file> -` writes a tar to stdout**, so md5-comparing that output compares
  tar metadata (mtimes) and reports different hashes for identical files. Compare with
  `docker exec <c> md5sum <file>` or diff the extracted files.
- **A dashboard bound to 127.0.0.1 is not a regression.** sensor1's dashboard listens on loopback only
  with nginx in front; curling the tailnet IP returns 000 by design. Verify via the public
  `https://<host>/api/...` path that Grafana actually uses.
- **Grafana Cloud needs the aggregator's public API**, so "lock down the dashboard" must never break
  `/api/*` — the HTML UI can be restricted, the API endpoints cannot.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
