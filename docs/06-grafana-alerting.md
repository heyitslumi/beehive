# Grafana alerting from a limited API token

Writing alert **rules** and writing **contact points / notification policies** need different
permissions. Expect the rule half to work and the routing half to 403.

## What each permission lets you do

| operation | endpoint | permission |
|---|---|---|
| read rules/state | `GET /api/prometheus/grafana/api/v1/rules?type=alert` | read |
| create/update a rule **group** | `PUT /api/v1/provisioning/folder/<folderUid>/rule-groups/<group>` | `alert.rules:write` |
| delete a group | `DELETE /api/v1/provisioning/folder/<folderUid>/rule-groups/<group>` | `alert.rules:write` |
| contact points | `POST /api/v1/provisioning/contact-points` | `alert.provisioning:write` |
| notification policy | `PUT /api/v1/provisioning/policies` | `alert.provisioning:write` |

A Grafana-MCP service account is typically read-only on the last two. The 403 body is
`{"accessErrorId":"ACE…","message":"You'll need additional permissions…"}` — **that ACE string is
an error id, not a resource UID**; GETting it as a UID returns 404 and sends you down a wrong path.
Always look at the HTTP **status**, never apply a `jq` filter to a write whose result you have not
seen yet — the filter error hides the status code that explains everything.

Consequence: rules can be fully provisioned by the agent, but alerts stay on the **default email
receiver** until a human (or a browser session with the user's own login) adds a Discord/webhook
contact point and points the default policy at it.

## Provision the whole group in ONE request

The rule-group PUT replaces the entire group, so define all rules in a single call instead of N
rule calls. Body: `{title, interval, rules: [...]}`; each rule `{uid, title, condition, data,
noDataState, execErrState, for, labels, annotations, isPaused}`.

## Mandatory: `instant: true` on Prometheus rule queries

The A-node model must carry `"instant": true, "range": false`. Without it the rule still shows
`health: ok` while every instance reads **`Normal (Error)`** with an empty value, and a rule that
uses `execErrState: Alerting` will then **false-fire** — a query error reported as a real incident.

Verify with the API, not the rule definition:

```
GET /api/prometheus/grafana/api/v1/rules?type=alert
jq: [.data.groups[] | select(.name=="<group>") | .rules[]
     | {name, state, health, alerts: [.alerts[]? | {node: .labels.node, state: .state}]}]
```

Expect `health: ok`, `state: inactive`, and one `Normal` instance per expected series (e.g. per
node). Wait a full evaluation interval after a write before judging it.

## Alerting semantics that matter

- **"Node stopped reporting"** needs `noDataState: Alerting` — a dead box pushes no metrics at all,
  so `up == 0` never fires. Keep `execErrState: OK` so a transient datasource hiccup cannot page
  anyone; NoData (absence) is the correct signal, an error is not.
- **Threshold rules need explicit `for`** (e.g. `10m`) or they flap on every scrape blip.
- **Beware 0/0 = NaN** in ratio expressions: nodes with no swap return `NaN` for
  `1 - SwapFree/SwapTotal`, which renders as broken series. Filter the denominator:
  `node_memory_SwapTotal_bytes{...} > 0`.
- Add the alert threshold as a **dashed threshold line** on the matching dashboard panel so the
  graph and the rule tell the same story at the same number.
- Annotations with `{{ $labels.node }}` make multi-node alerts readable; reference the threshold
  result as `{{ $values.C.Value }}` where `C` is the threshold refId.
- Full JSON writes of a dashboard created *after* an alert rule must be re-verified: patch the
  single expression and confirm `version` incremented rather than trusting the response body.

## Proving the pipe end to end

A configured-but-untested alert path is not a finished one. Expect to do this after every routing
change; it takes about three minutes and it is the only thing that proves delivery.

### Routing it (the human step, and the UI's trap)

The contact-point row's `⋯` menu in current Grafana offers only Manage permissions / Export /
Delete — **there is no "Set as default"**. Route it instead from **Alerting → Notification policies
→ default policy → Edit → Default contact point → Save policy**.

Confirm by the badge, not the toast: the policy card must read **"Delivered to <name>"** and the
contact-point badge must flip from **Unused** to **Used by 1 notification policies**. A contact point
that shows a configured integration while still tagged **Unused** means nothing is delivered — the
alerts are going to whatever the old receiver was (a stock email receiver often reports "No
integrations configured", i.e. alerts silently vanish).

Rules written without `notification_settings` inherit the default policy, so re-pointing the
**default receiver** is sufficient — no per-rule routing and no nested policies are needed.

### The throwaway rule

Use a constant expression: `vector(99)` with a threshold `> 1`. It is deterministic, depends on no
real series, and sidesteps quoting a PromQL selector with label matchers inside a JSON body (nested
quotes there are a reliable source of malformed payloads). Create it as its own rule group so
cleanup is one DELETE, with `for: 1m`, `noDataState: OK`, `execErrState: OK`, and a title marked
"delete me".

Write rule payloads through `grafana_api_request` as a **single escaped JSON string** body
(`POST /api/v1/provisioning/alert-rules`, or the group PUT for several rules). Deeply nested objects
passed as tool-call arguments can fail argument JSON validation; the same payload as one string is
accepted, and it keeps the request body reviewable.

### Check the rule before the channel

`GET /api/prometheus/grafana/api/v1/rules?type=alert` — expect `health: ok`, `state: Alerting`
(with a real value), while the production rules stay `inactive`. Budget ~2–3 minutes from creation:
evaluation interval + the rule's `for` + the policy's group wait (30 s by default).

Then read the destination channel back: Discord bot toolkit `DISCORDBOT_LIST_MESSAGES` with
`channel_id` (newest first, optional `limit`), and match the message's `webhook_id` against the
webhook you wired — read the embed title for the `[FIRING:n] <rule> <folder> (labels)` line. The
Grafana→Discord payload carries labels, annotations, a rule link and a one-click silence link, so
put a human sentence in `annotations.summary` / `annotations.description`: that text is what the
user actually reads. Note that the Composio meta-tool takes search queries as an array of
`{use_case, known_fields}` **objects**, not bare strings.

### Clean up and re-verify

`DELETE /api/v1/provisioning/folder/<folderUid>/rule-groups/<group>` (a 204). Deleting a firing rule
also emits a resolved notification — harmless, and extra evidence. Re-GET the rules list and confirm
only the real groups remain, the production rules read `health: ok` / `state: inactive`, and the
per-node instance count is intact.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · <a href="./README.md">docs index</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
