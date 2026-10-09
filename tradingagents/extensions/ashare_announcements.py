"""DuckDuckGo discovery of CNInfo disclosure excerpts, not a filing archive.

Only the disclosure date encoded in a CNInfo finalpage PDF URL is used. It is
explicitly lower-confidence URL metadata, never a verified publication clock
or proof that the complete filing has been read. No PDF or arbitrary URL fetch.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from threading import RLock
from urllib.parse import parse_qs, urlsplit

import requests

from tradingagents.extensions.duckduckgo_news import (
    _clean,
    _public_url,
    _read_response,
    _relevance,
    _SearchFailure,
    _settings,
    block_status,
    record_block,
)

SEARCH_URL = "https://html.duckduckgo.com/html/"
_CACHE = OrderedDict()
_LOCK = RLock()


class _Results(HTMLParser):
    """Read only result title/snippet anchors, ignoring scripts and navigation."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.current = None
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self.current = {"url": attrs.get("href", ""), "title": "", "content": ""}
            self.rows.append(self.current)
            self.capture = "title"
        elif tag == "a" and "result__snippet" in classes and self.current is not None:
            self.capture = "content"

    def handle_endtag(self, tag):
        if tag == "a":
            self.capture = None

    def handle_data(self, data):
        if self.capture and self.current is not None:
            self.current[self.capture] += data


def _disclosure_url(raw):
    try:
        return _parse_disclosure_url(raw)
    except (TypeError, ValueError):
        return None


def _parse_disclosure_url(raw):
    if raw.startswith("//duckduckgo.com/l/"):
        raw = "https:" + raw
    parts = urlsplit(raw)
    if parts.hostname == "duckduckgo.com" and parts.path == "/l/":
        raw = parse_qs(parts.query).get("uddg", [""])[0]
    checked = _public_url(raw, ())
    if not checked or checked[1] != "static.cninfo.com.cn":
        return None
    parts = urlsplit(checked[0])
    match = re.fullmatch(r"/finalpage/(\d{4}-\d{2}-\d{2})/\d+\.pdf", parts.path, re.I)
    if not match:
        return None
    try:
        return checked[0], date.fromisoformat(match[1])
    except ValueError:
        return None


def fetch_announcements(identity: dict, start_date: str, end_date: str, config: dict) -> dict:
    code = identity.get("code") or identity["canonical_symbol"].split(".")[0]
    name = identity.get("chinese_short_name") or identity.get("chinese_full_name")
    result = {"status": "unavailable", "evidence": [], "diagnostics": {}}
    if not name:
        result["diagnostics"]["reason"] = "No verified Chinese company name for official discovery"
        return result
    blocked = block_status()
    if blocked:
        result["diagnostics"] = {**blocked, "stop_search": True, "cooldown": True}
        return result
    query = f'site:cninfo.com.cn "{code}" "{name}" 公告'
    try:
        settings = _settings(config)
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end:
            raise ValueError("reversed window")
    except (ValueError, TypeError):
        result["status"] = "invalid_request"
        result["diagnostics"]["reason"] = "Invalid bounded news configuration or date window"
        return result
    key = (query, start_date, end_date, settings["max_results"], settings["timeout"])
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < min(settings["cache_ttl"], 30 if cached[1]["status"] == "unavailable" else settings["cache_ttl"]):
            hit = deepcopy(cached[1])
            hit["diagnostics"]["cache_hit"] = True
            return hit
    transport = []
    try:
        with requests.Session() as session:
            session._duckduckgo_news_deadline = min(
                time.monotonic() + settings.get("total_timeout", 45),
                config.get("_duckduckgo_news_deadline", float("inf")),
            )
            session._duckduckgo_news_transport = transport
            session._duckduckgo_news_min_interval = settings["min_interval"]
            session._duckduckgo_news_pause_hours = settings["pause_hours"]
            body = _read_response(session, SEARCH_URL, {"q": query, "kl": "cn-zh"}, settings["timeout"])
        parser = _Results()
        parser.feed(body)
        # No recognised wire format is not a successful empty search response.
        if not parser.rows and not re.search(r"no-results|No results found", body, re.I):
            raise _SearchFailure("unsupported_search_response")
        seen = set()
        rejected = 0
        for row in parser.rows[:100]:
            disclosed = _disclosure_url(row["url"])
            title, content = _clean(row["title"], 400), _clean(row["content"], 2000)
            matched = _relevance(title, content, (name, code))
            if (not disclosed or not title or not content or name not in matched or code not in matched
                    or not start <= disclosed[1] <= min(end, datetime.now(UTC).date())):
                rejected += 1
                continue
            url, day = disclosed
            if url in seen:
                continue
            seen.add(url)
            result["evidence"].append({
                "title": title, "content": content, "content_kind": "search_snippet",
                "url": url, "publisher": "CNInfo issuer disclosure",
                "retrieval_provider": "DuckDuckGo", "source_category": "official_disclosure_search_match",
                "issuer_attribution": "name_and_code_in_excerpt_not_verified_document_authorship",
                "published_at": day.isoformat(), "publication_precision": "day",
                "publication_date_source": "url_path", "publication_time_verified": False,
                "document_text_retrieved": False, "matched_aliases": matched,
                "query": query, "retrieved_at": datetime.now(UTC).isoformat(),
            })
            if len(result["evidence"]) >= settings["max_results"]:
                break
        result["status"] = "ok" if result["evidence"] else "empty"
        result["diagnostics"] = {"query": query, "rejected": rejected, "cache_hit": False,
                                 "coverage": "One search sample, not a complete issuer filing history"}
    except _SearchFailure as exc:
        if exc.stop and exc.reason != "budget_exhausted" and not exc.block_recorded:
            record_block(exc.reason, settings["pause_hours"])
        result["diagnostics"] = {"reason": exc.reason, "stop_search": exc.stop}
        pause = getattr(exc, "pause_status", None) or block_status()
        if pause:
            result["diagnostics"]["pause"] = pause
    except requests.RequestException:
        result["diagnostics"] = {"reason": "transport_unavailable"}
    result["diagnostics"]["transport"] = transport
    if result["diagnostics"].get("reason") == "budget_exhausted":
        return result
    with _LOCK:
        _CACHE[key] = (time.monotonic(), deepcopy(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return result
