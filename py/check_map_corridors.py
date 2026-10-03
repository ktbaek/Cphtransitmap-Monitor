#!/usr/bin/env python3
"""
check_map_corridors.py

Check GTFS (prev, stop, next) triplets against the map's lines, encoded by hand
in config/map_corridors.yml as canonical corridors (maximum length, all stops,
in order). Groups map agencies to modes. A corridor may list the routes
(route_short_name) drawn on it: triplets of a listed route are checked only
against that route's corridors, so an extension along another line's corridor
is caught. Triplets of unlisted routes are checked against every corridor of
their group.

A triplet is consistent if one corridor in scope contains all its non-NULL
stops in strictly increasing or strictly decreasing position. For corridor
ABCDEF, CDE, ACE (skip-stop) and AF-NULL (terminus) are all consistent. NULL
ends are wildcards, so short-turns are never conflicts. Ring corridors
(ring: true) may wrap around. Stops outside the Copenhagen area count as NULL,
and triplets centred on one are skipped.

Reports, per group: unknown stops (served, but on no corridor in scope),
conflicts (all stops known, but no corridor in scope orders them) and unserved
corridor stops (on the map, but not served by the corridor's routes, or by the
group if it lists none). Groups without corridors are skipped. Print-only;
requires stop_triplets (derive_stop_triplets.py).

Usage:
    python check_map_corridors.py --schema gtfs_20260921
"""

import argparse
from collections import defaultdict
from pathlib import Path

import yaml

from gtfs_common import add_connection_args, connect_from_args


def is_ordered(positions, ring_size=None):
    """True if positions strictly advance in one direction (modulo ring_size for rings)."""
    steps = list(zip(positions, positions[1:]))
    if not steps:
        return True
    if ring_size is None:
        return all(a < b for a, b in steps) or all(a > b for a, b in steps)
    for diffs in ([(b - a) % ring_size for a, b in steps],
                  [(a - b) % ring_size for a, b in steps]):
        if 0 not in diffs and sum(diffs) < ring_size:
            return True
    return False


def is_consistent(stops, corridors):
    """stops: non-NULL stop_ids in triplet order. corridors: list of corridor dicts."""
    for c in corridors:
        index = c["index"]
        if all(s in index for s in stops) and is_ordered([index[s] for s in stops], c["ring_size"]):
            return True
    return False


def normalize_name(name: str) -> str:
    """Collapse whitespace runs, since some feed stop_names contain stray double spaces."""
    return " ".join(name.split())


def load_corridors(path: Path, name_to_ids):
    with open(path) as f:
        cfg = yaml.safe_load(f)

    agency_group = {}
    for group, spec in cfg["groups"].items():
        for agency_id in spec["agencies"]:
            agency_group[str(agency_id)] = group

    corridors = defaultdict(list)        # group -> [corridor]
    route_corridors = defaultdict(list)  # (group, route_short_name) -> [corridor]
    problems = []
    for name, spec in (cfg.get("corridors") or {}).items():
        if spec.get("group") not in cfg["groups"]:
            problems.append(f"{name}: unknown or missing group {spec.get('group')!r}")
            continue
        if "stops" not in spec:
            problems.append(f"{name}: no 'stops' list")
            continue
        stop_names = [normalize_name(s) for s in spec["stops"]]
        index = {}
        for pos, stop_name in enumerate(stop_names):
            if stop_name not in name_to_ids:
                problems.append(f"{name}: stop name not in rail_stops: {stop_name}")
                continue
            for stop_id in name_to_ids[stop_name]:
                index[stop_id] = pos
        corridor = {
            "name": name,
            "group": spec["group"],
            "index": index,
            "ring_size": len(stop_names) if spec.get("ring") else None,
            "stops": stop_names,
            "routes": [str(r) for r in spec.get("routes") or []],
        }
        corridors[spec["group"]].append(corridor)
        for route in corridor["routes"]:
            route_corridors[(spec["group"], route)].append(corridor)

    if problems:
        raise SystemExit("Problems in corridor file:\n  " + "\n  ".join(problems))
    return agency_group, corridors, route_corridors


def fetch_stops(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'SELECT stop_id, stop_name, in_cph_area FROM "{schema}".rail_stops')
        rows = cur.fetchall()
    stop_info = {stop_id: (name, bool(in_area)) for stop_id, name, in_area in rows}
    name_to_ids = defaultdict(list)
    for stop_id, name, _ in rows:
        name_to_ids[normalize_name(name)].append(stop_id)
    return stop_info, name_to_ids


