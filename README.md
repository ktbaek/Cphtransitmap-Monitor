# Copenhagen rail network monitoring from Rejseplanen GTFS

This project detects changes to the Copenhagen-area rail network using a GTFS static feed from [Rejseplanen Labs](https://labs.rejseplanen.dk/). The goal is to maintain and update the customer-facing map [Copenhagen Transit Map](https://cphtransitmap.dk/en). The tools detect new/closed stations, changed routing, and changes in service pattern.

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
  bit further:
  1. The raw tables hold every row from the feed as-is. Bus, ferry, and long-distance rail outside
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

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--gtfs-dir` | *Required* | Path to the unzipped GTFS feed directory |
  | `--snapshot-date` | *Required* | e.g. `2026-09-21` |
  | `--polygon` | `config/cph_area.geojson` | Path to Copenhagen area polygon |
  | `--force` | *(flag)* | Overwrite the snapshot's schema if it already exists. Without this flag, the script refuses to run again for a date that's already been loaded. |

- `create_views.py --schema ...`: creates/replaces the rail views (depend only on raw tables).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

- `derive_route_patterns.py --schema ...`: table `route_patterns`, one row per distinct ordered
  stop sequence per route/direction/headsign (`pattern_id`, `stop_ids`, `stop_names`,
  `stop_in_cph_area`, `n_trips`, `trip_ids`). Keeps only patterns touching the Copenhagen area.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

- `derive_service_pattern.py --schema ...`: presence table `stop_service_pattern`
  (`route_id, stop_id, day_type, time_band`). Weekday bands are set by constants at the top of the
  script; Saturday and Sunday/holiday are not split by time of day. Also creates view
  `service_pattern`, collapsing that into one named `service_category` per (route_id, stop_id)
  (e.g. `all_times`, `peak_only`).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

- `derive_stop_triplets.py --schema ...`: table `route_stop_triplets` (prev, stop, next), one row
  per (pattern, position), route_id-attributed. Triplets are used rather than pairwise edges
  because they preserve which through-movements exist at junctions (lines A-X-C and B-X-D meeting
  at X do not imply A-X-B).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

### Visualization helpers
These scripts generate GeoJSON files of the shapes in the GTFS data, for a visual overview. They
are not required for any analysis.
- `export_geojson.py`: shapes + stops per agency, with far-away geometry grouped onto
  `config/destination_*.geojson` points to keep files readable.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--agency` | *Required* | Agency id to export; repeat the flag for multiple agencies |
  | `--out-dir` | `output` | Output directory |
  | `--polygon` | `config/cph_area.geojson` | Path to Copenhagen area polygon |
  | `--destinations-dir` | `config` | Directory containing `destination_*.geojson` polygons |
  | `--destination-loc` | `config/destination_loc.txt` | Replacement coordinates for each destination polygon |
  | `--no-group` | *(flag)* | Disable collapsing of out-of-area geometry onto destination points |
  | `--all-services` | *(flag)* | Use `rail_trips` (includes exception-only services) instead of `regular_rail_trips` |

- `export_shape.py`: a single `shape_id`, no joins, for quick spot checks.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--shape` | *Required* | `shape_id` to export |
  | `--out-dir` | `output` | Output directory |

See `output/` for example exports.

### Checking against the current map
These scripts check whether the topology and service patterns shown on the map are consistent
with the GTFS data. `config/map_corridors.yml` and `config/map_service_patterns.yml` are a manual
encoding of what's currently drawn on the map — they are not generated from the GTFS data, and
need to be kept up to date by hand whenever the map changes.
- `check_map_corridors.py --schema ... [--corridors ...]`: print-only. Checks `route_stop_triplets`
  against map lines encoded in `config/map_corridors.yml` (per group of agencies, stops by
  `stop_name`, optional `ring: true`, optional `routes:` list of route_short_names). A triplet is
  consistent if one corridor on the map holds its non-NULL stops in order, either direction; NULL and
  out-of-area neighbours are wildcards. Scope is the route's own corridors if the route is listed
  (catches extensions along another line's corridor), otherwise all corridors of its group.
  Reports unknown stops, conflicts, and corridor stops not served by the corridor's routes.
  `config/map_corridors.yml` currently encodes the whole rail network: Metro (M1-M4), all S-tog
  lines, the DSB regional corridors (Kystbanen, Øresundsbanen, Vestbanen, and others), every
  Lokaltog line, and Letbanen.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--corridors` | `config/map_corridors.yml` | Path to the corridor definitions |

- `check_map_service_patterns.py --schema ... [--patterns ...]`: print-only. Checks view
  `service_pattern` against `config/map_service_patterns.yml`, a sparse per-route exception list
  (every stop a route serves is assumed `default: all_times` unless listed). Every route of every
  `agencies:` entry is checked (S-tog/Metro), whether or not it's mentioned under `routes:` — so an
  unlisted route with a real deviation isn't silently skipped. Reports category mismatches and map
  exceptions for stops the route doesn't serve.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--patterns` | `config/map_service_patterns.yml` | Path to the service pattern exceptions |

### Common options
Every script above also accepts these, defined once in `gtfs_common.py` and resolved into a
connection by `connect_from_args()`. You normally don't need to pass any of them: with nothing
set, every script reads the `default` block of `config/db.yml`. They exist so a single run can be
pointed elsewhere — e.g. a different environment via `--config-env`, or a different database
entirely via `--dsn`.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--dsn` | `$GTFS_DB_DSN` | PostgreSQL connection string; overrides `--config` if set |
| `--config` | `config/db.yml` | Path to config file |
| `--config-env` | `default` | Top-level key in `db.yml` to use |

## Run order
1. `load_gtfs.py`
2. `create_views.py`
3. `derive_route_patterns.py`
4. `derive_service_pattern.py` and `derive_stop_triplets.py` (either order, both just need steps 1-3 first)

## When a new feed arrives

Expected cadence: couple times a year. Snapshot dates below use `YYYYMMDD`; replace `20261001` with the date of the feed you downloaded.

### 1. Get and unpack the feed
Download the GTFS zip from Rejseplanen Labs (requires authorization, see [Data](#data)) and unzip it into `<data/GTFS_2026-10-01/>`. Check that the expected `.txt` files are present.

### 2. Load and derive
```bash
python load_gtfs.py --gtfs-dir <data/GTFS_2026-10-01> --snapshot-date 2026-10-01 --polygon config/cph_area.geojson
python create_views.py --schema gtfs_20261001
python derive_route_patterns.py --schema gtfs_20261001
python derive_service_pattern.py --schema gtfs_20261001
python derive_stop_triplets.py --schema gtfs_20261001
```
`load_gtfs.py` refuses to overwrite an existing schema. Use `--force` only if you want to drop and reload that snapshot.

### 3. Sanity-check the load
Before trusting any results, confirm the snapshot looks plausible:
- Row counts in the raw tables are in the same ballpark as the previous snapshot.
- `stops.in_cph_area` has both true and false values.
- `route_patterns` is non-empty and the number of regional/IC patterns is roughly as expected (about 30 per direction).

### 4. Check against the map
```bash
python check_map_corridors.py --schema gtfs_20261001
python check_map_service_patterns.py --schema gtfs_20261001
```
Both scripts only print. Read the output for:
- **Unknown stops** (in `config/map_corridors.yml` but not in the feed, or the reverse): usually a renamed, new or closed station.
- **Conflicts**: a triplet the map's corridors can't explain, meaning routing has changed or the map is wrong.
- **Corridor stops not served**: a closed station, or a line truncated on the map's side.
- **Service category mismatches**: a change in frequency pattern for a stop.

### 5. Compare with the previous snapshot
No diff script exists yet. Compare by hand with `EXCEPT` queries across the two schemas, for example on triplet presence:
```sql
-- triplets in the new snapshot but not in the old one (swap the two for removals)
SELECT <route_short_name, prev_stop_name, stop_name, next_stop_name>
FROM gtfs_20261001.route_stop_triplets
EXCEPT
SELECT <route_short_name, prev_stop_name, stop_name, next_stop_name>
FROM gtfs_<previous>.route_stop_triplets;
```
Compare on names and `route_short_name`, not IDs. It is not yet verified that `stop_id` and `route_id` stay stable between exports. Do the same for `stop_service_pattern` to catch line extensions and truncations.

### 6. Update the map and record the result
1. Decide per finding whether the feed or the map is right, then update the map.
2. Edit `config/map_corridors.yml` and `config/map_service_patterns.yml` to match what the map now shows. These files are hand-maintained and are the only record of what the map currently draws.
3. Re-run step 4. A clean result means the YAML files and the new snapshot agree.
4. Commit the YAML changes and update the line below.

**Map last synced to snapshot:** `<gtfs_YYYYMMDD>` (`<date checked>`)

## To do
- `transfers.txt` is loaded (`load_gtfs.py`'s `GTFS_FILES`) into a raw `transfers` table, but
  nothing reads it yet. Could be used to check rail-to-rail interchanges against the map's drawn
  connections (e.g. Nørreport St. ↔ Nørreport St. (Metro)).
- Other diffing/exploratory SQL queries to aid understanding of the network.
