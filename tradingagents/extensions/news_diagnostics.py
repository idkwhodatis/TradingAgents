"""Run one bounded DuckDuckGo news diagnostic without LLMs, keys or filings.

Example: python -m tradingagents.extensions.news_diagnostics --query 中国能建 --days 7
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.extensions.duckduckgo_news import block_status, fetch_news


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Read local provider pause only; never make HTTP requests")
    parser.add_argument("--query", help="Company name or specific news subject")
    parser.add_argument("--alias", help="Required company-name match; defaults to the query")
    parser.add_argument("--days", type=int, default=7, help="Lookback days (1..365; default 7)")
    parser.add_argument("--start-date", help="Explicit YYYY-MM-DD start, instead of --days")
    parser.add_argument("--end-date", help="Explicit YYYY-MM-DD end; defaults to today UTC")
    args = parser.parse_args(argv)
    if args.status:
        pause = block_status()
        print(json.dumps({
            "mode": "duckduckgo_pause_status_only", "pause": pause,
            "network_attempted": False,
            "note": "No active local pause is not proof of DuckDuckGo availability. Expiry never schedules a request.",
        }, ensure_ascii=False, indent=2))
        return 1 if pause and pause.get("retry_after_seconds") is None else 0
    if not args.query:
        parser.error("--query is required unless --status is used")
    if not 1 <= args.days <= 365:
        parser.error("--days must be between 1 and 365")
    try:
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else datetime.now(UTC).date()
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else end - timedelta(days=args.days)
        if start > end:
            raise ValueError("reversed dates")
    except ValueError:
        parser.error("Dates must be an ordered YYYY-MM-DD range")
    result = fetch_news([args.query], start.isoformat(), end.isoformat(), {
        "duckduckgo_news_pause_hours": DEFAULT_CONFIG["duckduckgo_news_pause_hours"],
        "duckduckgo_news_timeout": 15,
        "duckduckgo_news_total_timeout": 45,
        "duckduckgo_news_max_queries": 1,
        "duckduckgo_news_max_results": 3,
        "duckduckgo_news_cache_ttl": 0,
        "duckduckgo_news_aliases": [args.alias or args.query],
    })
    print(json.dumps({
        "mode": "duckduckgo_news_only_no_llm_no_official_discovery",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "accepted_count": len(result.get("evidence", [])), **result,
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ok", "partial", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
