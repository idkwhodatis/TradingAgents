"""Snapshot-derived A-share search names improve discovery, not attribution."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tradingagents.dataflows.vendors.yahoo.news as ynews
from tradingagents.dataflows.config import get_config, run_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.extensions import ashare_identity

pytestmark = pytest.mark.unit

CODE = "600519"
CANONICAL = f"{CODE}.SS"
SHORT_NAME = "贵州茅台"
FULL_NAME = "贵州茅台酒股份有限公司"
ENGLISH_NAME = "Kweichow Moutai Co., Ltd."
SHORT_QUERY = f"{SHORT_NAME} {CODE}"
FULL_QUERY = f"{FULL_NAME} {CODE}"
ENGLISH_QUERY = f"{ENGLISH_NAME} {CODE}"
QUERIES = [CANONICAL, SHORT_QUERY, FULL_QUERY, ENGLISH_QUERY]


@pytest.fixture
def snapshot_config(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("News must consume a snapshot, never refresh exchange metadata")

    monkeypatch.setattr(ashare_identity, "_lookup", unexpected_lookup)
    return {
        "news_article_limit": 10,
        "_ashare_identity": {
            "adapter_version": 1,
            "code": CODE,
            "canonical_symbol": CANONICAL,
            "status": "resolved",
            "security_type": "A-share",
            "confidence": "verified",
            "exchange": "SSE",
            "chinese_short_name": SHORT_NAME,
            "chinese_full_name": FULL_NAME,
            "english_name": ENGLISH_NAME,
            "english_name_kind": "official",
            "sources": [{"provider": "SSE", "url": ashare_identity.SSE_URL}],
            "retrieved_at": "2026-10-08T12:00:00+00:00",
        },
    }


def _article(title, *, tickers=(CANONICAL,), day="2025-05-08", link=None, **extra):
    return {
        "title": title,
        "publisher": "Wire",
        "link": link if link is not None else f"https://example.test/{title}",
        "providerPublishTime": int(
            datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        ),
        "relatedTickers": list(tickers),
        **extra,
    }


def _yahoo(monkeypatch, results, *, feed=()):
    queried = []
    tickers = []

    class FakeTicker:
        def __init__(self, ticker):
            tickers.append(ticker)

        def get_news(self, count):
            return list(feed)

    def search(query, news_count):
        queried.append(query)
        response = results.get(query, [])
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(news=response, quotes=[{"symbol": CANONICAL}])

    monkeypatch.setattr(ynews.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(ynews.yf, "Search", search)
    return queried, tickers


def test_verified_snapshot_adds_bilingual_queries_with_exact_ticker_attribution(
    monkeypatch, snapshot_config,
):
    repeated = _article("Canonical story", uuid="same-story")
    queried, tickers = _yahoo(monkeypatch, {
        CANONICAL: [repeated],
        SHORT_QUERY: [dict(repeated), _article("Chinese story", tickers=("600519.ss",))],
        FULL_QUERY: [_article(FULL_NAME, tickers=("000858.SZ",))],
        ENGLISH_QUERY: [_article("English story"), _article(ENGLISH_NAME, tickers=("MOUTAI",))],
    })

    with run_config(snapshot_config):
        output = ynews.get_news_yfinance("600519.SH", "2025-05-01", "2025-05-09")

    assert tickers == [CANONICAL]
    assert queried == QUERIES
    assert "600519.SH (resolved to 600519.SS) News" in output
    assert output.count("### Canonical story") == 1
    assert "Chinese story" in output and "English story" in output
    assert FULL_NAME not in output and ENGLISH_NAME not in output
    assert "identity and its provenance" in output
    assert "do not establish Yahoo/overseas news coverage" in output
    assert "official exchange/issuer announcements" in output


def test_name_or_ticker_substring_matches_alone_never_establish_article_identity(
    monkeypatch, snapshot_config,
):
    queried, _ = _yahoo(monkeypatch, {
        SHORT_QUERY: [
            _article(f"{SHORT_NAME} {CANONICAL}", tickers=()),
            _article("Almost same ticker", tickers=("600519.SSX",)),
            _article("Different listing", tickers=("600519.SZ",)),
            _article("Wrong company", tickers=("000858.SZ",)),
        ],
    })
    with run_config(snapshot_config), pytest.raises(
        VendorUnavailableError, match="has no news articles tagged 600519.SS",
    ):
        ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == QUERIES


def test_unattributable_name_results_preserve_the_configured_vendor_fallback(
    monkeypatch, snapshot_config,
):
    from tradingagents.dataflows import router

    _yahoo(monkeypatch, {ENGLISH_QUERY: [_article(ENGLISH_NAME, tickers=("OTHER",))]})
    fallback_calls = []

    def other_vendor(*args, **kwargs):
        fallback_calls.append(args)
        return "NEXT VENDOR NEWS"

    monkeypatch.setitem(router.VENDOR_METHODS, "get_news", {
        "yfinance": ynews.get_news_yfinance, "alpha_vantage": other_vendor,
    })
    snapshot_config["data_vendors"] = {"news_data": "yfinance,alpha_vantage"}
    with run_config(snapshot_config):
        before = get_config()
        output = router.route_to_vendor("get_news", CANONICAL, "2025-05-01", "2025-05-09")
        assert get_config() == before

    assert output == "NEXT VENDOR NEWS"
    assert fallback_calls == [(CANONICAL, "2025-05-01", "2025-05-09")]


def test_optional_name_search_failure_does_not_discard_canonical_results(
    monkeypatch, snapshot_config,
):
    queried, _ = _yahoo(monkeypatch, {
        CANONICAL: [_article("Canonical story")],
        SHORT_QUERY: RuntimeError("name query unavailable"),
        ENGLISH_QUERY: [_article("English story")],
    })
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == QUERIES
    assert "Canonical story" in output and "English story" in output


def test_primary_search_error_keeps_existing_vendor_error_behavior(monkeypatch, snapshot_config):
    queried, _ = _yahoo(monkeypatch, {CANONICAL: RuntimeError("primary unavailable")})
    with run_config(snapshot_config), pytest.raises(VendorUnavailableError):
        ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == [CANONICAL]


def test_name_search_sample_still_cannot_prove_absence_of_news(monkeypatch, snapshot_config):
    _yahoo(monkeypatch, {
        SHORT_QUERY: [_article("Older", day="2025-04-01")],
        ENGLISH_QUERY: [_article("Future", day="2025-06-01")],
    })
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert "unavailable" in output and "not an absence" in output
    assert "No news found" not in output
    assert "###" not in output
    assert "official exchange/issuer announcements" in output


def test_bilingual_article_budget_applies_after_deduplication_and_date_filtering(
    monkeypatch, snapshot_config,
):
    snapshot_config["news_article_limit"] = 2
    first = _article("First")
    _yahoo(monkeypatch, {
        CANONICAL: [_article("Old", day="2025-04-01")],
        SHORT_QUERY: [first],
        FULL_QUERY: [dict(first)],
        ENGLISH_QUERY: [_article("Second"), _article("Third")],
    })
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert output.count("###") == 2
    assert "First" in output and "Second" in output
    assert "Old" not in output and "Third" not in output


def test_an_answering_quote_feed_retains_priority(monkeypatch, snapshot_config):
    queried, _ = _yahoo(monkeypatch, {}, feed=[_article("Quote feed story")])
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == []
    assert "Quote feed story" in output
    assert "A-share coverage note" in output


@pytest.mark.parametrize("ticker, canonical", [
    ("AAPL", "AAPL"), ("SHEL.L", "SHEL.L"), ("700.HK", "0700.HK"),
    ("BTCUSD", "BTC-USD"), ("EURUSD", "EURUSD=X"), ("XAUUSD", "GC=F"),
])
def test_other_markets_keep_canonical_only_queries_and_unchanged_output(
    monkeypatch, snapshot_config, ticker, canonical,
):
    queried, tickers = _yahoo(monkeypatch, {
        canonical: [_article("Other market story", tickers=(canonical,))],
    })
    # A snapshot for another instrument must never leak names or caveats.
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(ticker, "2025-05-01", "2025-05-09")
    assert queried == [canonical] and tickers == [canonical]
    resolved = "" if ticker == canonical else f" (resolved to {canonical})"
    assert output == (
        f"## {ticker}{resolved} News, from 2025-05-01 to 2025-05-09:\n\n"
        "### Other market story (source: Wire)\n"
        "Link: https://example.test/Other market story\n\n"
    )


@pytest.mark.parametrize("status", ["unavailable", "not_a_share"])
def test_unverified_snapshot_never_adds_names_or_claims_verification(
    monkeypatch, snapshot_config, status,
):
    snapshot_config["_ashare_identity"].update({
        "status": status, "confidence": "unknown", "security_type": None,
    })
    queried, _ = _yahoo(monkeypatch, {CANONICAL: [_article("Tagged story")]})
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == [CANONICAL]
    assert "A-share coverage note" not in output


def test_missing_snapshot_does_not_trigger_identity_lookup(monkeypatch, snapshot_config):
    queried, _ = _yahoo(monkeypatch, {CANONICAL: [_article("Tagged story")]})
    with run_config({"news_article_limit": 10}):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert queried == [CANONICAL]
    assert "A-share coverage note" not in output


def test_flat_and_nested_duplicates_are_merged_after_ticker_filter(monkeypatch, snapshot_config):
    nested = {
        "content": {
            "id": "shared-id", "title": "Nested duplicate", "pubDate": "2025-05-08T00:00:00Z",
            "provider": {"displayName": "Wire"},
            "canonicalUrl": {"url": "https://example.test/shared"},
        },
        "relatedTickers": [CANONICAL],
    }
    _yahoo(monkeypatch, {
        CANONICAL: [_article("Wrong", uuid="shared-id", tickers=("OTHER",))],
        SHORT_QUERY: [_article("Kept", uuid="shared-id", link="https://example.test/shared")],
        FULL_QUERY: [nested],
        ENGLISH_QUERY: [_article("Link duplicate", link="https://example.test/shared")],
    })
    with run_config(snapshot_config):
        output = ynews.get_news_yfinance(CANONICAL, "2025-05-01", "2025-05-09")
    assert output.count("###") == 1
    assert "Kept" in output
    assert "Wrong" not in output and "duplicate" not in output
