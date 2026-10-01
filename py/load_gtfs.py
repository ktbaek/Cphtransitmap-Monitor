#!/usr/bin/env python3
"""
load_gtfs.py

Load a raw Rejseplanen GTFS snapshot into its own PostgreSQL schema
(one schema per fetch, e.g. gtfs_20260921), using native COPY — no
type-guessing, no pandas. Rail-filtering happens afterwards via SQL
views, not in Python.

Usage:
    python load_gtfs.py --gtfs-dir /path/to/unzipped_gtfs --snapshot-date 2026-09-21

Connection: reads config/db.yml by default (matching your existing
config), or pass --dsn / --config explicitly.
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg2
import yaml

from create_views import RAIL_ROUTE_TYPES

# Files to load as-is. Add "shapes.txt" here too if you want route geometry.
GTFS_FILES = [
    "agency.txt", "routes.txt", "stops.txt", "trips.txt",
    "stop_times.txt", "calendar.txt", "calendar_dates.txt", 
    "shapes.txt", "transfers.txt"
]


def dsn_from_yaml(config_path: Path, env: str = "default") -> str:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)[env]
    password = cfg.get("password", os.environ.get("PGPASSWORD", ""))
    auth = cfg["user"] if not password else f"{cfg['user']}:{password}"
    return (f"postgresql://{auth}@{cfg['host']}:{cfg.get('port', 5432)}"
            f"/{cfg['dbname']}")


def schema_name_for(snapshot_date: str) -> str:
    return "gtfs_" + snapshot_date.replace("-", "")


def schema_exists(conn, schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
        return cur.fetchone() is not None


def load_raw_tables(conn, gtfs_dir: Path, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        for filename in GTFS_FILES:
            path = gtfs_dir / filename
            table = filename.removesuffix(".txt")

            if not path.exists():
                print(f"  (missing optional file: {filename}, skipping)")
                continue

            with open(path, newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f))

            cols_sql = ", ".join(f'"{c}" text' for c in header)
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE')
            cur.execute(f'CREATE TABLE "{schema}"."{table}" ({cols_sql})')

            with open(path, encoding="utf-8-sig") as f:
                cur.copy_expert(
                    f'COPY "{schema}"."{table}" FROM STDIN WITH (FORMAT csv, HEADER true)',
                    f,
                )
            cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
            print(f"  {table}: loaded {cur.fetchone()[0]} rows")
    conn.commit()


def load_polygon(path: Path):
    """Load a polygon from a GeoJSON file — a bare geometry, a Feature, or
    a FeatureCollection (in which case all *visible* features are unioned
    together; features with properties.visibility == false are reference/
    scratch shapes left over from editing in a map tool like geojson.io and
    are excluded)."""
    import json
    from shapely.geometry import shape
    from shapely.ops import unary_union

    with open(path) as f:
        gj = json.load(f)

    if gj.get("type") == "FeatureCollection":
        visible = [feat for feat in gj["features"]
                   if feat.get("properties", {}).get("visibility", True)]
        return unary_union([shape(feat["geometry"]) for feat in visible])
    if gj.get("type") == "Feature":
        return shape(gj["geometry"])
    return shape(gj)


def classify_stops_in_area(conn, schema: str, polygon_path: Path):
    """Computed fresh from each snapshot's own stop coordinates — nothing
    to maintain by hand, so newly built stations are classified correctly
    the moment they appear in a feed, same as everything else.

    Adds an `in_cph_area` boolean column directly onto the raw `stops`
    table (rather than a separate filtered table/view) so routes, trips,
    and stop_times are left untouched — a route can legitimately have
    stops both inside and outside the area, and that's still visible.
    Filter with `WHERE in_cph_area` wherever you want the restriction."""
    from shapely.geometry import Point
    from psycopg2.extras import execute_values

    polygon = load_polygon(polygon_path)

    with conn.cursor() as cur:
        cur.execute(f'ALTER TABLE "{schema}".stops ADD COLUMN IF NOT EXISTS in_cph_area boolean')
        cur.execute(f'SELECT stop_id, stop_lat, stop_lon FROM "{schema}".stops')
        rows = cur.fetchall()

    flags = [
        (stop_id, polygon.contains(Point(float(lon), float(lat))))
        for stop_id, lat, lon in rows
    ]

    with conn.cursor() as cur:
        execute_values(
            cur,
            f'''UPDATE "{schema}".stops AS s SET in_cph_area = v.in_area
                FROM (VALUES %s) AS v(stop_id, in_area)
                WHERE s.stop_id = v.stop_id''',
            flags,
            template="(%s, %s)",
        )
    conn.commit()

    n_in = sum(1 for _, in_area in flags if in_area)
    print(f"Area classification: {n_in} of {len(flags)} stops fall inside {polygon_path.name} "
          f"(stops.in_cph_area column added/updated)")


def print_route_type_summary(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT route_type, agency_id, count(*) AS n_routes
            FROM "{schema}".routes
            GROUP BY route_type, agency_id
            ORDER BY route_type, agency_id
        ''')
        print("\nDistinct (route_type, agency_id) combinations in this feed:")
        for route_type, agency_id, n in cur.fetchall():
            flag = "KEEP" if int(route_type) in RAIL_ROUTE_TYPES else "skip"
            print(f"  [{flag:4}] route_type={route_type:<5} agency_id={agency_id!s:<10} n_routes={n}")
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs-dir", required=True, type=Path)
    parser.add_argument("--snapshot-date", required=True, help="e.g. 2026-09-21")
    parser.add_argument("--dsn", default=os.environ.get("GTFS_DB_DSN"))
    parser.add_argument("--config", type=Path, default=Path("config/db.yml"))
    parser.add_argument("--config-env", default="default")
    parser.add_argument("--polygon", type=Path, default=Path("config/cph_area.geojson"),
                         help="GeoJSON polygon defining the area of interest. "
                              "Skipped (with a warning) if the file doesn't exist.")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite the snapshot's schema if it already exists. "
                              "Without this flag, the script refuses to run again for a "
                              "date that's already been loaded.")
    args = parser.parse_args()

    if not args.gtfs_dir.exists():
        sys.exit(f"GTFS directory not found: {args.gtfs_dir}")

    if args.dsn:
        dsn = args.dsn
    elif args.config.exists():
        dsn = dsn_from_yaml(args.config, args.config_env)
    else:
        sys.exit(f"No DSN and no config file found (looked for {args.config}). "
                  "Pass --dsn or --config.")

    schema = schema_name_for(args.snapshot_date)
    conn = psycopg2.connect(dsn)

    if schema_exists(conn, schema):
        if not args.force:
            conn.close()
            sys.exit(f'Schema "{schema}" already exists — refusing to overwrite it. '
                      f"Pass --force to reload this snapshot date anyway.")
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.commit()
        print(f'--force: dropped existing schema "{schema}" (including its views) before reloading.')

    print(f"Loading {args.gtfs_dir} into schema \"{schema}\" ...")
    load_raw_tables(conn, args.gtfs_dir, schema)
    print_route_type_summary(conn, schema)

    if args.polygon.exists():
        classify_stops_in_area(conn, schema, args.polygon)
    else:
        print(f"No polygon file found at {args.polygon} — skipping area classification "
              f"(stops.in_cph_area will not exist). Pass --polygon to point elsewhere.")

    conn.close()
    print(f"\nDone. Next: python create_views.py --schema {schema}")


if __name__ == "__main__":
    main()
