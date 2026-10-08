"""Short-lived, company-bound website scopes from official issuer metadata.

Search results, supplied aliases, general allowlists and third-party company
profiles cannot create these scopes. Only fixed exchange/regulator JSON
endpoints are requested; the discovered websites are never fetched. A scope
screens a self-published source, not the truth of its claims or historical
ownership. Unknown fields, identity changes and failed refreshes fail closed.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import requests
import tldextract
from urllib3.util import Timeout

from tradingagents.dataflows.symbols import normalize_symbol

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/index/companyGeneralization"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_VERSION = 1
_MAX_TTL = 604800
_MAX_RESPONSE_BYTES = 4_194_304
_CACHE_LIMIT = 256
_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_LOCK = RLock()
# No network refresh, disk cache, or dependency on a writable user home.
_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOCAL_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "test",
    "invalid",
    "example",
    "home.arpa",
    "onion",
    "localdomain",
    "lan",
    "home",
    "nip.io",
    "sslip.io",
    "localtest.me",
    "example.com",
    "example.net",
    "example.org",
)
_SHARED_IR = (
    "q4web.com",
    "q4inc.com",
    "q4ir.com",
    "gcs-web.com",
    "investorroom.com",
    "corporate-ir.net",
    "investis.com",
    "irwebpage.com",
)
_QUERY_KEYS = {
    "url",
    "u",
    "target",
    "redirect",
    "redirect_uri",
    "redirect_to",
    "return_to",
    "continue",
    "redirect_url",
    "return",
    "returnto",
    "next",
    "destination",
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


class _Unavailable(ValueError):
    """Sanitized failures only; never carry remote bodies or query values."""


def _utcnow():
    return datetime.now(UTC)


def _now(value=None):
    value = _utcnow() if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _number(config, key, default, low, high):
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a finite number from {low} to {high}")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a finite number from {low} to {high}") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be a finite number from {low} to {high}")
    return value


def _timeout(config, limit):
    deadline = config.get("_duckduckgo_news_deadline")
    if deadline is not None:
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise _Unavailable("metadata_budget_exhausted")
        limit = min(limit, deadline - time.monotonic())
    if limit <= 0:
        raise _Unavailable("metadata_budget_exhausted")
    return limit


def _get_json(url, params, referer, timeout, user_agent=None):
    """The sole fetch seam: fixed HTTPS metadata only, no redirects or retries."""
    if url not in {SSE_URL, SZSE_URL, SEC_TICKERS_URL} and not re.fullmatch(
        r"https://data\.sec\.gov/submissions/CIK\d{10}\.json", url
    ):
        raise _Unavailable("unapproved_metadata_endpoint")
    headers = {"Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    if user_agent:
        headers["User-Agent"] = user_agent
    started = time.monotonic()
    # Preserve headroom for one blocking socket read; a requests total timeout
    # alone does not bound streaming a slow-drip response body.
    read_timeout = timeout / 2
    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=Timeout(total=read_timeout, connect=read_timeout, read=read_timeout),
        allow_redirects=False,
        stream=True,
    )
    try:
        if response.status_code != 200 or response.history:
            raise _Unavailable("official_metadata_http_failure")
        wanted, actual = urlsplit(url), urlsplit(response.url)
        if (wanted.scheme, wanted.netloc, wanted.path) != (
            actual.scheme,
            actual.netloc,
            actual.path,
        ):
            raise _Unavailable("official_metadata_endpoint_mismatch")
        body = bytearray()
        chunks = response.iter_content(chunk_size=1)
        while True:
            if time.monotonic() - started >= timeout - read_timeout:
                raise _Unavailable("official_metadata_response_limit")
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES or time.monotonic() - started >= timeout:
                raise _Unavailable("official_metadata_response_limit")
        return json.loads(body)
    finally:
        response.close()


def _name(value):
    if not isinstance(value, str):
        return None
    value = " ".join(html.unescape(re.sub(r"<[^>]*>", "", value)).split())
    return value if value and len(value) <= 300 and value not in {"-", "null"} else None


def _validated_url(value, *, metadata=False):
    """ASCII host and unambiguous path; never fetch, resolve DNS, or widen scope."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    if metadata and "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not parsed.netloc.isascii()
            or not _HOST.fullmatch(host)
            or any(label.startswith("xn--") for label in host.split("."))
        ):
            return None
        if any(host == suffix or host.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        domain = _PSL(host)
        if not domain.suffix or not domain.domain:
            return None
        # A public or private suffix is never a company, including www.<suffix>.
        if domain.domain == "www" and host == "www." + domain.suffix:
            return None
        path = parsed.path or "/"
        if not path.startswith("/") or "//" in path or ";" in path:
            return None
        # Reject encoded separators, dot traversal and nested encodings rather
        # than guessing how a downstream web server normalizes them.
        decoded = unquote(path, errors="strict")
        if (
            "%" in decoded
            or "\\" in decoded
            or any(c in decoded for c in ";?#")
            or decoded.count("/") != path.count("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(segment in {".", ".."} for segment in decoded.split("/"))
        ):
            return None
        if metadata and (parsed.query or parsed.fragment):
            return None
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            if (
                key.lower() in _QUERY_KEYS
                or "://" in unquote(val)
                or unquote(val).strip().startswith("//")
                or "%" in val
                or any(ord(c) < 32 for c in val)
            ):
                return None
        return {
            "url": urlunsplit((parsed.scheme, host, path, "", "")),
            "host": host,
            "path_prefix": path.rstrip("/") or "/",
            "registered_domain": domain.top_domain_under_public_suffix,
        }
    except (ValueError, UnicodeError):
        return None


def _scopes(websites):
    """Keep every provider path boundary; third-party IR never gets host trust."""
    parsed = []
    for value, kind in websites:
        if value in (None, "", "-"):
            continue
        item = _validated_url(value, metadata=True)
        if not item:
            raise _Unavailable("unsafe_official_website")
        item["kind"] = kind
        parsed.append(item)
    company_domains = {p["registered_domain"] for p in parsed if p["kind"] == "company_website"}
    result = []
    for item in parsed:
        shared = any(item["host"] == d or item["host"].endswith("." + d) for d in _SHARED_IR)
        third_party = (
            item["kind"] == "investor_relations"
            and item["registered_domain"] not in company_domains
        )
        if item["path_prefix"] == "/" and (shared or third_party):
            raise _Unavailable("third_party_ir_requires_path")
        scope = {key: item[key] for key in ("url", "host", "path_prefix", "kind")}
        if scope not in result:
            result.append(scope)
    return result


def _pinned_identity(ticker, config):
    identity = config.get("_ashare_identity")
    if identity is None:
        return None
    if not isinstance(identity, dict) or identity.get("canonical_symbol") != ticker:
        raise _Unavailable("pinned_identity_mismatch")
    if (
        identity.get("status") != "resolved"
        or identity.get("security_type") != "A-share"
        or identity.get("confidence") != "verified"
    ):
        raise _Unavailable("pinned_identity_unverified")
    return identity


def _check_names(identity, full_name, short_name):
    if not identity:
        return
    for key, actual in (("chinese_full_name", full_name), ("chinese_short_name", short_name)):
        pinned = _name(identity.get(key))
        if pinned and pinned != _name(actual):
            raise _Unavailable("pinned_identity_changed")


def _szse(ticker, config, timeout):
    # Official page uses data.http, labelled cols.http == 公司网址:
    # https://www.szse.cn/certificate/individual/index.html?code=000001
    code = ticker[:6]
    identity = _pinned_identity(ticker, config)
    if identity and identity.get("exchange") != "SZSE":
        raise _Unavailable("pinned_identity_mismatch")
    payload = _get_json(
        SZSE_URL, {"secCode": code}, "https://www.szse.cn/", _timeout(config, timeout)
    )
    if (
        not isinstance(payload, dict)
        or payload.get("code") != "0"
        or payload.get("plate") not in {"XA", "CY"}
        or not isinstance(payload.get("data"), dict)
        or not isinstance(payload.get("cols"), dict)
        or payload["cols"].get("agdm") != "A股代码"
        or payload["cols"].get("http") != "公司网址"
        or payload["data"].get("agdm") != code
    ):
        raise _Unavailable("official_identity_mismatch")
    row = payload["data"]
    _check_names(identity, row.get("gsqc"), row.get("agjc"))
    if not _name(row.get("gsqc")) or not _name(row.get("agjc")):
        raise _Unavailable("official_identity_mismatch")
    scopes = _scopes([(row.get("http"), "company_website")])
    return f"SZSE:{code}", scopes, {"provider": "SZSE", "url": SZSE_URL, "identity": ticker}


def _sec(ticker, config, timeout):
    from tradingagents.dataflows.vendors.sec_edgar import _user_agent

    user_agent = _user_agent()
    table = _get_json(SEC_TICKERS_URL, None, None, _timeout(config, timeout), user_agent)
    if not isinstance(table, dict):
        raise _Unavailable("official_metadata_schema_mismatch")
    rows = [r for r in table.values() if isinstance(r, dict) and r.get("ticker") == ticker]
    if len(rows) != 1:
        raise _Unavailable("official_identity_not_unique")
    cik = rows[0].get("cik_str")
    if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik < 10**10:
        raise _Unavailable("official_metadata_schema_mismatch")
    cik = str(cik).zfill(10)
    endpoint = SEC_SUBMISSIONS_URL.format(cik=cik)
    payload = _get_json(endpoint, None, None, _timeout(config, timeout), user_agent)
    if (
        not isinstance(payload, dict)
        or payload.get("cik") != cik
        or payload.get("entityType") != "operating"
        or not isinstance(payload.get("tickers"), list)
        or payload["tickers"].count(ticker) != 1
        or not isinstance(payload.get("exchanges"), list)
        or len(payload["exchanges"]) != len(payload["tickers"])
        or payload["exchanges"][payload["tickers"].index(ticker)] not in {"Nasdaq", "NYSE", "CBOE"}
        or not _name(payload.get("name"))
    ):
        raise _Unavailable("official_identity_mismatch")
    scopes = _scopes(
        [
            (payload.get("website"), "company_website"),
            (payload.get("investorWebsite"), "investor_relations"),
        ]
    )
    return (
        f"SEC:{cik}",
        scopes,
        {
            "provider": "SEC",
            "url": endpoint,
            "identity": ticker,
            "identity_source": SEC_TICKERS_URL,
        },
    )


def _empty(ticker, status, reason, *, company_key=None, source=None):
    return {
        "adapter_version": _VERSION,
        "status": status,
        "canonical_symbol": ticker,
        "company_key": company_key,
        "scopes": [],
        "verified_at": None,
        "expires_at": None,
        "source": source,
        "reason": reason,
    }


def resolve_company_website(ticker: str, config: dict, *, now=None) -> dict:
    """Resolve one current company without mutating config or reusing old trust.

    ``now`` is an aware datetime for deterministic testing. TTL=0 disables cache
    storage/reuse; newly checked evidence gets a 120-second consumption lifetime.
    Failure, changed identity, missing fields, and unsupported markets have no
    trusted scopes. Existing bundles in config are deliberately never consumed.
    """
    if not isinstance(config, dict):
        raise ValueError("config must be a dictionary")
    ticker = normalize_symbol(ticker)
    enabled = config.get("company_website_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("company_website_enabled must be true or false")
    if not enabled:
        return _empty(ticker, "disabled", "company_website_disabled")
    timeout = _number(config, "company_website_timeout", 5, 0.1, 15)
    ttl = _number(config, "company_website_cache_ttl", 86400, 0, _MAX_TTL)
    current = _now(now)
    if re.fullmatch(r"\d{6}\.SS", ticker):
        # Its current official profile schema has no website field, so do not
        # spend the search budget duplicating the existing identity request.
        return _empty(
            ticker,
            "unsupported",
            "official_website_field_unavailable",
            company_key=f"SSE:{ticker[:6]}",
            source={"provider": "SSE", "url": SSE_URL, "identity": ticker},
        )
    elif re.fullmatch(r"\d{6}\.SZ", ticker):
        provider = _szse
    elif re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:-[A-Z])?", ticker):
        provider = _sec
    else:
        return _empty(ticker, "unsupported", "no_supported_official_website_metadata")
    identity = config.get("_ashare_identity")
    if ticker.endswith((".SS", ".SZ")):
        try:
            _pinned_identity(ticker, config)
        except _Unavailable as exc:
            return _empty(ticker, "unavailable", str(exc))
    # Include pinned identity in cache identity: changed names cannot reuse a
    # previous run's verification, and observation timestamps do not extend it.
    identity_key = (
        json.dumps(
            {
                k: identity.get(k)
                for k in (
                    "canonical_symbol",
                    "exchange",
                    "status",
                    "confidence",
                    "security_type",
                    "chinese_full_name",
                    "chinese_short_name",
                )
            },
            sort_keys=True,
        )
        if isinstance(identity, dict)
        else None
    )
    key = (_VERSION, ticker, identity_key)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and ttl > 0 and company_website_cache_key(cached, ticker, now=current):
            age = (current - datetime.fromisoformat(cached["verified_at"])).total_seconds()
            if age < ttl:
                _CACHE.move_to_end(key)
                result = deepcopy(cached)
                policy_expiry = datetime.fromisoformat(result["verified_at"]) + timedelta(
                    seconds=ttl
                )
                result["expires_at"] = min(
                    datetime.fromisoformat(result["expires_at"]), policy_expiry
                ).isoformat()
                return result
        # An expired entry is removed before refresh, including failed refresh.
        for previous in [old for old in _CACHE if old[1] == ticker]:
            _CACHE.pop(previous, None)
    try:
        company_key, scopes, source = provider(ticker, config, timeout)
    except _Unavailable as exc:
        return _empty(ticker, "unavailable", str(exc))
    except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError):
        return _empty(ticker, "unavailable", "official_metadata_unavailable")
    if not scopes:
        return _empty(
            ticker,
            "unavailable",
            "official_website_field_unavailable",
            company_key=company_key,
            source=source,
        )
    current = _now(now)  # Successful verification time, not request start.
    result = {
        "adapter_version": _VERSION,
        "status": "resolved",
        "canonical_symbol": ticker,
        "company_key": company_key,
        "scopes": scopes,
        "verified_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=ttl if ttl else 120)).isoformat(),
        "source": source,
        "reason": None,
    }
    if ttl:
        with _CACHE_LOCK:
            _CACHE[key] = deepcopy(result)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return result


def company_website_cache_key(bundle, ticker: str, *, now=None) -> str | None:
    """Validate company/TTL/provenance/scope before using a news cache entry."""
    if not isinstance(bundle, dict):
        return None
    ticker = normalize_symbol(ticker)
    if (
        bundle.get("adapter_version") != _VERSION
        or bundle.get("status") != "resolved"
        or bundle.get("canonical_symbol") != ticker
    ):
        return None
    try:
        verified, expires = (
            datetime.fromisoformat(bundle[key]) for key in ("verified_at", "expires_at")
        )
        current = _now(now)
        if (
            verified.tzinfo is None
            or expires.tzinfo is None
            or not verified <= current < expires
            or not 0 < (expires - verified).total_seconds() <= _MAX_TTL
        ):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    source = bundle.get("source")
    if not isinstance(source, dict) or source.get("identity") != ticker:
        return None
    company_key = bundle.get("company_key")
    if source.get("provider") == "SSE":
        if (
            not re.fullmatch(r"\d{6}\.SS", ticker)
            or company_key != f"SSE:{ticker[:6]}"
            or source.get("url") != SSE_URL
        ):
            return None
    elif source.get("provider") == "SZSE":
        if (
            not re.fullmatch(r"\d{6}\.SZ", ticker)
            or company_key != f"SZSE:{ticker[:6]}"
            or source.get("url") != SZSE_URL
        ):
            return None
    elif source.get("provider") == "SEC":
        if (
            not isinstance(company_key, str)
            or not re.fullmatch(r"SEC:\d{10}", company_key)
            or source.get("url") != SEC_SUBMISSIONS_URL.format(cik=company_key[4:])
            or source.get("identity_source") != SEC_TICKERS_URL
        ):
            return None
    else:
        return None
    scopes = bundle.get("scopes")
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 4:
        return None
    for scope in scopes:
        if not isinstance(scope, dict) or scope.get("kind") not in {
            "company_website",
            "investor_relations",
        }:
            return None
        checked = _validated_url(scope.get("url"), metadata=True)
        if not checked or any(checked[key] != scope.get(key) for key in ("host", "path_prefix")):
            return None
    try:
        if _scopes([(s["url"], s["kind"]) for s in scopes]) != scopes:
            return None
        material = {
            k: bundle[k]
            for k in (
                "adapter_version",
                "canonical_symbol",
                "company_key",
                "scopes",
                "verified_at",
                "expires_at",
                "source",
            )
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (ValueError, TypeError, KeyError):
        return None


def matches_company_website(url: str, bundle, ticker: str, *, now=None) -> bool:
    """Match exact host and path segment boundary; no inferred sibling trust."""
    if not company_website_cache_key(bundle, ticker, now=now):
        return False
    candidate = _validated_url(url)
    if not candidate:
        return False
    path = candidate["path_prefix"]
    return any(
        candidate["host"] == scope["host"]
        and (
            scope["path_prefix"] == "/"
            or path == scope["path_prefix"]
            or path.startswith(scope["path_prefix"] + "/")
        )
        for scope in bundle["scopes"]
    )
