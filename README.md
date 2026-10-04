# Copenhagen rail network monitoring from Rejseplanen GTFS

This project detects changes to the Copenhagen-area rail network using a [GTFS](https://gtfs.org) static feed from [Rejseplanen Labs](https://labs.rejseplanen.dk/). The goal is to maintain and update the customer-facing map [Copenhagen Transit Map](https://cphtransitmap.dk/en). The project contains tools that detect new/closed stations, changed routing, and changes in service pattern.

## Data
Access to the data requires authorization from Rejseplanen Labs. The GTFS data is not included in this repository and is subject to Rejseplanen Labs' terms. Unzipped feeds go in `data/`.

## Setup
Python 3.9+ and PostgreSQL (no PostGIS needed). Dependencies:
- `psycopg2` (or `psycopg2-binary`) — database connection
- `PyYAML` — reading the `config/*.yml` files
- `shapely` — point-in-polygon checks for the Copenhagen area and GeoJSON export grouping

```bash
pip install psycopg2-binary PyYAML shapely
```

Connection settings go in `config/db.yml` (block `default`; password optional, falls back to `PGPASSWORD`). The database must already exist.

Area polygons live in `config/` as GeoJSON files.

## When a new feed arrives

Dates: `YYYY-MM-DD` in the feed folder name and `--snapshot-date`, `YYYYMMDD` in the schema name (`gtfs_YYYYMMDD`). Replace them with the date of the feed you downloaded.

### 1. Get and unpack the feed
Download the GTFS zip from Rejseplanen Labs (requires authorization, see [Data](#data)) and unzip it into `<data/GTFS_YYYY-MM-DD/>`. Check that the expected `.txt` files are present.

### 2. Load and derive
```bash
python load_gtfs.py --gtfs-dir data/GTFS_YYYY-MM-DD --snapshot-date YYYY-MM-DD
python create_views.py --schema gtfs_YYYYMMDD
python derive_trip_patterns.py --schema gtfs_YYYYMMDD
python derive_service_pattern.py --schema gtfs_YYYYMMDD
python derive_stop_triplets.py --schema gtfs_YYYYMMDD
```

Each script needs the ones above it; the last two only need steps 1-3 and can run in either order. `load_gtfs.py` refuses to overwrite an existing schema. Use `--force` only if you want to drop and reload that snapshot.

### 3. Check the end date of the feed
Check how long the current calendar is valid for:
```sql
SELECT min(start_date::date) AS first_day,
       min(end_date::date)   AS earliest_end,
       max(end_date::date)   AS latest_end
FROM gtfs_<new>.calendar
WHERE service_id IN (SELECT service_id FROM gtfs_<new>.regular_rail_trips);
```
If `earliest_end` is only weeks away, the next timetable isn't in this feed yet, so note it and plan to re-fetch once the new one is published. If `first_day` is in the future, the feed describes an upcoming timetable, so compare it against the map with that in mind.

### 4. Check against the map
```bash
python check_map_corridors.py --schema gtfs_YYYYMMDD
python check_map_service_patterns.py --schema gtfs_YYYYMMDD
```
Both scripts only print. Read the output for:
- **Unknown stops** (in `config/map_corridors.yml` but not in the feed, or the reverse): usually a renamed, new or closed station.
- **Conflicts**: a triplet the map's corridors can't explain, meaning routing has changed or the map is wrong.
- **Unserved corridor stops**: usually a closed station still drawn on the map, a name mismatch, or a route that no longer stops there.
- **Listed routes with no triplets**: usually a typo, a renamed route_short_name, or a route that's been discontinued.
- **Service category mismatches**: a change in frequency pattern for a stop.

*Known false positives*: Høvelte St., early morning trips of S-tog route H to Frederikssund St., afternoon trips of S-tog route F to Klampenborg St. See [Gotchas](#gotchas).

### 5. Compare with the previous snapshot
Keep the previous snapshot's schema in the database until this step is done, and compare by hand with `EXCEPT` queries across the two schemas, for example on triplet presence:
```sql
-- triplets in the new snapshot but not in the old one (swap the two for removals)
SELECT <route_short_name, prev_stop_name, stop_name, next_stop_name>
FROM gtfs_<new>.stop_triplets
EXCEPT
SELECT <route_short_name, prev_stop_name, stop_name, next_stop_name>
FROM gtfs_<previous>.stop_triplets;
```
Compare on names and `route_short_name`, not IDs. Do the same for `stop_service_pattern` to catch line extensions and truncations.

### 6. Update the map and record the result
1. Decide per finding whether the feed or the map is right, then update the map.
2. Edit `config/map_corridors.yml` and `config/map_service_patterns.yml` to match what the map now shows (see [Config files](#config-files)). 
3. Re-run step 4. A clean result means the YAML files and the new snapshot agree.
4. Commit the YAML changes.


## Pipeline
Each snapshot lives in its own Postgres schema, named `gtfs_YYYYMMDD`, so diffing is cross-schema SQL. Raw tables are loaded with `COPY` and every column is stored as `text`; cast explicitly when needed (see [Gotchas](#gotchas)).
1. **Raw tables** hold every row from the feed as-is. Bus, ferry, and long-distance rail outside the area are all still there.
2. **`rail_*` views** (`create_views.py`) narrow to rail route types only, with no area restriction yet. They include `rail_stops`, `rail_trips`, and `regular_rail_trips` (`rail_trips` without exception-only services).
3. **`stops.in_cph_area`** is a flag, not a filter: a stop is inside if its coordinates fall in the polygon `config/cph_area.geojson` (`shapely` point-in-polygon). There are no hand-maintained station lists, so new stations classify themselves.
4. **Derived tables** (`trip_patterns`, `stop_service_pattern`, `stop_triplets`) apply content filters: pass-through stops and exception-only services are dropped, and only patterns that touch the Copenhagen area are kept. Details under [Trip pattern](#trip-pattern).


## Concepts

### Trip pattern
A trip pattern is one distinct ordered sequence of stops that trips on a route run in one direction. `trip_patterns` has one row per pattern, with the stops (ids, names, in-area flags) as arrays in visiting order, plus the trips that run it. In other words, all trips having the same sequence of stops per `route_id` and direction constitute one trip pattern. 

Left out are pass-through stops (`pickup_type` and `drop_off_type` both `1`), trips whose service has an all-zero weekly calendar, and patterns that never touch the Copenhagen area. Trips that overlap the area are kept in full, with `stop_in_cph_area` marking which stops are in scope. `pattern_id` is just a row identifier within one snapshot, so it can't be used to match patterns between snapshots.

### Stop triplets
Triplets (prev, stop, next) are the unit for map checks and snapshot diffing. `stop_triplets` has one row per (pattern, position), each attributed to the route it comes from. `pattern_id` stays on each row, so any suspicious triplet can be traced back to its full pattern in `trip_patterns`. Termini and out-of-area neighbours are NULL wildcards, so line ends are checked more loosely.
 
A map check can ask whether each triplet's stops sit in order on some corridor, with no need to split patterns at junctions. Unlike pairwise edges, triplets also keep which through-movements exist at a junction (lines A-X-C and B-X-D meeting at X do not imply A-X-B).
 
### Corridor
A corridor is one line as drawn on the map, written out in `config/map_corridors.yml` as its complete stop list in order, at maximum length. 

A triplet is consistent if its stops appear in order on one corridor, in either direction. Corridors belong to a group (`metro`, `stog`, `dsb`, `lokaltog`, `letbane`, defined by GTFS agency IDs) and may list the `routes` (by `route_short_name`) that run on them. A triplet is checked against its route's own corridors if the route is listed, otherwise against all corridors in its group. Stops are matched by exact `stop_name` (metro stations end in "(Metro)"), so a station renamed in the feed shows up as an unknown stop. A line that loops (e.g. M3) sets `ring: true`. Corridors may overlap: M1 and M2 each list the shared trunk from Vanløse to Christianshavn, because they are separate lines on the map.


## Scripts

### Loading and deriving
These scripts load the relevant GTFS files into PostgreSQL tables and derive trip patterns, service patterns, and stop triplets.

#### `gtfs_common.py`
Shared DB connection helpers (`--dsn`, `--config`, `--config-env`).

#### `load_gtfs.py`
Loads raw tables (all columns as `text`) and adds the boolean `in_cph_area` to the `stops` table (see [Pipeline](#pipeline)).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--gtfs-dir` | *Required* | Path to the unzipped GTFS feed directory |
  | `--snapshot-date` | *Required* | e.g. `2026-09-21` |
  | `--polygon` | `config/cph_area.geojson` | Path to Copenhagen area polygon |
  | `--force` | *(flag)* | Overwrite the snapshot's schema if it already exists. Without this flag, the script refuses to run again for a date that's already been loaded. |

#### `create_views.py`
Creates/replaces the rail views (`rail_stops`, `rail_trips`, `regular_rail_trips`, `<others>`). Rail route types come from `RAIL_ROUTE_TYPES`, which covers basic and extended GTFS codes.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_trip_patterns.py`
Creates table `trip_patterns`, one row per distinct ordered stop sequence per route/direction Columns: `pattern_id`, `route_id`, `route_short_name`, `direction_id`, `first_stop`, `last_stop`, `stop_ids`, `stop_names`, `stop_in_cph_area` (arrays in visiting order), `n_trips`, `trip_ids`. See [Trip pattern](#trip-pattern) for what is kept and left out.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_service_pattern.py`
Creates presence table `stop_service_pattern` (`route_id, stop_id, day_type, time_band`), applying the same pass-through and exception-only filters as the trip patterns. Weekday bands are set by constants at the top of the script, which are the source of truth for the hours below. Saturday and Sunday are not split by time of day and are tested together (holidays are not modeled, see [Gotchas](#gotchas)).
 
Also creates view `service_pattern`, collapsing the presence table into one named `service_category` per (route_id, stop_id):
 
  | `service_category` | Weekday peak | Weekday daytime | Weekday evening | Sat/Sun |
  | :--- | :-: | :-: | :-: | :-: |
  | `all_times` | ✓ | ✓ | ✓ | ✓ |
  | `weekday_only` | ✓ | ✓ | ✓ | – |
  | `weekday_daytime_only` | ✓ | ✓ | – | – |
  | `peak_only` | ✓ | – | – | – |
  | `weekend_and_daytime_only` | ✓ | ✓ | – | ✓ |
  | `weekend_and_evening_only` | – | – | ✓ | ✓ |
  | `weekend_only` | – | – | – | ✓ |
 
The bands are the windows the script tests, not full coverage of the day: peak 06–09 and 14–17, daytime 11–13, evening 21–24. Because Saturday and Sunday are tested together there is no Saturday-only or Sunday-only category. Any combination that doesn't match a row above falls back to a raw `+`-joined list of the bands it was actually seen in.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_stop_triplets.py`
Creates table `stop_triplets` (prev, stop, next), one row per (pattern, position), attributed to the route it comes from. Columns: `<list columns>`. See [Triplets](#triplets). 

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

### Visualization helpers
These scripts generate GeoJSON files of the shapes in the GTFS data, for a visual overview. They
are not required for any analysis.

#### `export_geojson.py`
Shapes + stops per agency, with far-away geometry grouped onto
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

#### `export_shape.py`
A single `shape_id`, no joins, for quick spot checks.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--shape` | *Required* | `shape_id` to export |
  | `--out-dir` | `output` | Output directory |

See `output/` for example exports.

### Checking against the current map
These scripts check whether the topology and service patterns shown on the map are consistent
with the GTFS data, using the hand-maintained files described under [Config files](#config-files).

#### `check_map_corridors.py`
Print-only. Checks `stop_triplets` against the corridors in `config/map_corridors.yml` (matching rules under [Corridor](#corridor)). Reports unknown stops, conflicts, and corridor stops not served by the corridor's routes.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--corridors` | `config/map_corridors.yml` | Path to the corridor definitions |

#### `check_map_service_patterns.py`
Print-only. Checks view `service_pattern` against `config/map_service_patterns.yml`. Every route of every `agencies:` entry is checked (S-tog/Metro), whether or not it's mentioned under `routes:`, so an unlisted route with a real deviation isn't silently skipped. Reports category mismatches and map exceptions for stops the route doesn't serve.

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


## Config files
`config/map_corridors.yml` and `config/map_service_patterns.yml` are manual encodings of what's currently drawn on the map. They are not generated from the GTFS data, and they are the only record of what the map currently draws, so edit them whenever the map changes.
 
**`map_corridors.yml`** currently encodes the whole rail network: Metro (M1-M4), all S-tog lines, the DSB regional corridors (Kystbanen, Øresundsbanen, Vestbanen, and others), every Lokaltog line, and Letbanen. Stop names must match `rail_stops.stop_name` exactly, although runs of whitespace in either the YAML file or the GTFS feed are tolerated. Excerpt:
 
```yaml
groups:
  metro:    {agencies: ['319']}
  stog:     {agencies: ['318']}
  dsb:      {agencies: ['203', '471', '401', '451']}
  lokaltog: {agencies: ['316']}
  letbane:  {agencies: ['481']}
 
corridors:
  M1:
    group: metro
    routes: [M1]
    stops:
      - Vanløse St. (Metro)
      - Flintholm St. (Metro)
      # ...
```
 
**`map_service_patterns.yml`** is a sparse per-route exception list: every stop a route serves is assumed `default: all_times` unless listed. Excerpt:
 
```yaml
default: all_times

agencies:
  - '318'  # DSB S-tog
  - '319'  # Metroselskabet

routes:
  A:
    Virum St.: weekend_and_evening_only
    Sorgenfri St.: weekend_and_evening_only
```

## Gotchas
- **Stop name signifiers.** Stop names in the feed may add signifiers, e.g. "(Metro)" as in "Nørreport St. (Metro)", or "(Hillerød)" for some Lokaltog stops as in "Kagerup St. (Hillerød)".
- **Text columns.** Every raw column is `text`, so cast before comparing or sorting (`stop_sequence::int`, `route_type::int`, `stop_lat::float`). Ordering by `stop_sequence` without the cast sorts lexicographically and scrambles patterns with 10+ stops (already fixed in `derive_trip_patterns.py`).
- **Holidays are not modeled.** `calendar_dates.txt` is not read, so holiday timetables don't affect `stop_service_pattern`.
- **Høvelte St.** appears in the feed as a stop but is intentionally not shown on the map.

## To do
- `transfers.txt` is loaded (`load_gtfs.py`'s `GTFS_FILES`) into a raw `transfers` table, but
  nothing reads it yet. Could be used to check rail-to-rail interchanges against the map's drawn
  connections (e.g. Nørreport St. ↔ Nørreport St. (Metro)).
- A snapshot diff script, replacing the manual `EXCEPT` queries in [step 5](#5-compare-with-the-previous-snapshot).
- Other diffing/exploratory SQL queries to aid understanding of the network.

## License

The code and configuration in this repository are released under the MIT License (see `LICENSE`). This does not cover the [Copenhagen Transit Map](https://cphtransitmap.dk/en) itself, including its design, data and name, which are not part of this repository. The GTFS data is not included and is subject to Rejseplanen Labs' terms.
 