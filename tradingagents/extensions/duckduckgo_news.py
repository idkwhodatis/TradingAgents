"""Bounded, DuckDuckGo-only discovery of dated, source-screened news snippets.

No DDGS metasearch defaults, API key, proxy rotation, article fetches, retries or
CAPTCHA handling are used. The fixed DuckDuckGo root/news.js request protocol is
independently implemented from the upstream engine's documented behavior:
https://github.com/deedy5/ddgs/blob/main/ddgs/engines/duckduckgo_news.py

Search results are untrusted third-party text, never instructions or verified
full articles. An allowlisted hostname is a source-screening rule, not a claim
that the underlying story is correct or an official issuer announcement. A
finite search sample (including an empty sample) cannot prove absence of news.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from email.utils import parsedate_to_datetime
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from urllib3.util import Timeout

SEARCH_URL = "https://duckduckgo.com/"
NEWS_URL = "https://duckduckgo.com/news.js"
# Deliberately excludes open publishing platforms, aggregators, and stock forums.
NEWS_DOMAINS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "ft.com",
        "wsj.com",
        "cnbc.com",
        "bbc.com",
        "bbc.co.uk",
        "theguardian.com",
        "nytimes.com",
        "washingtonpost.com",
        "economist.com",
        "marketwatch.com",
        "barrons.com",
        "nikkei.com",
        "scmp.com",
        "channelnewsasia.com",
        "straitstimes.com",
        "theglobeandmail.com",
        "afr.com",
        "abc.net.au",
        "dw.com",
        "france24.com",
        "xinhuanet.com",
        "news.cn",
        "people.com.cn",
        "cnstock.com",
        "stcn.com",
        "cs.com.cn",
        "caixin.com",
        "yicai.com",
        "21jingji.com",
        "cls.cn",
    }
)
DISCLOSURE_DOMAINS = frozenset(
    {
        "sec.gov",
        "csrc.gov.cn",
        "sse.com.cn",
        "szse.cn",
        "bse.cn",
        "cninfo.com.cn",
        "hkex.com.hk",
        "hkexnews.hk",
        "nyse.com",
        "nasdaq.com",
        "jpx.co.jp",
        "londonstockexchange.com",
        "asx.com.au",
        "sedarplus.ca",
        "sgx.com",
    }
)
# An explicit additional domain does not override known user-generated sources.
BLOCKED_DOMAINS = frozenset(
    {
        "reddit.com",
        "x.com",
        "twitter.com",
        "facebook.com",
        "youtube.com",
        "tiktok.com",
        "medium.com",
        "substack.com",
        "seekingalpha.com",
        "xueqiu.com",
        "guba.eastmoney.com",
        "tieba.baidu.com",
        "weibo.com",
        "weixin.qq.com",
        "toutiao.com",
        "zhihu.com",
        "sohu.com",
        "baijiahao.baidu.com",
    }
)
_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_CACHE_LOCK = RLock()
_CACHE_LIMIT = 128
_VERSION = 3
_BLOCK_UNTIL = 0.0
_BLOCK_REASON = ""
_BLOCK_SECONDS = 60.0
_REQUEST_LOCK = RLock()
_LAST_REQUEST_STARTED = None
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_ROWS_PER_QUERY = 100
_HAN = re.compile(r"[\u3400-\u9fff]")
_PRIVATE_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".invalid",
    ".example",
    ".home.arpa",
)
_CREDENTIAL_KEYS = {
    "token",
    "access_token",
    "api_key",
    "apikey",
    "password",
    "auth",
    "authorization",
    "credential",
    "signature",
    "x-amz-signature",
}
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CHALLENGE = re.compile(
    r"anomaly\.js|challenge-form|anomaly-modal|g-recaptcha|h-captcha|"
    r"unfortunately, bots use duckduckgo|verify (?:that )?you are (?:a )?human|"
    r"^\s*(?:captcha|access denied|forbidden)\b",
    re.I,
)


class _SearchFailure(Exception):
    """A sanitized transport/protocol failure, with no remote body in its message."""

    def __init__(self, reason, *, stop=False):
        super().__init__(reason)
        self.reason = reason
        self.stop = stop
        self.block_recorded = False


def block_status() -> dict | None:
    """Share a short provider-wide backoff across news and official discovery.

    This is a cooldown only, never permission to retry or bypass a challenge.
    No remote body, credentials, URL, or source content is retained.
    """
    with _CACHE_LOCK:
        remaining = _BLOCK_UNTIL - time.monotonic()
        if remaining <= 0:
            return None
        return {"reason": _BLOCK_REASON, "retry_after_seconds": math.ceil(remaining)}


def record_block(reason: str) -> None:
    """Prevent subsequent analyst/tool calls from immediately repeating a block."""
    global _BLOCK_UNTIL, _BLOCK_REASON
    safe_reason = (
        reason
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,80}", reason)
        else "provider_blocked"
    )
    with _CACHE_LOCK:
        _BLOCK_UNTIL = time.monotonic() + _BLOCK_SECONDS
        _BLOCK_REASON = safe_reason


def _utcnow():
    return datetime.now(UTC)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = html.unescape(re.sub(r"<[^>]*>", " ", value))
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return " ".join(value.split())[:limit]


def _number(config, key, default, low, high, *, integer=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a finite number from {low} to {high}")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a finite number from {low} to {high}") from None
    if not math.isfinite(value) or not low <= value <= high or (integer and not value.is_integer()):
        raise ValueError(
            f"{key} must be a finite {'integer' if integer else 'number'} from {low} to {high}"
        )
    return int(value) if integer else value


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("start_date and end_date must be YYYY-MM-DD dates")
    return date.fromisoformat(value)


def _matches_domain(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _settings(config):
    if not isinstance(config, dict):
        raise ValueError("config must be a dictionary")
    settings = {
        "timeout": _number(config, "duckduckgo_news_timeout", 15, 0.5, 15),
        "total_timeout": _number(config, "duckduckgo_news_total_timeout", 45, 5, 120),
        "min_interval": _number(config, "duckduckgo_news_min_interval", 3, 0, 30),
        "max_queries": _number(config, "duckduckgo_news_max_queries", 2, 1, 8, integer=True),
        "max_results": _number(config, "duckduckgo_news_max_results", 10, 1, 30, integer=True),
        "cache_ttl": _number(config, "duckduckgo_news_cache_ttl", 300, 0, 3600),
    }
    region = config.get("duckduckgo_news_region", "auto")
    if not isinstance(region, str) or not (
        region == "auto" or re.fullmatch(r"[a-z]{2}-[a-z]{2}", region)
    ):
        raise ValueError("duckduckgo_news_region must be auto or a country-language code")
    settings["region"] = region
    domains = config.get("duckduckgo_news_allowed_domains", [])
    if not isinstance(domains, (list, tuple)) or len(domains) > 40:
        raise ValueError("duckduckgo_news_allowed_domains must contain at most 40 trusted domains")
    checked = []
    for domain in domains:
        if not isinstance(domain, str) or not _HOST.fullmatch(domain.lower()):
            raise ValueError("Additional source domains must be plain ASCII hostnames")
        domain = domain.lower()
        if domain in {"co.uk", "com.cn", "gov.cn", "com.au", "co.jp", "co.in", "com.hk"}:
            raise ValueError("Additional source domains must identify a publisher or issuer")
        checked.append(domain)
    settings["allowed_domains"] = tuple(sorted(set(checked)))
    aliases = config.get("duckduckgo_news_aliases", [])
    if not isinstance(aliases, (list, tuple)) or len(aliases) > 20:
        raise ValueError("duckduckgo_news_aliases must contain at most 20 company names")
    checked_aliases = []
    for alias in aliases:
        if not isinstance(alias, str) or len(alias) > 200:
            raise ValueError("Company aliases must be strings of at most 200 characters")
        alias = _clean(alias, 200)
        letters = re.sub(r"[^A-Za-z]", "", alias)
        # Dotted acronyms such as C.E.E.C are ambiguous organization names.
        # Six-digit listing codes and exact canonical ticker tokens are useful
        # only when supplied by the identity-aware caller, never scraped.
        dotted_acronym = bool(re.fullmatch(r"(?:[A-Za-z]\.){2,}[A-Za-z]?\.?", alias))
        if not dotted_acronym and (
            len(_HAN.findall(alias)) >= 2
            or re.fullmatch(r"\d{6}", alias)
            or len(letters) >= 4
            or re.fullmatch(r"[A-Z]{1,3}", alias)
            or re.fullmatch(r"(?:\d{3,6}|[A-Z]{1,8})\.[A-Z]{1,5}", alias)
            or re.fullmatch(r"S&P\s*500", alias, re.I)
        ):
            checked_aliases.append(alias)
    if aliases and not checked_aliases:
        raise ValueError("No strong company-name alias; ticker/acronym matches are insufficient")
    settings["aliases"] = tuple(sorted(set(checked_aliases)))
    return settings


def _public_url(value, extra_domains, *, company_website=None, company_ticker=None, now=None):
    """Screen public article URLs without DNS requests or fetching their contents."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme.lower() not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        if (
            parts.port not in {None, 80, 443}
            or not _HOST.fullmatch(host)
            or host.endswith(_PRIVATE_SUFFIXES)
        ):
            return None
        if _matches_domain(host, BLOCKED_DOMAINS):
            return None
        from tradingagents.extensions.company_website import matches_company_website
        if matches_company_website(value, company_website, company_ticker, now=now):
            category = "issuer_self_published"
        elif _matches_domain(host, DISCLOSURE_DOMAINS):
            category = "exchange_regulator_disclosure"
        elif _matches_domain(host, NEWS_DOMAINS):
            category = "established_news"
        elif _matches_domain(host, extra_domains):
            category = "configured_trusted_domain"
        else:
            return None
        query_fields = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
        if any(k.lower() in _CREDENTIAL_KEYS for k, _ in query_fields):
            return None
        query = urlencode(
            [
                (k, v)
                for k, v in query_fields
                if not k.lower().startswith("utm_")
                and k.lower() not in {"fbclid", "gclid", "msclkid"}
            ]
        )
        url = urlunsplit((parts.scheme.lower(), host, parts.path or "/", query, ""))
        return url, host, category
    except (ValueError, UnicodeError):
        return None


