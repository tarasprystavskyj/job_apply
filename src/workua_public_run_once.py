from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_platforms import DiscoveryQuery
from job_platforms.workua import run_workua_public_search_once
from shared_job_db import DEFAULT_DB_PATH


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one bounded public Work.ua discovery pass and stop at review-only artifacts."
    )
    parser.add_argument("--query", required=True, help="Public Work.ua search text, for example 'Python AI'.")
    parser.add_argument("--location", default="", help="Optional Work.ua location slug/text.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum vacancies to persist from the public page.")
    parser.add_argument("--source-url", default="", help="Optional explicit public Work.ua search/listing URL.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Shared SQLite DB path.")
    parser.add_argument("--artifact-dir", type=Path, default=None, help="Directory for approval/blocker JSON artifacts.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    query = DiscoveryQuery(
        text=args.query,
        location=args.location,
        limit=args.limit,
        source_url=args.source_url,
    )
    summary = run_workua_public_search_once(
        query,
        db_path=args.db_path,
        artifact_dir=args.artifact_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
