# Reading the whole event-log window (surviving Cowrie's midnight rotation)

## The failure mode

Cowrie's JSON event log rotates once a day by default: `cowrie.json` is renamed to
`cowrie.json.YYYY-MM-DD` at 00:00 and a fresh `cowrie.json` starts. Any dashboard,
exporter or script that opens one fixed path therefore reports **only since midnight**:

- every total/stat tile collapses to a fraction of its previous value overnight;
- a "7 day" graph is silently showing one day, and looks like the sensor keeps dying;
- nothing errors, and no data is lost — the previous day is sitting next to it.

Symptom to recognise: a node reading e.g. 4,081 when it was 20,510 an hour before, with
the log's newest event just after 00:00 and a fat sibling file beside it.

## The shape of the fix

Read the window — the live file plus its rotated siblings — and merge:

1. **Discover** files by prefix in the log directory, accepting the live name plus any
   rotated name whose trailing `YYYY-MM-DD` is inside the retention window (7–8 days).
   Sorting by name works because the date is zero-padded.
2. **Fold each file incrementally, cached per path.** Keep `{offset, size, rollup}` per
   file and parse only the bytes appended since the last poll. This matters: a day of
   this log runs ~12 MB, so re-reading a 7-day window per HTTP request is not an option,
   and the honeypot nodes are small (one is a 458 MB box with swap added by hand).
   - When a file **shrinks** below the stored offset, it was rotated/truncated under you:
     reset that file's rollup and offset to zero.
   - Stop at the last complete newline (`rfind(b"\n")`). A half-written final line must
     be left for the next poll, or advancing the offset past it loses that event forever.
3. **Merge** file rollups into one window rollup: sum counters, merge hourly buckets, and
   per attacker take the sum of counts with `min(first_seen)` / `max(last_seen)`.
   Per-hour IP *sets* must be rebuilt into a fresh set while merging, never shared, or two
   merged views mutate each other.
4. **Prune** cache entries whose path dropped out of the window, so memory tracks the
   window instead of the process lifetime.
5. Keep the "recent activity" panels on a tail read of the newest file (last ~80 lines) —
   they want recency, not the window, and holding full command/payload lists for 7 days
   would be the expensive part.

Rotation timing is the point, so verify with an independent count rather than by trusting
the dashboard: sum every parseable, timestamped line across `cowrie.json*` in a shell, and
compare it to the API's total. They must be equal; an API value a few dozen events *behind*
is just the parse memo, and an API value *ahead* would be a bug.

A ready-made patcher lives with the deploy scripts (`30-logwindow-patch.py`, run it on each
node's `server.py`). It replaces only the module constants, `build_aggregate` and
`get_stats_uncached`, and inserts the window helpers, so per-node values — `SERVER_NAME`,
and the listen address/port in `__main__` (nodes are not all on the same port, and one uses
a dual bind) — survive untouched. It is idempotent (detects its own marker) and refuses to
write a file that does not parse.

## Cross-checks worth keeping

- Rotated files are *not* counted twice: at rotation the renamed path is new to the cache
  and is parsed once, while the live path's own entry resets when it shrinks.
- A node whose log directory is empty or missing must still serve zeroes, not 500s.
- Fleet-wide, historic totals only become comparable *after* every node runs the window
  build — a mixed fleet makes some nodes look like they are surging.

---

<div align="center">
  <sub><a href="../README.md">← back to the README</a> · MIT · built for a <a href="https://github.com/cowrie/cowrie">Cowrie</a> fleet</sub>
</div>
