#!/usr/bin/env python3
"""
derive_service_pattern.py

Classify, per (route_id, stop_id), which day-type / time-of-day service
categories are actually served in this snapshot:
    day_type:  'weekday' | 'saturday' | 'sunday_holiday'
    time_band: 'peak' | 'daytime' | 'evening'   (weekdays only)
               'all'                             (saturday / sunday_holiday
                                                    — not split by time of day)

Holiday exceptions in calendar_dates.txt are NOT modeled here — only
calendar.txt's regular weekly pattern is used. See project notes for why
(the coarse weekly pattern is enough for a twice-a-year structural check;
day-by-day holiday handling is a separate, more involved piece of work).

Stores only rows that ARE served — a presence table. A stop's absence for
a given (route_id, day_type, time_band) means "not served that way", which
is sufficient for diffing later: a row present in one snapshot and missing
in another is a real change (gained or lost service).

Creates/replaces "{schema}".stop_service_pattern with columns:
    route_id, stop_id, day_type, time_band

Also creates/replaces view "{schema}".service_pattern, which collapses the
presence rows above into one named service_category per (route_id, stop_id)
— e.g. all_times, peak_only, weekend_and_evening_only — for
check_map_service_patterns.py and manual inspection.

Only stops with stops.in_cph_area = true are included — a stop entirely
outside the area of interest (on a fully-external DSB regional/IC line,
mainly) is left out, same restriction as route_patterns.

Usage:
    python derive_service_pattern.py --schema gtfs_20260921
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args

# Weekday hour bands, as [start, end) in the local 24h clock. GTFS times
# past midnight are written as e.g. "25:10:00" for a trip that departs at
# 01:10 the next service day — since that's hour >= 24, it already falls
# outside every band below and lands in "evening" without needing any
# modulo arithmetic. Saturday/Sunday are not split by time of day at all.
PEAK_HOURS = [(6, 9), (14, 17)]
DAYTIME_HOURS = (11, 13)
EVENING_HOURS = (21, 24)


def build_time_band_case_sql() -> str:
    peak_conditions = " OR ".join(
        f"(hour >= {start} AND hour < {end})" for start, end in PEAK_HOURS
    )
    return f'''
        CASE
            WHEN {peak_conditions} THEN 'peak'
            WHEN hour >= {DAYTIME_HOURS[0]} AND hour < {DAYTIME_HOURS[1]} THEN 'daytime'
            WHEN hour >= {EVENING_HOURS[0]} AND hour < {EVENING_HOURS[1]} THEN 'evening'
            ELSE NULL
        END
    '''


def derive_service_pattern(conn, schema: str):
    time_band_case = build_time_band_case_sql()

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".stop_service_pattern CASCADE')
        cur.execute(f'''
            CREATE TABLE "{schema}".stop_service_pattern AS
            WITH trip_day_types AS (
                SELECT
                    t.trip_id,
                    t.route_id,
                    (c.monday = '1' OR c.tuesday = '1' OR c.wednesday = '1'
                     OR c.thursday = '1' OR c.friday = '1') AS is_weekday,
                    (c.saturday = '1') AS is_saturday,
                    (c.sunday = '1') AS is_sunday
                FROM "{schema}".rail_trips t
                JOIN "{schema}".calendar c ON c.service_id = t.service_id
            ),
            stop_time_bands AS (
                SELECT
                    trip_id,
                    stop_id,
                    {time_band_case} AS time_band
                FROM (
                    SELECT
                        st.trip_id,
                        st.stop_id,
                        split_part(st.arrival_time, ':', 1)::int AS hour
                    FROM "{schema}".stop_times st
                    JOIN "{schema}".stops s USING (stop_id)
                    WHERE st.arrival_time IS NOT NULL AND st.arrival_time <> ''
                      AND s.in_cph_area
                       AND NOT (
                          COALESCE(NULLIF(st.pickup_type, ''), '0') = '1'
                          AND COALESCE(NULLIF(st.drop_off_type, ''), '0') = '1'
                      )
                ) hourly
            ),
            combined AS (
                SELECT d.route_id, b.stop_id, 'weekday' AS day_type, b.time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_weekday AND b.time_band IS NOT NULL

                UNION

                SELECT d.route_id, b.stop_id, 'saturday' AS day_type, 'all' AS time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_saturday

                UNION

                SELECT d.route_id, b.stop_id, 'sunday_holiday' AS day_type, 'all' AS time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_sunday
            )
            SELECT DISTINCT route_id, stop_id, day_type, time_band
            FROM combined
        ''')
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT day_type, time_band, count(*)
            FROM "{schema}".stop_service_pattern
            GROUP BY day_type, time_band
            ORDER BY day_type, time_band
        ''')
        print(f'stop_service_pattern rows in "{schema}":')
        for day_type, time_band, n in cur.fetchall():
            print(f"  {day_type:<15} {time_band:<8} {n} (route, stop) combinations served")


def create_service_pattern_view(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'''
            CREATE OR REPLACE VIEW "{schema}".service_pattern AS
            WITH flags AS (
                SELECT
                    sp.route_id,
                    r.route_short_name,
                    sp.stop_id,
                    bool_or(day_type = 'weekday' AND time_band = 'peak')    AS has_peak,
                    bool_or(day_type = 'weekday' AND time_band = 'daytime') AS has_daytime,
                    bool_or(day_type = 'weekday' AND time_band = 'evening') AS has_evening,
                    bool_or(day_type = 'saturday')                          AS has_saturday,
                    bool_or(day_type = 'sunday_holiday')                    AS has_sunday
                FROM "{schema}".stop_service_pattern sp
                JOIN "{schema}".rail_routes r USING (route_id)
                GROUP BY route_id, route_short_name, stop_id
            ),
            labeled AS (
                SELECT
                    *,
                    array_remove(ARRAY[
                        CASE WHEN has_peak     THEN 'weekday_peak' END,
                        CASE WHEN has_daytime  THEN 'weekday_daytime' END,
                        CASE WHEN has_evening  THEN 'weekday_evening' END,
                        CASE WHEN has_saturday THEN 'saturday' END,
                        CASE WHEN has_sunday   THEN 'sunday_holiday' END
                    ], NULL) AS categories_present
                FROM flags
            )
            SELECT
                route_id,
                route_short_name,
                stop_id,
                s.stop_name,
                CASE
                    WHEN array_length(categories_present, 1) = 5 THEN 'all_times'
                    WHEN categories_present = ARRAY['weekday_peak']            THEN 'peak_only'
                    WHEN categories_present = ARRAY['saturday', 'sunday_holiday'] THEN 'weekend_only'
                    WHEN categories_present = ARRAY['weekday_evening', 'saturday', 'sunday_holiday'] THEN 'weekend_and_evening_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime', 'saturday', 'sunday_holiday'] THEN 'weekend_and_daytime_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime'] THEN 'weekday_daytime_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime', 'weekday_evening'] THEN 'weekday_only'
                    ELSE array_to_string(categories_present, '+')
                END AS service_category
            FROM labeled
            JOIN "{schema}".stops s USING (stop_id)
            ORDER BY route_id, stop_id
        ''')
    conn.commit()
    print(f'View "{schema}".service_pattern created.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_service_pattern(conn, args.schema)
    create_service_pattern_view(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
