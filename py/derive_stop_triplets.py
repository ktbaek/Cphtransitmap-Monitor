#!/usr/bin/env python3
"""
derive_stop_triplets.py

Builds (prev_stop, stop, next_stop) triplets from trip_patterns — a plain
A-B edge can't tell you which through-combinations actually exist at a
junction where multiple lines cross (e.g. lines A-X-C and B-X-D meet at X,
but A-X-B, B-X-C, C-X-D, D-X-A don't exist even though the track layout
could support them). A triplet disambiguates this by keeping one stop of
context on each side.

Requires "{schema}".trip_patterns to already exist (run
derive_trip_patterns.py first) — triplets are built from it, so they
inherit every filter already baked into that table: area restriction,
pass-through-stop exclusion, and the calendar-based exclusion of
exception-only services.

Creates one table:

  stop_triplets — one row per (pattern, position), keeping
      route_id/direction_id/pattern_id so you can attribute a given
      transition to a specific route. The first stop of a pattern has
      prev_stop_id = NULL, the last has next_stop_id = NULL — these NULLs
      are meaningful (route termini), not missing data, and are kept
      rather than filtered out, since a terminus changing between
      snapshots is itself a real network change.

Usage:
    python derive_stop_triplets.py --schema gtfs_20260921
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args


def derive_stop_triplets(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".stop_triplets')
        cur.execute(f'''
            CREATE TABLE "{schema}".stop_triplets AS
            WITH pattern_stops AS (
                SELECT
                    rp.pattern_id,
                    rp.route_id,
                    rp.route_short_name,
                    rp.direction_id,
                    rp.n_trips,
                    u.stop_id,
                    u.ord
                FROM "{schema}".trip_patterns rp,
                     unnest(rp.stop_ids) WITH ORDINALITY AS u(stop_id, ord)
            )
            SELECT
                curr.route_id,
                curr.route_short_name,
                curr.direction_id,
                curr.pattern_id,
                curr.n_trips,
                prev.stop_id AS prev_stop_id,
                curr.stop_id,
                next.stop_id AS next_stop_id
            FROM pattern_stops curr
            LEFT JOIN pattern_stops prev
              ON prev.pattern_id = curr.pattern_id AND prev.ord = curr.ord - 1
            LEFT JOIN pattern_stops next
              ON next.pattern_id = curr.pattern_id AND next.ord = curr.ord + 1
        ''')

    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}".stop_triplets')
        n_triplets = cur.fetchone()[0]

    print(f'"{schema}": {n_triplets} stop_triplets rows.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_stop_triplets(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
