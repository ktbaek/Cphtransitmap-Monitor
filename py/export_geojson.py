#!/usr/bin/env python3
"""
export_geojson.py

Export rail line shapes and stops for one or more agencies as a single GeoJSON
FeatureCollection, for visual inspection of the network (e.g. in geojson.io or
QGIS). LineString features come from `shapes` (joined to route_id/route_short_name
via regular_rail_trips, or rail_trips with --all-services), Point features
from the `rail_stops` view.

To keep files small and readable, geometry outside the Copenhagen area
(config/cph_area.geojson) is collapsed onto a handful of fixed destination
points: each config/destination_<name>.geojson polygon maps to a replacement
coordinate in config/destination_loc.txt. Line points falling in the same
destination collapse to consecutive duplicate coordinates, which are then
dropped (no new shape_pt_sequence values are invented). Stops falling in the
same destination are merged into a single Point feature. Pass --no-group to
disable all of this and export raw, full-detail coordinates instead.

Usage:
    python export_geojson.py --schema gtfs_20260921 --agency 203
    python export_geojson.py --schema gtfs_20260921 --agency 203 --agency 318 --no-group
"""

import argparse
import csv
import json
from pathlib import Path

from gtfs_common import add_connection_args, connect_from_args


def load_polygon(path: Path):
    """Load a polygon from a GeoJSON file — a bare geometry, a Feature, or
    a FeatureCollection (in which case all *visible* features are unioned
    together; features with properties.visibility == false are reference/
    scratch shapes left over from editing in a map tool like geojson.io and
    are excluded)."""
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


def load_destinations(destinations_dir: Path, destination_loc_path: Path):
    """Returns {name: (polygon, (lat, lon))} for every destination_<name>.geojson
    in destinations_dir, matched against rows in destination_loc.txt."""
    locations = {}
    with open(destination_loc_path, newline="") as f:
        reader = csv.reader(f, skipinitialspace=True)
        next(reader)  # header
        for row in reader:
            if len(row) < 3:
                continue
            name, lat, lon = row
            locations[name.strip()] = (float(lat), float(lon))

    destinations = {}
    for path in sorted(destinations_dir.glob("destination_*.geojson")):
        name = path.stem.removeprefix("destination_")
        if name not in locations:
            raise SystemExit(
                f"{path} has no matching row in {destination_loc_path} "
                f"(looked for destination name '{name}')"
            )
        destinations[name] = (load_polygon(path), locations[name])

    missing = set(locations) - set(destinations)
    if missing:
        print(f"Note: destination_loc.txt rows with no matching polygon file, ignored: {sorted(missing)}")

    return destinations


def make_classifier(cph_area, destinations, group: bool):
    """Returns classify(lat, lon) -> (out_lat, out_lon, destination_name_or_None)."""
    from shapely.geometry import Point

    warned = set()

    def classify(lat, lon):
        if not group:
            return lat, lon, None

        point = Point(lon, lat)
        if cph_area.contains(point):
            return lat, lon, None

        for name, (polygon, (dest_lat, dest_lon)) in destinations.items():
            if polygon.contains(point):
                return dest_lat, dest_lon, name

        key = round(lat, 3), round(lon, 3)
        if key not in warned:
            warned.add(key)
            print(f"  Warning: point ({lat}, {lon}) is outside the Copenhagen area "
                  f"and outside every destination polygon — leaving it unchanged.")
        return lat, lon, None

    return classify