def fetch_triplets(conn, schema: str):
    """Returns {(agency_id, route_short_name, prev, stop, next): n_trips}."""
    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT r.agency_id, t.route_short_name, t.prev_stop_id, t.stop_id, t.next_stop_id,
                   sum(t.n_trips)
            FROM "{schema}".stop_triplets t
            JOIN "{schema}".routes r USING (route_id)
            GROUP BY 1, 2, 3, 4, 5
        ''')
        return {row[:5]: row[5] for row in cur.fetchall()}


def check(triplets, stop_info, agency_group, corridors, route_corridors):
    in_area = lambda s: s is not None and stop_info.get(s, (None, False))[1]

    known_cache = {}

    def known_stops(scope, scope_corridors):
        if scope not in known_cache:
            known_cache[scope] = set().union(*(c["index"] for c in scope_corridors))
        return known_cache[scope]

    per_group = defaultdict(lambda: {
        "checked": 0, "consistent": 0, "n_conflicting": 0, "n_unknown": 0, "skipped": 0,
        "conflicts": defaultdict(lambda: [set(), 0]),  # (triplet, scope) -> [routes, n_trips]
        "unknown": defaultdict(set),                   # (stop_id, scope) -> routes
        "served_group": set(),
        "served_route": defaultdict(set),
        "routes_seen": set(),
    })
    unmapped_agencies = defaultdict(int)
    groups_without_corridors = set()

    for (agency_id, route, prev_id, stop_id, next_id), n_trips in triplets.items():
        group = agency_group.get(agency_id)
        if group is None:
            unmapped_agencies[agency_id] += 1
            continue
        if group not in corridors:
            groups_without_corridors.add(group)
            continue
        g = per_group[group]
        g["routes_seen"].add(route)
        if not in_area(stop_id):
            g["skipped"] += 1
            continue

        key = (prev_id if in_area(prev_id) else None, stop_id, next_id if in_area(next_id) else None)
        stops = [s for s in key if s is not None]
        g["checked"] += 1
        g["served_group"].add(stop_id)
        g["served_route"][route].add(stop_id)

        if (group, route) in route_corridors:
            scope, scope_corridors = f"route {route}", route_corridors[(group, route)]
        else:
            scope, scope_corridors = "group", corridors[group]

        known = known_stops((group, scope), scope_corridors)
        unknown = [s for s in stops if s not in known]
        if unknown:
            g["n_unknown"] += 1
            for s in unknown:
                g["unknown"][(s, scope)].add(route)
        elif is_consistent(stops, scope_corridors):
            g["consistent"] += 1
        else:
            g["n_conflicting"] += 1
            entry = g["conflicts"][(key, scope)]
            entry[0].add(route)
            entry[1] += n_trips

    return per_group, unmapped_agencies, groups_without_corridors


def print_report(per_group, unmapped_agencies, groups_without_corridors,
                 stop_info, corridors, route_corridors, name_to_ids):
    name = lambda s: "∅" if s is None else stop_info[s][0]

    for group in sorted(corridors):
        g = per_group[group]
        print(f"\n=== {group} ({', '.join(c['name'] for c in corridors[group])}) ===")
        print(f"  {g['checked']} route triplets checked: {g['consistent']} consistent, "
              f"{g['n_conflicting']} conflicting, {g['n_unknown']} with unknown stops; "
              f"{g['skipped']} skipped (centre outside area)")

        missing_routes = sorted(r for (grp, r) in route_corridors
                                if grp == group and r not in g["routes_seen"])
        if missing_routes:
            print(f"  Listed routes with no triplets: {', '.join(missing_routes)}")

        if g["unknown"]:
            print("  Unknown stops (served, but on no corridor in scope):")
            for (s, scope), routes in sorted(g["unknown"].items(),
                                             key=lambda kv: (kv[0][1], name(kv[0][0]))):
                print(f"    [{scope}] {name(s)}  ({', '.join(sorted(routes))})")

        if g["conflicts"]:
            print("  Conflicts (no corridor in scope orders these stops):")
            for (key, scope), (routes, n_trips) in sorted(
                    g["conflicts"].items(), key=lambda kv: (kv[0][1], [name(s) for s in kv[0][0]])):
                print(f"    [{scope}] {' -> '.join(name(s) for s in key)}  "
                      f"({', '.join(sorted(routes))}) {n_trips} trips")

        unserved = []
        for c in corridors[group]:
            if c["routes"]:
                served = set().union(*(g["served_route"][r] for r in c["routes"]))
            else:
                served = g["served_group"]
            unserved += [(c["name"], stop_name) for stop_name in c["stops"]
                         if not any(s in served for s in name_to_ids[stop_name])]
        if unserved:
            print("  Unserved corridor stops (on the map, not served by its routes/group):")
            for corridor_name, stop_name in unserved:
                print(f"    {corridor_name}: {stop_name}")

    if groups_without_corridors:
        print(f"\nGroups without corridors (skipped): {', '.join(sorted(groups_without_corridors))}")
    if unmapped_agencies:
        print("Agencies in no group (not checked): "
              + ", ".join(f"{a} ({n} triplets)" for a, n in sorted(unmapped_agencies.items())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    parser.add_argument("--corridors", type=Path, default=Path("config/map_corridors.yml"))
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    stop_info, name_to_ids = fetch_stops(conn, args.schema)
    agency_group, corridors, route_corridors = load_corridors(args.corridors, name_to_ids)
    triplets = fetch_triplets(conn, args.schema)
    conn.close()

    per_group, unmapped_agencies, groups_without_corridors = check(
        triplets, stop_info, agency_group, corridors, route_corridors)
    print_report(per_group, unmapped_agencies, groups_without_corridors,
                 stop_info, corridors, route_corridors, name_to_ids)


if __name__ == "__main__":
    main()
