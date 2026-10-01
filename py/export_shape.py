#!/usr/bin/env python3
"""
export_shape.py

Export a single shape_id from the raw `shapes` table as a GeoJSON LineString.
No joins — just the shape's own points, in order.

Usage:
    python export_shape.py --schema gtfs_20260921 --shape 2345
"""

import argparse
import json
from pathlib import Path

from gtfs_common import add_connection_args, connect_from_args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    parser.add_argument("--shape", required=True, help="shape_id to export")
    parser.add_argument("--out-dir", type=Path, default=Path("output"))
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT shape_pt_lat::float, shape_pt_lon::float
            FROM "{args.schema}".shapes
            WHERE shape_id = %s
            ORDER BY shape_pt_sequence::int
            ''',
            (args.shape,),
        )
        points = cur.fetchall()
    conn.close()

    if not points:
        raise SystemExit(f"No points found for shape_id={args.shape} in schema {args.schema}")

    feature_collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in points],
            },
            "properties": {"shape_id": args.shape},
        }],
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.schema}_shape_{args.shape}.geojson"
    with open(out_path, "w") as f:
        json.dump(feature_collection, f)

    print(f"Wrote {len(points)} points to {out_path}")


if __name__ == "__main__":
    main()
