#!/usr/bin/env python3
"""
check_map_service_patterns.py

Check GTFS service categories (view "service_pattern") against the map's
service patterns, encoded by hand in config/map_service_patterns.yml as a
sparse list of exceptions per route: every stop a route serves is assumed
"default" (normally all_times) unless listed there.

Every route of every agency listed under `agencies:` is checked (normally
S-tog and Metro), not just routes mentioned under `routes:` — otherwise a
real deviation on a route nobody got around to adding to the YAML would
silently pass. Stop names are matched via the same whitespace-normalizing
comparison as check_map_corridors.py.

Reports, per route: mismatches (GTFS category differs from the map's
expectation) and map exceptions for a stop the route doesn't serve at all
(GTFS has no rows for that route/stop).

Usage:
    python check_map_service_patterns.py --schema gtfs_20260921
"""

import argparse
from collections import defaultdict
from pathlib import Path

import yaml

from gtfs_common import add_connection_args, connect_from_args


def normalize_name(name: str) -> str:
    """Collapse whitespace runs, since some feed stop_names contain stray double spaces."""
    return " ".join(name.split())


def load_expectations(path: Path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    default = cfg["default"]
    agencies = [str(a) for a in cfg["agencies"]]
    routes = {
        route: {normalize_name(stop_name): category for stop_name, category in stops.items()}
        for route, stops in (cfg.get("routes") or {}).items()
    }
    return default, agencies, routes


def fetch_actual(conn, schema: str, agencies):
    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT sp.route_short_name, sp.stop_name, sp.service_category
            FROM "{schema}".service_pattern sp
            JOIN "{schema}".routes r USING (route_id)
            WHERE r.agency_id = ANY(%s)
        ''', (agencies,))
        rows = cur.fetchall()
    # {route_short_name: {normalized_stop_name: (original_stop_name, category)}}
    actual = defaultdict(dict)
    for route, stop_name, category in rows:
        actual[route][normalize_name(stop_name)] = (stop_name, category)
    return actual


def check(default, routes, actual):
    """actual: every route in scope (from fetch_actual), whether or not it has YAML
    overrides. Returns {route: {"mismatches": [...], "not_served": [...]}}."""
    report = {}
    for route, served in actual.items():
        exceptions = routes.get(route, {})
        mismatches = []
        not_served = []

        for stop_name, (_, category) in served.items():
            expected = exceptions.get(stop_name, default)
            if category != expected:
                mismatches.append((stop_name, expected, category))

        for stop_name in exceptions:
            if stop_name not in served:
                not_served.append(stop_name)

        report[route] = {"mismatches": mismatches, "not_served": not_served}
    return report


def print_report(report, routes_with_no_data):
    for route in sorted(report):
        r = report[route]
        print(f"\n=== {route} ===")
        print(f"  {len(r['mismatches'])} mismatch(es), {len(r['not_served'])} exception(s) "
              f"for stops the route doesn't serve")

        if r["mismatches"]:
            print("  Mismatches (map expects vs. GTFS has):")
            for stop_name, expected, actual_cat in sorted(r["mismatches"]):
                print(f"    {stop_name}: expected {expected}, got {actual_cat}")

        if r["not_served"]:
            print("  Map exceptions for stops not served by this route in GTFS:")
            for stop_name in sorted(r["not_served"]):
                print(f"    {stop_name}")

    if routes_with_no_data:
        print(f"\nRoutes listed in the YAML with no rows for an agency in scope "
              f"(typo, renamed route, or wrong agency?): {', '.join(sorted(routes_with_no_data))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    parser.add_argument("--patterns", type=Path, default=Path("config/map_service_patterns.yml"))
    add_connection_args(parser)
    args = parser.parse_args()

    default, agencies, routes = load_expectations(args.patterns)

    conn = connect_from_args(args)
    actual = fetch_actual(conn, args.schema, agencies)
    conn.close()

    if not actual:
        raise SystemExit(f"No service_pattern rows found for agencies {agencies}")

    report = check(default, routes, actual)
    routes_with_no_data = set(routes) - set(actual)
    print_report(report, routes_with_no_data)


if __name__ == "__main__":
    main()
