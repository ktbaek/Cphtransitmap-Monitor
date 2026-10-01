# Copenhagen rail network monitoring from Rejseplanen GTFS

This project detects changes to the Copenhagen-area rail network using a GTFS static feed from [Rejseplanen Labs](https://labs.rejseplanen.dk/). The goal is to maintain and update the customer-facing map [Copenhagen Transit Map](https://cphtransitmap.dk/en), where S-train and metro are shown as individual lines with service patterns, and regional/national rail is shown as one simplified line with no service patterns. The tools detect new/closed stations, changed routing, and changes in service pattern.

## Data
Access to the data requires authorization from Rejseplanen Labs.

## Stack and layout
Python + PostgreSQL. Connection settings in `config/db.yml` (block `default`; password optional, falls back to `PGPASSWORD`). Area polygons in `config/` as geojson files. No PostGIS is used, so point-in-polygon analyses are done in Python using `shapely`.

## Design decisions
- **One Postgres schema per snapshot**, named `gtfs_YYYYMMDD`. Diffing = cross-schema SQL.
- **Raw `COPY` load, every column stored as `text`.** Cast explicitly when needed
  (`stop_sequence::int`, `route_type::int`, `stop_lat::float`). Ordering by `stop_sequence`
  without the cast sorts lexicographically and scrambles patterns with 10+ stops (already fixed in `derive_route_patterns.py`).
- **Filtering stages**. Each step in the pipeline narrows the data a
  bit further, so it's worth knowing which question each stage answers:
  1. The raw tables hold every row from the feed as-is — bus, ferry, and long-distance rail outside
     the area are all still there.
  2. The `rail_*` views (`create_views.py`) narrow to rail route types only (`RAIL_ROUTE_TYPES`
     covers basic and extended GTFS codes), with no area restriction yet — a regional train is
     "rail" even far outside Copenhagen.
  3. `stops.in_cph_area` is a flag, not a filter: every stop keeps its row, so a regional line's
     full extent stays visible while it's still clear which of its stops are in scope.
  4. The derived tables (`route_patterns`, `stop_service_pattern`, `route_stop_triplets`) apply the
     real content filters: pass-through stops and exception-only services are dropped, and only
     patterns that touch the Copenhagen area are kept at all.
- **Copenhagen area** = `shapely` point-in-polygon on stop coordinates, written to a boolean
  column `stops.in_cph_area`. No hand-maintained station/agency lists, so new stations classify
  themselves.
- Excluded wherever it matters (stage 4 above): pass-through stops (`pickup_type` and
  `drop_off_type` both `1`), and trips whose `service_id` has an all-zero weekly calendar
  (exception-only services). `calendar_dates.txt` (holidays) is not modeled.

## Scripts

### Loading and deriving
These scripts load the relevant GTFS files into PostgreSQL tables and derive route patterns, service patterns, and stop triplets.
- `gtfs_common.py`: shared DB connection helpers (`--dsn`, `--config`, `--config-env`).
- `load_gtfs.py --gtfs-dir ... --snapshot-date YYYY-MM-DD [--polygon ...] [--force]`: loads raw
  tables, adds `in_cph_area`. Refuses to overwrite an existing schema without `--force` (which
  drops the schema first).
- `create_views.py --schema ...`: creates/replaces the rail views (depend only on raw tables).
- `derive_route_patterns.py --schema ...`: table `route_patterns`, one row per distinct ordered
  stop sequence per route/direction/headsign (`pattern_id`, `stop_ids`, `stop_names`,
  `stop_in_cph_area`, `n_trips`, `trip_ids`). Keeps only patterns touching the area.
- `derive_service_pattern.py --schema ...`: presence table `stop_service_pattern`
  (`route_id, stop_id, day_type, time_band`). Weekday bands are set by constants at the top of the
  script; Saturday and Sunday/holiday are not split by time of day. Also creates view
  `service_pattern`, collapsing that into one named `service_category` per (route_id, stop_id)
  (e.g. `all_times`, `peak_only`).
- `derive_stop_triplets.py --schema ...`: table `route_stop_triplets` (prev, stop, next), one row
  per (pattern, position), route_id-attributed. Triplets are used rather than pairwise edges
  because they preserve which through-movements exist at junctions (lines A-X-C and B-X-D meeting
  at X do not imply A-X-B).

### Visualization helpers
These scripts generate GeoJSON files of the shapes in the GTFS data, for a visual overview — they
are not required for any analysis.
- `export_geojson.py`: shapes + stops per agency, with far-away geometry grouped onto
  `config/destination_*.geojson` points to keep files readable.
- `export_shape.py`: a single `shape_id`, no joins, for quick spot checks.

See `output/` for example exports.

### Checking against the current map
These scripts check whether the topology and service patterns shown on the map are consistent
with the GTFS data.
- `check_map_corridors.py --schema ... [--corridors ...]`: print-only. Checks `route_stop_triplets`
  against map lines encoded in `config/map_corridors.yml` (per group of agencies, stops by
  `stop_name`, optional `ring: true`, optional `routes:` list of route_short_names). A triplet is
  consistent if one corridor in scope holds its non-NULL stops in order, either direction; NULL and
  out-of-area neighbours are wildcards. Scope is the route's own corridors if the route is listed
  (catches extensions along another line's corridor), otherwise all corridors of its group.
  Reports unknown stops, conflicts, and corridor stops not served by the corridor's routes.
  `config/map_corridors.yml` currently encodes the whole network: Metro (M1-M4), all S-tog
  lines, the DSB regional corridors (Kystbanen, Øresundsbanen, Vestbanen, and others), every
  Lokaltog line, and Letbanen.
- `check_map_service_patterns.py --schema ... [--patterns ...]`: print-only. Checks view
  `service_pattern` against `config/map_service_patterns.yml`, a sparse per-route exception list
  (every stop a route serves is assumed `default: all_times` unless listed). Every route of every
  `agencies:` entry is checked (S-tog/Metro), whether or not it's mentioned under `routes:` — so an
  unlisted route with a real deviation isn't silently skipped. Reports category mismatches and map
  exceptions for stops the route doesn't serve. `config/map_service_patterns.yml` currently has
  real exceptions encoded for S-tog routes A, Bx, C, E, H; B, F and all four Metro lines match
  `default: all_times` with no exceptions needed.

## Run order
1. `load_gtfs.py`
2. `create_views.py`
3. `derive_route_patterns.py`
4. `derive_service_pattern.py` and `derive_stop_triplets.py` (either order, both just need steps 1-3 first)

## Suggested maintenance analyses
- Run `check_map_corridors.py` and `check_map_service_patterns.py` against a new snapshot and
  review the two YAML files for drift against the map.
- Diffing between snapshots is not implemented yet. Plan: plain `EXCEPT` queries for triplet
  presence/absence. Whether `stop_id`/`route_id` stay stable between exports is unverified
  (`trip_id`/`service_id` almost certainly are not). Diffing `stop_service_pattern` on
  (route_short_name, stop name) would catch line extensions/truncations between snapshots.
- `transfers.txt` is loaded (`load_gtfs.py`'s `GTFS_FILES`) into a raw `transfers` table, but
  nothing reads it yet. Could be used to check rail-to-rail interchanges against the map's drawn
  connections (e.g. Nørreport St. ↔ Nørreport St. (Metro)).
- Other diffing/exploratory SQL queries to aid understanding of the network.

## Gotchas
- DBeaver's array cell viewer can show array elements out of order; the stored order is correct.
  Use `unnest(...) WITH ORDINALITY ... ORDER BY ord` to inspect.
- `array_position`/`@>` on `stop_names` tests membership, not adjacency.
