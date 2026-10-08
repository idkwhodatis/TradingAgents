"""Explicit archive management: python -m tradingagents.storage --help."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import SQLiteStorage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing run archive database")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List recent runs and metadata")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--ticker")
    show = commands.add_parser("show", help="Read a run or a named text artifact")
    show.add_argument("run_id")
    show.add_argument("--artifact")
    export = commands.add_parser("export", help="Re-render Markdown/HTML into a new directory")
    export.add_argument("run_id")
    export.add_argument("destination", type=Path)
    export.add_argument("--no-html", action="store_true")
    retention = commands.add_parser("retention", help="Preview retention, or explicitly apply it")
    retention.add_argument("--max-age-days", type=float)
    retention.add_argument("--max-bytes", type=int, help="Logical encoded payload quota, NOT file size")
    retention.add_argument("--apply", action="store_true", help="Delete the selected terminal runs")
    vacuum = commands.add_parser("vacuum", help="Explicit physical reclamation; use while idle")
    vacuum.add_argument("--apply", action="store_true", help="Run VACUUM (can require extra disk space)")
    commands.add_parser("stats", help="Logical payload bytes and physical DB/WAL sizes")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("--db must identify an existing archive; analysis creates new archives")
    archive = SQLiteStorage(args.db)
    if args.command == "list":
        result = archive.list_runs(limit=args.limit, ticker=args.ticker)
    elif args.command == "show":
        if args.artifact:
            value = archive.read_artifact(args.run_id, args.artifact)
            if not isinstance(value, str):
                parser.error("binary artifacts require the Python API")
            print(value)
            return
        result = archive.get_run(args.run_id)
    elif args.command == "export":
        result = {"output_dir": str(archive.export_run(args.run_id, args.destination, html=not args.no_html))}
    elif args.command == "retention":
        if args.max_age_days is None and args.max_bytes is None:
            parser.error("retention requires --max-age-days and/or --max-bytes")
        result = archive.retention(max_age_days=args.max_age_days, max_bytes=args.max_bytes,
                                   dry_run=not args.apply)
    elif args.command == "vacuum":
        if not args.apply:
            parser.error("vacuum requires --apply; use stats to inspect current sizes")
        result = archive.vacuum()
    else:
        result = archive.statistics()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
