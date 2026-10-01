#!/usr/bin/env python3
"""
create_views.py

Create (or replace) the rail-filtering views in a loaded snapshot schema.
Depends only on the raw GTFS tables loaded by load_gtfs.py:

    rail_routes         routes with route_type in RAIL_ROUTE_TYPES
    rail_trips          trips on rail_routes
    regular_rail_trips  rail_trips whose service_id has at least one weekday set
                        in calendar (excludes exception-only services)
    rail_stop_times     stop_times on rail_trips
    rail_stops          stops (and their parent stations) used by rail_stop_times

Usage:
    python create_views.py --schema gtfs_20260921
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args

# Covers both basic GTFS codes and the "extended" codes some European feeds
# use. load_gtfs.py prints what the feed actually contains — adjust to match.
RAIL_ROUTE_TYPES = [0, 1, 2, 100, 109, 400, 900, 1000]


def create_views(conn, schema: str):
    types_list = ",".join(str(t) for t in RAIL_ROUTE_TYPES)
    with conn.cursor() as cur:
        cur.execute(f'''
            CREATE OR REPLACE VIEW "{schema}".rail_routes AS
            SELECT * FROM "{schema}".routes
            WHERE route_type::int = ANY(ARRAY[{types_list}]);

            CREATE OR REPLACE VIEW "{schema}".rail_trips AS
            SELECT t.* FROM "{schema}".trips t
            JOIN "{schema}".rail_routes r ON t.route_id = r.route_id;

            CREATE OR REPLACE VIEW "{schema}".regular_rail_trips AS
            SELECT t.* FROM "{schema}".rail_trips t
            JOIN "{schema}".calendar c USING (service_id)
            WHERE c.monday = '1' OR c.tuesday = '1' OR c.wednesday = '1'
               OR c.thursday = '1' OR c.friday = '1' OR c.saturday = '1'
               OR c.sunday = '1';

            CREATE OR REPLACE VIEW "{schema}".rail_stop_times AS
            SELECT st.* FROM "{schema}".stop_times st
            JOIN "{schema}".rail_trips t ON st.trip_id = t.trip_id;

            CREATE OR REPLACE VIEW "{schema}".rail_stops AS
            SELECT DISTINCT s.* FROM "{schema}".stops s
            WHERE s.stop_id IN (SELECT stop_id FROM "{schema}".rail_stop_times)
               OR s.stop_id IN (
                   SELECT parent_station FROM "{schema}".stops
                   WHERE parent_station IS NOT NULL
                     AND stop_id IN (SELECT stop_id FROM "{schema}".rail_stop_times)
               );
        ''')
    conn.commit()
    print(f'Views created in schema "{schema}": rail_routes, rail_trips, '
          f'regular_rail_trips, rail_stop_times, rail_stops')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    create_views(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