def export_lines(conn, schema: str, agency_ids, classify, group: bool, trips_view: str):
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT DISTINCT r.route_id, r.route_short_name, sh.shape_id, a.agency_id
            FROM "{schema}".shapes sh
            JOIN "{schema}".{trips_view} t USING (shape_id)
            JOIN "{schema}".routes r USING (route_id)
            JOIN "{schema}".agency a USING (agency_id)
            WHERE a.agency_id = ANY(%s)
            ''',
            (agency_ids,),
        )
        shape_routes = cur.fetchall()

    features = []
    n_points_before = 0
    n_points_after = 0
    n_dropped = 0

    with conn.cursor() as cur:
        for route_id, route_short_name, shape_id, agency_id in shape_routes:
            cur.execute(
                f'''
                SELECT shape_pt_lat::float, shape_pt_lon::float
                FROM "{schema}".shapes
                WHERE shape_id = %s
                ORDER BY shape_pt_sequence::int
                ''',
                (shape_id,),
            )
            raw_points = cur.fetchall()
            n_points_before += len(raw_points)

            coords = []
            for lat, lon in raw_points:
                out_lat, out_lon, _ = classify(lat, lon)
                coord = [out_lon, out_lat]
                if not coords or coords[-1] != coord:
                    coords.append(coord)
            n_points_after += len(coords)

            if len(coords) < 2:
                n_dropped += 1
                print(f"  Warning: shape_id={shape_id} route_id={route_id} collapsed to "
                      f"{len(coords)} point(s) — dropping this line.")
                continue

            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "shape_id": shape_id,
                    "route_id": route_id,
                    "route_short_name": route_short_name,
                    "agency_id": agency_id,
                },
            })

    print(f"Lines: {len(features)} exported ({n_dropped} dropped as degenerate), "
          f"{n_points_before} points -> {n_points_after} points"
          + (" (grouping on)" if group else " (grouping off)"))
    return features


def export_stops(conn, schema: str, agency_ids, classify, group: bool, trips_view: str):
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT s.stop_id, s.stop_name, s.stop_lat::float, s.stop_lon::float, s.in_cph_area,
                   array_agg(DISTINCT r.route_short_name ORDER BY r.route_short_name) AS route_short_names
            FROM "{schema}".rail_stops s
            JOIN "{schema}".rail_stop_times st USING (stop_id)
            JOIN "{schema}".{trips_view} t USING (trip_id)
            JOIN "{schema}".routes r USING (route_id)
            JOIN "{schema}".agency a USING (agency_id)
            WHERE a.agency_id = ANY(%s)
            GROUP BY s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, s.in_cph_area
            ''',
            (agency_ids,),
        )
        stops = cur.fetchall()

    features = []
    merged_groups = {}  # destination_name -> feature dict
    n_individual = 0
    n_merged_into = 0

    for stop_id, stop_name, lat, lon, in_cph_area, route_short_names in stops:
        out_lat, out_lon, destination = classify(lat, lon)

        if not group or destination is None:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [out_lon, out_lat]},
                "properties": {
                    "stop_id": stop_id,
                    "stop_name": stop_name,
                    "route_short_names": route_short_names,
                },
            })
            n_individual += 1
            continue

        group_feature = merged_groups.get(destination)
        if group_feature is None:
            group_feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [out_lon, out_lat]},
                "properties": {
                    "destination": destination,
                    "stop_ids": [],
                    "stop_names": [],
                    "route_short_names": [],
                    "n_stops": 0,
                    "marker-color": "#ff0000",
                },
            }
            merged_groups[destination] = group_feature

        props = group_feature["properties"]
        props["stop_ids"].append(stop_id)
        props["stop_names"].append(stop_name)
        props["route_short_names"] = sorted(set(props["route_short_names"]) | set(route_short_names))
        props["n_stops"] += 1
        n_merged_into += 1

    features.extend(merged_groups.values())

    print(f"Stops: {n_individual} exported individually, {n_merged_into} merged into "
          f"{len(merged_groups)} destination point(s)"
          + (" (grouping on)" if group else " (grouping off)"))
    return features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    parser.add_argument("--agency", required=True, action="append",
                         help="Agency id to export; repeat for multiple agencies.")
    parser.add_argument("--out-dir", type=Path, default=Path("output"))
    parser.add_argument("--polygon", type=Path, default=Path("config/cph_area.geojson"))
    parser.add_argument("--destinations-dir", type=Path, default=Path("config"))
    parser.add_argument("--destination-loc", type=Path, default=Path("config/destination_loc.txt"))
    parser.add_argument("--no-group", action="store_true",
                         help="Disable collapsing of out-of-area geometry onto destination points.")
    parser.add_argument("--all-services", action="store_true",
                         help="Use rail_trips (includes exception-only services) instead of regular_rail_trips.")
    add_connection_args(parser)
    args = parser.parse_args()

    group = not args.no_group
    trips_view = "rail_trips" if args.all_services else "regular_rail_trips"

    if group:
        cph_area = load_polygon(args.polygon)
        destinations = load_destinations(args.destinations_dir, args.destination_loc)
    else:
        cph_area = None
        destinations = {}

    classify = make_classifier(cph_area, destinations, group)

    conn = connect_from_args(args)
    line_features = export_lines(conn, args.schema, args.agency, classify, group, trips_view)
    stop_features = export_stops(conn, args.schema, args.agency, classify, group, trips_view)
    conn.close()

    feature_collection = {
        "type": "FeatureCollection",
        "features": line_features + stop_features,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ("_nogroup" if args.no_group else "") + ("_allservices" if args.all_services else "")
    out_path = args.out_dir / f"{args.schema}_agency_{'_'.join(args.agency)}{suffix}.geojson"
    with open(out_path, "w") as f:
        json.dump(feature_collection, f)

    print(f"\nWrote {len(feature_collection['features'])} features to {out_path}")


if __name__ == "__main__":
    main()
