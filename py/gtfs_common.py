#!/usr/bin/env python3
"""Shared connection helpers for the GTFS monitoring scripts (load_gtfs.py,
derive_route_patterns.py, derive_service_pattern.py, and eventually
compare_gtfs.py). Reads the same config/db.yml format used elsewhere."""

import os
from pathlib import Path

import psycopg2
import yaml


def dsn_from_yaml(config_path: Path, env: str = "default") -> str:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)[env]
    password = cfg.get("password", os.environ.get("PGPASSWORD", ""))
    auth = cfg["user"] if not password else f"{cfg['user']}:{password}"
    return (f"postgresql://{auth}@{cfg['host']}:{cfg.get('port', 5432)}"
            f"/{cfg['dbname']}")


def add_connection_args(parser):
    """Adds the standard --dsn / --config / --config-env options."""
    parser.add_argument("--dsn", default=os.environ.get("GTFS_DB_DSN"),
                         help="PostgreSQL connection string. Defaults to $GTFS_DB_DSN.")
    parser.add_argument("--config", type=Path, default=Path("config/db.yml"),
                         help="Path to a db.yml config file (used if --dsn is not given).")
    parser.add_argument("--config-env", default="default",
                         help="Top-level key in db.yml to use, e.g. 'default'.")


def connect_from_args(args):
    """Resolves --dsn / --config into a live psycopg2 connection."""
    if args.dsn:
        dsn = args.dsn
    elif args.config.exists():
        dsn = dsn_from_yaml(args.config, args.config_env)
    else:
        raise SystemExit(f"No DSN and no config file found (looked for {args.config}). "
                          "Pass --dsn or --config.")
    return psycopg2.connect(dsn)