def _issuer_metadata(bundle):
    return {
        "issuer_website_verification": {
            key: deepcopy(bundle.get(key))
            for key in ("company_key", "source", "verified_at", "expires_at")
        },
        "independently_verified": False,
    }


def _published(value):
    """Reject relative/ambiguous timestamps rather than manufacture publication dates."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value <= 0:
                return None
            return datetime.fromtimestamp(value, UTC), "timestamp"
        if not isinstance(value, str):
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), datetime_time(), UTC), "day"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC), "timestamp"
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _relevance(title, content, aliases):
    if not aliases:
        return []
    text = " ".join(re.sub(r"[^\w]+", " ", title + " " + content).casefold().split())
    matched = []
    for alias in aliases:
        normalized = " ".join(re.sub(r"[^\w]+", " ", alias).casefold().split())
        raw_text = title + " " + content
        if re.fullmatch(r"[A-Z]{1,2}", alias):
            # Single-letter/common-word tickers must be explicitly marked.
            # Company names can independently match when supplied by the caller.
            marker = (
                r"(?:\$|\b(?:NYSE|NASDAQ|AMEX|NYSEARCA|NYSEAMERICAN|LSE|TSX|"
                r"ASX|HKEX|SSE|SZSE|BSE|TSE|JPX|NSE):\s*)"
            )
            hit = bool(re.search(marker + re.escape(alias) + r"(?!\w)", raw_text, re.I))
        elif re.fullmatch(r"(?:\d{3,6}|[A-Z]{1,8})\.[A-Z]{1,5}", alias):
            # Qualified foreign symbols need their exact listing suffix, not a bare code.
            hit = bool(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", raw_text, re.I))
        elif re.fullmatch(r"S&P\s*500", alias, re.I):
            hit = bool(re.search(r"\bS\s*&\s*P\s*500\b", raw_text, re.I))
        elif _HAN.search(alias):
            hit = normalized in text
        else:
            hit = bool(re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", text))
        if hit and re.fullmatch(r"[A-Z]{3}", alias):
            # Three-letter words collide readily: require market context or an
            # explicit ticker marker. Shorter tickers use the stricter rule above.
            hit = bool(
                re.search(
                    r"\b(?:shares?|stock|earnings|revenue|dividend|investors?|"
                    r"nasdaq|nyse|lse|tsx|market|equity)\b",
                    text,
                )
            ) or bool(re.search(r"\$" + re.escape(alias) + r"\b", title + " " + content))
        if hit:
            matched.append(alias)
    return matched


def _remaining_budget(session, *, reserve=0.0):
    """Check the shared monotonic deadline, with optional headroom for one read."""
    deadline = getattr(session, "_duckduckgo_news_deadline", None)
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= reserve:
        raise _SearchFailure("budget_exhausted", stop=True)
    return remaining


def _check_provider_ready(session):
    """Never spend pacing time or start another request during a known block."""
    blocked = block_status()
    if blocked:
        failure = _SearchFailure(blocked["reason"], stop=True)
        failure.block_recorded = True
        raise failure
    return _remaining_budget(session)


@contextmanager
def _request_slot(session):
    """Serialize and pace DDG HTTP requests across tool calls in this process.

    Token, news, and official-discovery requests each take a slot. Keep the
    gate until the response is checked so a challenge is recorded before the
    next caller can send. Lock contention and pacing both use the caller's
    existing deadline; neither creates a fresh budget or a retry loop.
    """
    remaining = _check_provider_ready(session)
    acquired = _REQUEST_LOCK.acquire(timeout=remaining) if remaining is not None else _REQUEST_LOCK.acquire()
    if not acquired:
        raise _SearchFailure("budget_exhausted", stop=True)
    try:
        remaining = _check_provider_ready(session)
        interval = getattr(session, "_duckduckgo_news_min_interval", 3.0)
        delay = (
            max(0.0, _LAST_REQUEST_STARTED + interval - time.monotonic())
            if _LAST_REQUEST_STARTED is not None else 0.0
        )
        if delay:
            # Do not sleep to (or beyond) the deadline when no request can fit.
            if remaining is not None and delay >= remaining:
                raise _SearchFailure("budget_exhausted", stop=True)
            time.sleep(delay)
        _check_provider_ready(session)
        try:
            yield
        except _SearchFailure as exc:
            if exc.stop and exc.reason != "budget_exhausted" and not exc.block_recorded:
                record_block(exc.reason)
                exc.block_recorded = True
            raise
    finally:
        _REQUEST_LOCK.release()


def _read_response(session, url, params, timeout):
    with _request_slot(session):
        return _read_response_unpaced(session, url, params, timeout)


def _read_response_unpaced(session, url, params, timeout):
    global _LAST_REQUEST_STARTED
    # Only fixed DuckDuckGo endpoints are ever fetched. Redirects and retries are
    # deliberately disabled, including when an anti-bot response offers a route.
    remaining = _remaining_budget(session)
    # Reserve half the remaining shared budget for a possibly blocking socket
    # read. Requests' total timeout alone does not cap streamed response time.
    request_budget = timeout if remaining is None else min(timeout, remaining / 2)
    read_timeout = request_budget
    request_timeout = Timeout(total=request_budget, connect=request_budget, read=read_timeout)
    started = time.monotonic()
    transport = getattr(session, "_duckduckgo_news_transport", None)
    if transport is None:
        transport = session._duckduckgo_news_transport = []
    observation = {
        "endpoint": url
        if url in {SEARCH_URL, NEWS_URL, "https://html.duckduckgo.com/html/"}
        else "unsupported_endpoint",
        "status": None,
        "content_type": None,
    }
    transport.append(observation)
    response = None
    try:
        _LAST_REQUEST_STARTED = time.monotonic()
        response = session.get(
            url,
            params=params,
            timeout=request_timeout,
            allow_redirects=False,
            stream=True,
        )
        observation["status"] = response.status_code
        headers = getattr(response, "headers", {})
        raw_content_type = headers.get("content-type", "")
        content_type = (
            raw_content_type.partition(";")[0].strip().lower()
            if isinstance(raw_content_type, str)
            else ""
        )
        if re.fullmatch(r"[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,64}", content_type):
            observation["content_type"] = content_type
        if response.status_code in {202, 401, 403, 429}:
            raise _SearchFailure(f"http_{response.status_code}_blocked", stop=True)
        if response.status_code != 200:
            raise _SearchFailure(
                f"http_{response.status_code}", stop=300 <= response.status_code < 400
            )
        _remaining_budget(session)
        body = bytearray()
        # One-byte yields prevent a slow drip from hiding inside a buffered
        # 4096-byte read. Check before advancing, leaving one socket timeout of
        # headroom. No background request survives a return from this function.
        chunks = response.iter_content(chunk_size=1)
        while True:
            _remaining_budget(session, reserve=read_timeout)
            if time.monotonic() - started > timeout:
                raise _SearchFailure("response_timeout")
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            _remaining_budget(session)
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise _SearchFailure("response_too_large")
        text = body.decode("utf-8", errors="replace")
        if _CHALLENGE.search(text):
            # Search snippets can themselves discuss CAPTCHA pages. A valid
            # news envelope is data, not a provider challenge to be acted on.
            payload = None
            if url == NEWS_URL:
                with suppress(ValueError):
                    payload = json.loads(text)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise _SearchFailure("captcha_or_challenge", stop=True)
        return text
    except requests.RequestException:
        _remaining_budget(session, reserve=read_timeout)
        raise
    finally:
        observation["elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 3)
        if response is not None:
            response.close()


def _extract_vqd(landing):
    # Match a complete upstream-delimited value; never send a numeric prefix of
    # an opaque token or accept an unterminated/overlong quoted value.
    token = re.search(
        r"""\bvqd\s*=\s*(?:"([^"\r\n]{1,512})"|'([^'\r\n]{1,512})'|([^&\s"'<>]{1,512})&)""",
        landing,
    )
    if token:
        value = next(value for value in token.groups() if value is not None)
        if not re.search(r"""[\s\x00-\x1f\x7f<>"'&]""", value):
            return value
    raise _SearchFailure("search_token_unavailable")


