#!/usr/bin/env python3
"""
derive_trip_patterns.py

Compute one row per distinct stop-sequence ("pattern") for every
route/direction in a loaded GTFS snapshot schema. A trip pattern is
composed of all trips that are identical with regard to their stop
sequence.

Creates/replaces "{schema}".trip_patterns with columns:
    pattern_id, route_id, route_short_name, direction_id,
    first_stop, last_stop, stop_ids, stop_names, stop_in_cph_area
    (arrays, in visiting order), n_trips, trip_ids (which trips run this
    exact pattern)

pattern_id is a stable-within-this-table row identifier (not stable across
snapshots) — used by derive_stop_triplets.py to join stops within one
pattern's array without drifting into a different pattern of the same
route/direction.

first_stop/last_stop are just stop_names[1]/stop_names[-1], kept as their
own columns so a pattern's rough extent is visible without expanding
stop_names.

trip_headsign is not part of the grouping.

Trips whose service_id has an all-zero weekly calendar (no day of the week
set) are excluded — these are typically services defined purely through
one-off calendar_dates.txt exceptions (holiday specials, event trains)
rather than a recurring pattern, and aren't representative of the network's
regular topology. (Confirmed for this feed: every trip's service_id has a
calendar.txt row, so this is a plain INNER JOIN — if that ever changes,
double check services that exist only in calendar_dates.txt aren't being
silently dropped too.)

Only patterns that touch the area at all are kept — a route entirely
outside Copenhagen (fully-external DSB regional/IC lines, mainly) is
dropped rather than cluttering the table. A route that only partly
overlaps (e.g. a regional line passing through) is kept in full, with
stop_in_cph_area showing which of its stops are actually in scope.

Stops where the train passes through without actually stopping are
excluded from the sequence: per GTFS, pickup_type = 1 AND drop_off_type = 1
means no boarding and no alighting there, so it isn't a real stop on this
pattern as far as a passenger-facing view is concerned — matches the same
filter in derive_service_pattern.py.

A route/direction with more than one row here has more than one distinct
stop sequence in service (branches, skip-stop, short-turns, etc.).

Usage:
    python derive_trip_patterns.py --schema gtfs_20260921
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args


def derive_trip_patterns(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".trip_patterns')
        cur.execute(f'''
            CREATE TABLE "{schema}".trip_patterns AS
            WITH trip_seqs AS (
                SELECT
                    t.trip_id,
                    t.route_id,
                    r.route_short_name,
                    t.direction_id,
                    array_agg(st.stop_id ORDER BY st.stop_sequence::int) AS stop_ids,
                    array_agg(s.stop_name ORDER BY st.stop_sequence::int) AS stop_names,
                    array_agg(s.in_cph_area ORDER BY st.stop_sequence::int) AS stop_in_cph_area,
                    bool_or(s.in_cph_area) AS touches_cph_area
                FROM "{schema}".rail_trips t
                JOIN "{schema}".stop_times st USING (trip_id)
                JOIN "{schema}".stops s USING (stop_id)
                JOIN "{schema}".routes r USING (route_id)
                JOIN "{schema}".calendar c USING (service_id)
                WHERE (
                    c.monday = '1' OR
                    c.tuesday = '1' OR
                    c.wednesday = '1' OR
                    c.thursday = '1' OR
                    c.friday = '1' OR
                    c.saturday = '1' OR
                    c.sunday = '1'
                ) AND NOT (
                    COALESCE(NULLIF(st.pickup_type, ''), '0') = '1'
                    AND COALESCE(NULLIF(st.drop_off_type, ''), '0') = '1'
                )
                GROUP BY t.trip_id, t.route_id, r.route_short_name, t.direction_id
            )
            SELECT
                row_number() OVER (ORDER BY route_id, direction_id, stop_ids) AS pattern_id,
                route_id,
                route_short_name,
                direction_id,
                stop_names[1] AS first_stop,
                stop_names[array_length(stop_names, 1)] AS last_stop,
                stop_ids,
                stop_names,
                stop_in_cph_area,
                count(*) AS n_trips,
                array_agg(trip_id ORDER BY trip_id) AS trip_ids
            FROM trip_seqs
            WHERE touches_cph_area
            GROUP BY route_id, route_short_name, direction_id,
                     stop_ids, stop_names, stop_in_cph_area
        ''')
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT route_short_name, direction_id, count(*) AS n_patterns, sum(n_trips) AS n_trips
            FROM "{schema}".trip_patterns
            GROUP BY route_short_name, direction_id
            ORDER BY route_short_name, direction_id
        ''')
        print(f'Patterns per route/direction in "{schema}" (area-relevant only):')
        for route_short_name, direction_id, n_patterns, n_trips in cur.fetchall():
            flag = "" if n_patterns == 1 else "  <-- multiple patterns, worth checking"
            print(f"  {route_short_name:<8} dir={direction_id}  "
                  f"{n_patterns} pattern(s), {n_trips} trips{flag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_trip_patterns(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
