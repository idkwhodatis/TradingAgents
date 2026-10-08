"""Official website trust is isolated, temporary, and never learned from search."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
import requests

from tradingagents.extensions import company_website as websites

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clear_cache():
    websites._CACHE.clear()
    yield
    websites._CACHE.clear()


def szse_payload(website="bank.pingan.com"):
    # Minimal projection of the official companyGeneralization JSON schema,
    # checked 2026-10-08; no fabricated website field on the A-share list API.
    return {
        "code": "0",
        "plate": "XA",
        "data": {
            "agdm": "000001",
            "agjc": "平安银行",
            "gsqc": "平安银行股份有限公司",
            "http": website,
        },
        "cols": {"agdm": "A股代码", "http": "公司网址"},
    }


def sec_payload(website="https://www.apple.com/", investor="https://investor.apple.com/"):
    # Website fields exist in real SEC submissions; empty is common and must
    # remain unknown. Populated values here are explicitly synthetic tests.
    return {
        "cik": "0000320193",
        "entityType": "operating",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "website": website,
        "investorWebsite": investor,
    }


@pytest.fixture
def official(monkeypatch):
    calls = []
    data = {
        "szse": szse_payload(),
        "sec": sec_payload(),
        "tickers": {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}},
    }

    def get(url, params, referer, timeout, user_agent=None):
        calls.append((url, deepcopy(params), referer, timeout, user_agent))
        if url == websites.SZSE_URL:
            return deepcopy(data["szse"])
        if url == websites.SEC_TICKERS_URL:
            return deepcopy(data["tickers"])
        if url == websites.SEC_SUBMISSIONS_URL.format(cik="0000320193"):
            return deepcopy(data["sec"])
        pytest.fail(f"Unexpected metadata endpoint: {url}")

    monkeypatch.setattr(websites, "_get_json", get)
    return data, calls


def test_szse_official_website_company_bound_provenance(official):
    _, calls = official
    config = {"duckduckgo_news_allowed_domains": ["untrusted.com"]}
    original = deepcopy(config)
    bundle = websites.resolve_company_website("000001.SZ", config, now=NOW)
    assert config == original
    assert bundle["status"] == "resolved"
    assert bundle["company_key"] == "SZSE:000001"
    assert bundle["source"] == {
        "provider": "SZSE",
        "url": websites.SZSE_URL,
        "identity": "000001.SZ",
    }
    assert bundle["verified_at"] == NOW.isoformat()
    assert bundle["expires_at"] == (NOW + timedelta(days=1)).isoformat()
    assert bundle["scopes"] == [
        {
            "url": "https://bank.pingan.com/",
            "host": "bank.pingan.com",
            "path_prefix": "/",
            "kind": "company_website",
        }
    ]
    assert calls[0][:2] == (websites.SZSE_URL, {"secCode": "000001"})
    assert websites.matches_company_website(
        "https://bank.pingan.com/news/one", bundle, "000001.SZ", now=NOW
    )
    assert not websites.matches_company_website(
        "https://www.pingan.com/news/one", bundle, "000001.SZ", now=NOW
    )
    assert not websites.matches_company_website(
        "https://bank.pingan.com/news/one", bundle, "000002.SZ", now=NOW
    )


def test_sec_exact_cik_ticker_operating_entity(official):
    _, calls = official
    bundle = websites.resolve_company_website("AAPL", {}, now=NOW)
    assert bundle["status"] == "resolved"
    assert bundle["company_key"] == "SEC:0000320193"
    assert len(bundle["scopes"]) == 2
    assert bundle["source"]["identity_source"] == websites.SEC_TICKERS_URL
    assert len(calls) == 2
    assert all(call[4] for call in calls)
    assert websites.matches_company_website(
        "https://investor.apple.com/news", bundle, "AAPL", now=NOW
    )
    assert not websites.matches_company_website("https://apple.com/news", bundle, "AAPL", now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cik", "0000789019"),
        ("cik", 320193),
        ("tickers", ["MSFT"]),
        ("tickers", ["AAPL", "AAPL"]),
        ("entityType", "investment"),
        ("exchanges", ["OTC"]),
        ("exchanges", []),
        ("name", ""),
    ],
)
def test_sec_wrong_identity_type_and_ambiguous_schema_rejected(official, field, value):
    data, _ = official
    data["sec"][field] = value
    assert websites.resolve_company_website("AAPL", {}, now=NOW)["scopes"] == []


@pytest.mark.parametrize("cik", [True, "320193", 0, -1, 10**10])
def test_sec_unvalidated_cik_never_interpolated_into_endpoint(official, cik):
    data, calls = official
    data["tickers"]["0"]["cik_str"] = cik
    assert websites.resolve_company_website("AAPL", {}, now=NOW)["scopes"] == []
    assert len(calls) == 1


def test_sec_ambiguous_ticker_mapping_does_not_choose_first(official):
    data, calls = official
    data["tickers"]["1"] = {"ticker": "AAPL", "cik_str": 789019}
    assert websites.resolve_company_website("AAPL", {}, now=NOW)["scopes"] == []
    assert len(calls) == 1


@pytest.mark.parametrize(
    "update",
    [
        {"code": 0},
        {"plate": "XB"},
        {"data": {"agdm": "000002"}},
        {"data": {"agdm": "1"}},
        {"cols": {"agdm": "B股代码", "http": "公司网址"}},
    ],
)
def test_szse_exact_equity_identity_required(official, update):
    data, _ = official
    data["szse"].update(update)
    assert websites.resolve_company_website("000001.SZ", {}, now=NOW)["scopes"] == []


def test_current_sse_schema_does_not_invent_website_from_email(monkeypatch):
    monkeypatch.setattr(
        websites,
        "_get_json",
        lambda *a: {
            "result": [
                {
                    "COMPANY_CODE": "601868",
                    "A_STOCK_CODE": "601868",
                    "SEC_TYPE": "主板A",
                    "FULL_NAME": "中国能源建设股份有限公司",
                    "SECURITY_ABBR_A_CN": "中国能建",
                    "E_MAIL_ADDRESS": "test@ceec.net.cn",
                    "website": "https://search-injected.com",
                    "COMPANY_URL": "https://also-injected.com",
                }
            ]
        },
    )
    bundle = websites.resolve_company_website("601868.SH", {}, now=NOW)
    assert bundle["company_key"] == "SSE:601868"
    assert bundle["scopes"] == []
    assert bundle["reason"] == "official_website_field_unavailable"


@pytest.mark.parametrize(
    "symbol", ["920001.BJ", "830799.BJ", "600519", "0700.HK", "BTC-USD", "^GSPC", "BRK.B"]
)
def test_unsupported_never_fetches_or_uses_injected_trust(monkeypatch, symbol):
    monkeypatch.setattr(
        websites, "_get_json", lambda *a, **k: pytest.fail("Unsupported network request")
    )
    bundle = websites.resolve_company_website(
        symbol, {"_company_website": {"scopes": [{"url": "https://evil.com"}]}}, now=NOW
    )
    assert bundle["status"] == "unsupported"
    assert not bundle["scopes"]


def test_disabled_never_fetches(monkeypatch):
    monkeypatch.setattr(
        websites, "_get_json", lambda *a, **k: pytest.fail("Disabled network request")
    )
    assert (
        websites.resolve_company_website("AAPL", {"company_website_enabled": False})["status"]
        == "disabled"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://localhost/",
        "http://foo.local/",
        "http://foo.internal/",
        "http://127.0.0.1.nip.io/",
        "https://com/",
        "https://co.uk/",
        "https://com.cn/",
        "https://github.io/",
        "https://blogspot.com/",
        "https://www.co.uk/",
        "https://xn--pple-43d.com/",
        "https://аpple.com/",
        "https://evil.invalid/",
        "https://bank.pingan.com.evil.test/",
        "ftp://bank.pingan.com/",
        "https://user:secret@bank.pingan.com/",
        "https://bank.pingan.com:443/",
        "https://bank.pingan.com./",
        "https://bank.pingan.com\\@evil.com/",
        " https://bank.pingan.com/",
        "https://bank.pingan.com/\n",
        "https://bank.pingan.com/a/../b",
        "https://bank.pingan.com/a/%2e%2e/b",
        "https://bank.pingan.com/a%2fb",
        "https://bank.pingan.com/a%252fb",
        "https://bank.pingan.com/a;b",
        "https://bank.pingan.com//evil",
        "https://bank.pingan.com/?token=secret",
        "https://bank.pingan.com/?redirect=https%3A%2F%2Fevil.com",
        "https://bank.pingan.com/#other",
    ],
)
def test_unsafe_metadata_urls_fail_closed(official, url):
    data, _ = official
    data["szse"]["data"]["http"] = url
    result = websites.resolve_company_website("000001.SZ", {}, now=NOW)
    assert result["scopes"] == []
    assert result["reason"] == "unsafe_official_website"


def test_exact_host_rejects_lookalikes_and_arbitrary_subdomains(official):
    bundle = websites.resolve_company_website("AAPL", {}, now=NOW)
    for host in [
        "www.apple.com.evil.com",
        "evilapple.com",
        "apple.com",
        "news.www.apple.com",
        "www.app1e.com",
    ]:
        assert not websites.matches_company_website(f"https://{host}/news", bundle, "AAPL", now=NOW)


def test_third_party_ir_preserves_path_segment_boundary(official):
    data, _ = official
    data["sec"]["investorWebsite"] = "https://ir.examplecorp.com/companies/apple/"
    bundle = websites.resolve_company_website("AAPL", {}, now=NOW)
    assert bundle["status"] == "resolved"
    for path in ["/companies/apple", "/companies/apple/", "/companies/apple/news?id=42"]:
        assert websites.matches_company_website(
            "https://ir.examplecorp.com" + path, bundle, "AAPL", now=NOW
        )
    for path in [
        "/",
        "/companies/other",
        "/companies/apple-inc",
        "/companies/apple/../other",
        "/companies/apple%2f../other",
    ]:
        assert not websites.matches_company_website(
            "https://ir.examplecorp.com" + path, bundle, "AAPL", now=NOW
        )
    data["sec"]["investorWebsite"] = "https://ir.examplecorp.com/"
    assert (
        websites.resolve_company_website("AAPL", {"company_website_cache_ttl": 0}, now=NOW)[
            "scopes"
        ]
        == []
    )


def test_shared_ir_company_field_still_requires_path(official):
    data, _ = official
    data["sec"] = sec_payload("https://investorroom.com/", "")
    assert (
        websites.resolve_company_website("AAPL", {}, now=NOW)["reason"]
        == "third_party_ir_requires_path"
    )


def test_cache_reuses_copy_then_refresh_replaces_changed_domain(official):
    data, calls = official
    first = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 10}, now=NOW
    )
    first["scopes"].clear()
    second = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 10}, now=NOW + timedelta(seconds=1)
    )
    assert second["scopes"]
    assert len(calls) == 1
    data["szse"]["data"]["http"] = "https://new.pingan.com/"
    third = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 10}, now=NOW + timedelta(seconds=10)
    )
    assert len(calls) == 2
    assert not websites.matches_company_website(
        "https://bank.pingan.com/a", third, "000001.SZ", now=NOW + timedelta(seconds=10)
    )
    assert websites.matches_company_website(
        "https://new.pingan.com/a", third, "000001.SZ", now=NOW + timedelta(seconds=10)
    )
    assert (
        websites.company_website_cache_key(second, "000001.SZ", now=NOW + timedelta(seconds=10))
        is None
    )


def test_failed_refresh_never_uses_stale_cache_or_leaks_error(official, monkeypatch):
    old = websites.resolve_company_website("000001.SZ", {"company_website_cache_ttl": 1}, now=NOW)
    monkeypatch.setattr(
        websites, "_get_json", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("SECRET"))
    )
    result = websites.resolve_company_website("000001.SZ", {}, now=NOW + timedelta(seconds=2))
    assert result["scopes"] == []
    assert "SECRET" not in json.dumps(result)
    assert not websites._CACHE
    assert not websites.company_website_cache_key(old, "000001.SZ", now=NOW + timedelta(seconds=2))


def test_smaller_config_ttl_cannot_extend_cached_evidence(official):
    _, calls = official
    websites.resolve_company_website("000001.SZ", {}, now=NOW)
    websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 1}, now=NOW + timedelta(seconds=2)
    )
    assert len(calls) == 2


def test_pinned_identity_mismatch_and_changed_name_are_closed(official):
    identity = {
        "canonical_symbol": "000001.SZ",
        "status": "resolved",
        "security_type": "A-share",
        "confidence": "verified",
        "exchange": "SZSE",
        "chinese_short_name": "平安银行",
    }
    config = {"_ashare_identity": identity}
    assert websites.resolve_company_website("000001.SZ", config, now=NOW)["status"] == "resolved"
    identity["chinese_short_name"] = "其他银行"
    assert (
        websites.resolve_company_website("000001.SZ", config, now=NOW)["reason"]
        == "pinned_identity_changed"
    )
    identity["canonical_symbol"] = "000002.SZ"
    assert (
        websites.resolve_company_website("000001.SZ", config, now=NOW)["reason"]
        == "pinned_identity_mismatch"
    )


def test_deadline_exhaustion_stops_metadata_before_io(monkeypatch):
    monkeypatch.setattr(websites, "_get_json", lambda *a, **k: pytest.fail("Past deadline fetched"))
    monkeypatch.setattr(websites.time, "monotonic", lambda: 10)
    result = websites.resolve_company_website(
        "000001.SZ", {"_duckduckgo_news_deadline": 9}, now=NOW
    )
    assert result["reason"] == "metadata_budget_exhausted"


def test_deadline_clamps_every_request(official, monkeypatch):
    _, calls = official
    monkeypatch.setattr(websites.time, "monotonic", lambda: 10)
    websites.resolve_company_website("AAPL", {"_duckduckgo_news_deadline": 10.5}, now=NOW)
    assert [c[3] for c in calls] == [0.5, 0.5]


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_website_enabled", "true"),
        ("company_website_timeout", 0),
        ("company_website_timeout", 16),
        ("company_website_cache_ttl", -1),
        ("company_website_cache_ttl", float("nan")),
        ("company_website_cache_ttl", 604801),
    ],
)
def test_invalid_settings_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        websites.resolve_company_website("000001.SZ", {field: value})


def test_bundle_tamper_and_naive_or_future_dates_are_rejected(official):
    bundle = websites.resolve_company_website("000001.SZ", {}, now=NOW)
    for key, value in [
        ("company_key", "SZSE:000002"),
        ("canonical_symbol", "000002.SZ"),
        ("adapter_version", 99),
        ("verified_at", "2026-10-08T12:00:00"),
        ("verified_at", (NOW + timedelta(seconds=1)).isoformat()),
        ("expires_at", (NOW + timedelta(days=8)).isoformat()),
        ("source", {"provider": "Eastmoney", "url": websites.SZSE_URL, "identity": "000001.SZ"}),
    ]:
        changed = deepcopy(bundle)
        changed[key] = value
        assert websites.company_website_cache_key(changed, "000001.SZ", now=NOW) is None
    bundle["scopes"][0]["host"] = "evil.com"
    assert websites.company_website_cache_key(bundle, "000001.SZ", now=NOW) is None


def test_transport_does_not_follow_redirects_fetch_issuer_or_retry(monkeypatch):
    calls = []

    class Response:
        status_code = 302
        history = []
        url = websites.SZSE_URL

        def close(self):
            pass

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(websites.requests, "get", get)
    result = websites.resolve_company_website("000001.SZ", {}, now=NOW)
    assert result["scopes"] == []
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True
    with pytest.raises(ValueError, match="unapproved_metadata_endpoint"):
        websites._get_json("https://bank.pingan.com/", None, None, 1)
    assert len(calls) == 1


def test_psl_has_no_network_sources_or_disk_cache():
    assert websites._PSL.suffix_list_urls == ()
    assert not websites._PSL._cache.enabled


@pytest.mark.parametrize(
    "path",
    [
        "/tenant/..%3b/other/news",
        "/tenant/%2e%2e%3b/other/news",
        "/tenant/a%3bb",
        "/tenant/%3Fredirect=elsewhere",
        "/tenant/%23fragment",
        "/tenant/news?continue=%2F%2Fevil.com",
        "/tenant/news?redirect_to=%2F%2Fevil.com",
        "/tenant/news?return_to=evil",
        "/tenant/news?value=%2F%2Fevil.com",
        "/tenant/news?value=%252F%252Fevil.com",
        "/tenant/news?value=javascript%253Aalert(1)",
    ],
)
def test_encoded_path_and_query_redirect_ambiguity_rejected(official, path):
    data, _ = official
    data["szse"]["data"]["http"] = "https://bank.pingan.com/tenant"
    bundle = websites.resolve_company_website("000001.SZ", {}, now=NOW)
    assert not websites.matches_company_website(
        "https://bank.pingan.com" + path, bundle, "000001.SZ", now=NOW
    )


def test_zero_ttl_refreshes_each_time_but_lease_survives_bounded_search(official):
    _, calls = official
    first = websites.resolve_company_website("000001.SZ", {"company_website_cache_ttl": 0}, now=NOW)
    second = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 0}, now=NOW
    )
    assert len(calls) == 2
    assert not websites._CACHE
    assert first == second
    assert websites.company_website_cache_key(first, "000001.SZ", now=NOW + timedelta(seconds=119))
    assert not websites.company_website_cache_key(
        first, "000001.SZ", now=NOW + timedelta(seconds=120)
    )


def test_timestamp_is_after_completed_metadata_verification(official, monkeypatch):
    ticks = iter([NOW, NOW + timedelta(seconds=3)])
    monkeypatch.setattr(websites, "_utcnow", lambda: next(ticks))
    bundle = websites.resolve_company_website("000001.SZ", {})
    assert bundle["verified_at"] == (NOW + timedelta(seconds=3)).isoformat()


def test_unsupported_sse_schema_does_not_duplicate_identity_fetch(monkeypatch):
    monkeypatch.setattr(websites, "_get_json", lambda *a, **k: pytest.fail("SSE no website field"))
    assert websites.resolve_company_website("601868.SS", {})["status"] == "unsupported"


def test_streaming_slow_drip_is_bounded_and_closed(monkeypatch):
    ticks = [0.0]
    closed = []
    read_sizes = []
    monkeypatch.setattr(websites.time, "monotonic", lambda: ticks[0])

    class Response:
        status_code = 200
        history = []
        url = websites.SZSE_URL

        def iter_content(self, chunk_size):
            read_sizes.append(chunk_size)
            while True:
                ticks[0] += 0.2
                yield b" "

        def close(self):
            closed.append(True)

    def get(*args, **kwargs):
        ticks[0] += 0.2  # Header time counts against the same request budget.
        assert kwargs["timeout"].read_timeout == 0.5
        return Response()

    monkeypatch.setattr(websites.requests, "get", get)
    with pytest.raises(ValueError, match="official_metadata_response_limit"):
        websites._get_json(websites.SZSE_URL, {"secCode": "000001"}, None, 1)
    assert ticks[0] <= 1
    assert read_sizes == [1]
    assert closed == [True]


def test_streaming_oversized_and_redirected_responses_closed(monkeypatch):
    closed = []

    class Response:
        status_code = 200
        history = []
        url = websites.SZSE_URL

        def iter_content(self, chunk_size):
            yield b" " * 20

        def close(self):
            closed.append(True)

    response = Response()
    monkeypatch.setattr(websites.requests, "get", lambda *a, **k: response)
    monkeypatch.setattr(websites, "_MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ValueError, match="official_metadata_response_limit"):
        websites._get_json(websites.SZSE_URL, {}, None, 1)
    response.url = "https://evil.com/"
    with pytest.raises(ValueError, match="official_metadata_endpoint_mismatch"):
        websites._get_json(websites.SZSE_URL, {}, None, 1)
    assert closed == [True, True]


def test_smaller_ttl_clamps_still_fresh_bundle_without_fetch(official):
    _, calls = official
    original = websites.resolve_company_website("000001.SZ", {}, now=NOW)
    shortened = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 60}, now=NOW + timedelta(seconds=30)
    )
    assert len(calls) == 1
    assert shortened["expires_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert not websites.company_website_cache_key(
        shortened, "000001.SZ", now=NOW + timedelta(seconds=60)
    )
    assert original["expires_at"] == (NOW + timedelta(days=1)).isoformat()
    longer = websites.resolve_company_website(
        "000001.SZ", {"company_website_cache_ttl": 604800}, now=NOW + timedelta(seconds=30)
    )
    assert longer["expires_at"] == original["expires_at"]
    assert len(calls) == 1