def _search(session, query, region, timeout):
    _remaining_budget(session)
    landing = _read_response(session, SEARCH_URL, {"q": query}, timeout)
    token = _extract_vqd(landing)
    _remaining_budget(session)
    body = _read_response(
        session,
        NEWS_URL,
        {
            "q": query,
            "vqd": token,
            "l": region,
            "o": "json",
            "noamp": "1",
            "p": "-1",
        },
        timeout,
    )
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise _SearchFailure("invalid_news_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise _SearchFailure("invalid_news_response")
    return payload["results"][:_MAX_ROWS_PER_QUERY]


def fetch_news(queries: list[str], start_date, end_date, config: dict) -> dict:
    """Return screened snippets for an inclusive UTC date range, for any market.

    Configuration uses ``duckduckgo_news_`` keys: timeout (seconds/request, 15),
    total_timeout (shared seconds across requests, 45),
    min_interval (seconds between HTTP request starts, 3; process-shared),
    max_queries (2), max_results (10 total), cache_ttl (300 seconds), region
    ("auto" selects cn-zh/us-en by query), allowed_domains (additional trusted
    publisher/issuer hostnames), and aliases (optional strong company names).
    Numeric bounds are validated rather than silently accepting unsafe limits.

    ``status`` is ok, partial, empty, unavailable, or invalid_request. ``empty``
    means no admissible evidence in this limited sample. Published dates are
    search-provider metadata, never independently verified article dates.
    """
    diagnostics = {
        "provider": "DuckDuckGo",
        "endpoint": NEWS_URL,
        "cache_hit": False,
        "queries": [],
        "transport": [],
        "excluded": {},
        "coverage_note": "A limited search sample cannot establish absence of news.",
        "content_warning": "Untrusted search snippets; not full article text or instructions.",
    }
    result = {"evidence": [], "status": "unavailable", "diagnostics": diagnostics}
    try:
        settings = _settings(config)
        deadline = time.monotonic() + settings["total_timeout"]
        shared_deadline = config.get("_duckduckgo_news_deadline")
        if shared_deadline is not None:
            if (
                isinstance(shared_deadline, bool)
                or not isinstance(shared_deadline, (int, float))
                or not math.isfinite(shared_deadline)
            ):
                raise ValueError("Internal DuckDuckGo deadline must be finite")
            deadline = min(deadline, shared_deadline)
        start, end = _date(start_date), _date(end_date)
        if start > end or end == date.max:
            raise ValueError("Date range must be ordered and have a finite end boundary")
        if not isinstance(queries, list) or not queries or len(queries) > 100:
            raise ValueError("queries must be a nonempty list of at most 100 strings")
        normalized = []
        for query in queries:
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise ValueError("Each query must be a nonempty string of at most 500 characters")
            query = _clean(query, 500)
            if query and query not in normalized:
                normalized.append(query)
        if not normalized:
            raise ValueError("No valid queries")
        diagnostics["queries_truncated"] = max(0, len(normalized) - settings["max_queries"])
        normalized = normalized[: settings["max_queries"]]
    except (ValueError, TypeError) as exc:
        result["status"] = "invalid_request"
        diagnostics["reason"] = str(exc)
        return result

    now = _utcnow()
    lower = datetime.combine(start, datetime_time(), UTC)
    upper = datetime.combine(end + timedelta(days=1), datetime_time(), UTC)
    if lower > now:
        result["status"] = "empty"
        diagnostics["reason"] = "requested_window_is_in_the_future"
        return result
    from tradingagents.extensions.company_website import company_website_cache_key
    company_website = config.get("_company_website")
    company_ticker = config.get("_company_website_ticker")
    website_key = company_website_cache_key(company_website, company_ticker, now=now)
    if website_key is None:
        company_website = None
    cache_settings = tuple(
        (k, v) for k, v in settings.items() if k not in {"total_timeout", "min_interval"}
    )
    key = (_VERSION, tuple(normalized), start.isoformat(), end.isoformat(), cache_settings, website_key)
    ttl = settings["cache_ttl"]
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if ttl and cached and time.monotonic() - cached[0] < ttl:
            _CACHE.move_to_end(key)
            copy = deepcopy(cached[1])
            copy["diagnostics"]["cache_hit"] = True
            return copy
    blocked = block_status()
    if blocked:
        diagnostics.update(
            reason="provider_cooldown",
            stop_reason=blocked["reason"],
            stop_search=True,
            retry_after_seconds=blocked["retry_after_seconds"],
        )
        return result
    diagnostics["limits"] = {
        k: settings[k]
        for k in ("max_queries", "max_results", "timeout", "total_timeout", "min_interval")
    }
    diagnostics["retrieved_at"] = now.isoformat()
    by_url = {}
    by_copy = {}
    successes = failures = 0
    with requests.Session() as session:
        session._duckduckgo_news_deadline = deadline
        session._duckduckgo_news_min_interval = settings["min_interval"]
        session._duckduckgo_news_transport = diagnostics["transport"]
        # requests' default adapter has zero retries. Never add a retry adapter.
        session.headers.update({"User-Agent": "TradingAgents-NewsEvidence/1.0"})
        for query in normalized:
            query_language = "zh" if _HAN.search(query) else "und"
            region = settings["region"]
            if region == "auto":
                region = "cn-zh" if query_language == "zh" else "us-en"
            outcome = {"query": query, "query_language": query_language, "region": region}
            diagnostics["queries"].append(outcome)
            try:
                _remaining_budget(session)
                rows = _search(session, query, region, settings["timeout"])
            except _SearchFailure as exc:
                failures += 1
                outcome.update(status="unavailable", reason=exc.reason)
                if exc.stop:
                    if exc.reason != "budget_exhausted" and not exc.block_recorded:
                        record_block(exc.reason)
                    diagnostics["stop_reason"] = exc.reason
                    diagnostics["stop_search"] = True
                    break
                continue
            except requests.RequestException as exc:
                failures += 1
                outcome.update(
                    status="unavailable",
                    reason="timeout" if isinstance(exc, requests.Timeout) else "network_error",
                )
                continue
            successes += 1
            outcome.update(status="ok", returned=len(rows), accepted=0)
            for row in rows:
                reason = None
                if not isinstance(row, dict):
                    reason = "malformed"
                else:
                    published = _published(row.get("date"))
                    source = _public_url(
                        row.get("url"), settings["allowed_domains"],
                        company_website=company_website, company_ticker=company_ticker,
                        now=_utcnow(),
                    )
                    title = _clean(row.get("title"), 500)
                    content = _clean(row.get("excerpt", row.get("body")), 2000)
                    matched = _relevance(title, content, settings["aliases"])
                    if not published:
                        reason = "undated_or_ambiguous_date"
                    elif published[0] > now:
                        reason = "future"
                    elif not lower <= published[0] < upper:
                        reason = "out_of_window"
                    elif not source:
                        reason = "untrusted_or_invalid_source"
                    elif not title or not content:
                        reason = "missing_title_or_snippet"
                    elif settings["aliases"] and not matched:
                        reason = "company_not_attributed"
                if reason:
                    diagnostics["excluded"][reason] = diagnostics["excluded"].get(reason, 0) + 1
                    continue
                url, host, category = source
                # Exact same-day normalized headline AND snippet is a
                # conservative syndicated-copy signal. A shared headline alone
                # never collapses independently written reporting.
                fingerprint = (
                    published[0].date().isoformat(),
                    re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", title).casefold()).strip(),
                    re.sub(
                        r"[^\w]+", " ", unicodedata.normalize("NFKC", content).casefold()
                    ).strip(),
                )
                previous = by_url.get(url) or by_copy.get(fingerprint)
                if previous is not None:
                    if query not in previous["queries"]:
                        previous["queries"].append(query)
                    if query_language not in previous["query_languages"]:
                        previous["query_languages"].append(query_language)
                    duplicate_kind = (
                        "duplicate" if url == previous["url"] else "duplicate_syndicated"
                    )
                    diagnostics["excluded"][duplicate_kind] = (
                        diagnostics["excluded"].get(duplicate_kind, 0) + 1
                    )
                    if url != previous["url"]:
                        alternatives = previous.setdefault("alternate_sources", [])
                        if not any(item["url"] == url for item in alternatives):
                            alternatives.append(
                                {
                                    "url": url,
                                    "publisher": _clean(row.get("source"), 200) or host,
                                    "source_domain": host,
                                    "source_category": category,
                                    "published_at": published[0].isoformat(),
                                    **(_issuer_metadata(company_website)
                                       if category == "issuer_self_published" else {}),
                                }
                            )
                    continue
                evidence = {
                    "title": title,
                    "url": url,
                    "published_at": published[0].isoformat(),
                    "date_precision": published[1],
                    "date_source": "search_provider_metadata",
                    "publisher": _clean(row.get("source"), 200) or host,
                    "publisher_label_source": "search_provider_metadata"
                    if _clean(row.get("source"), 200)
                    else "url_hostname",
                    "source_domain": host,
                    "source_category": category,
                    "content": content,
                    "content_kind": "snippet",
                    "retrieval_provider": "DuckDuckGo",
                    "language": "zh" if _HAN.search(title + content) else "und",
                    "language_basis": "script_heuristic",
                    "query": query,
                    "queries": [query],
                    "query_language": query_language,
                    "query_languages": [query_language],
                    "matched_aliases": matched,
                    "retrieved_at": now.isoformat(),
                }
                if category == "issuer_self_published":
                    evidence.update(_issuer_metadata(company_website))
                by_url[url] = evidence
                by_copy[fingerprint] = evidence
                outcome["accepted"] += 1
                if len(by_url) >= settings["max_results"]:
                    diagnostics["result_limit_reached"] = True
                    break
            if len(by_url) >= settings["max_results"]:
                break
    result["evidence"] = sorted(
        by_url.values(), key=lambda item: item["published_at"], reverse=True
    )
    result["status"] = (
        ("partial" if failures else "ok")
        if by_url
        else ("empty" if successes and not failures else "unavailable")
    )
    if not by_url:
        diagnostics["reason"] = (
            "budget_exhausted"
            if diagnostics.get("stop_reason") == "budget_exhausted"
            else "no_admissible_evidence"
            if successes
            else "search_unavailable"
        )
    # Cache only complete successful observations; no stale/failed evidence on errors.
    if ttl and not failures:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), deepcopy(result))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return result
