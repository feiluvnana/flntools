# sync: synced queries

Part of [Phase 8](README.md). **Built in Phase 7, step 10** (user, 2026-11-13: "So should only have
download. Now download can choose a query target. Remove finder and call it a day."). A synced query
is `download --sync '<site>?<query>'`, one `sync ls` row, and each `flnd sync` runs it again. Its design
is [flnd §9](../phase-7/flnd.md#9-query-targets-710): at most 25 new results per query per run,
targets the user already follows left alone, seen results kept in `targets.db`.

Phase 8 keeps only the follow-ups that apply to any target-yielding route:

1. **Tests**, with fake routes and the fake clock:
   - 30 new results start 25 targets and leave 5 for the next sync;
   - a result whose target is already known, synced or not, is marked seen and not touched;
   - `--dry` lists the targets and marks no result seen;
   - a run stopped by Ctrl-C or `--limit` closes the route's generator: no further page is asked
     for, and only the results marked so far stay seen;
   - a route that raises fails its target (exit 1), and the next target still runs.
2. **Risk**: a site that re-keys its results between runs gets a target downloaded again. The
   target's history dedupes its files, so only its listing repeats.
