"""Shared tools consume real retrieval evidence, not planned query strings."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from tradingagents.dataflows.config import get_config, run_config
from tradingagents.extensions import (
    ashare_announcements as official,
    duckduckgo_news as ddg,
    news_evidence as evidence,
)

pytestmark = pytest.mark.unit

IDENTITY = {"canonical_symbol": "601868.SS", "code": "601868", "status": "resolved",
            "security_type": "A-share", "confidence": "verified", "exchange": "SSE",
            "chinese_short_name": "中国能建", "chinese_full_name": "中国能源建设股份有限公司",
            "english_name": "China Energy Engineering Corporation Limited"}


@pytest.fixture
def fetched(monkeypatch):
    fetch = Mock(return_value={"status": "ok", "evidence": [{"title": "Actual story",
                  "url": "https://www.reuters.com/article", "published_at": "2026-04-02T12:00:00Z",
                  "content": "Retrieved excerpt", "content_kind": "search_snippet"}], "diagnostics": {}})
    monkeypatch.setattr(ddg, "fetch_news", fetch)
    return fetch


@pytest.mark.parametrize("primary", [None, "", {}, [], evidence.NewsText("No news"),
                                      "DATA_UNAVAILABLE: provider down", '{"feed": []}'])
def test_known_missing_primary_uses_shared_fallback(primary, fetched):
    with run_config({}):
        out = evidence.retrieve_news(lambda: primary, "AAPL", "2026-04-01", "2026-04-03")
    assert fetched.call_count == 1
    assert "Retrieved excerpt" in out and '"evidence_retrieved": true' in out
    assert "search_snippet" in out and "not full article" in out


def test_error_is_sanitized_and_falls_back(fetched):
    def fail():
        raise RuntimeError("secret=DO_NOT_LEAK")
    with run_config({}):
        out = evidence.retrieve_news(fail, "AAPL", "2026-04-01", "2026-04-03")
    assert '"primary_status": "error"' in out
    assert "DO_NOT_LEAK" not in out


@pytest.mark.parametrize("primary", ["opaque custom provider report", evidence.NewsText("valid", 2)])
def test_opaque_custom_formats_and_known_good_primary_stay_unchanged(primary, fetched):
    with run_config({}):
        assert evidence.retrieve_news(lambda: primary, "AAPL", "2026-04-01", "2026-04-03") == primary
    fetched.assert_not_called()


def test_disabled_restores_primary_error(fetched):
    with run_config({"duckduckgo_news_enabled": False, "ashare_announcements_enabled": False}), pytest.raises(RuntimeError):
        evidence.retrieve_news(lambda: (_ for _ in ()).throw(RuntimeError()), "AAPL", "2026-04-01", "2026-04-03")
    fetched.assert_not_called()


@pytest.mark.parametrize("ticker, expected", [("AAPL", "AAPL"), ("SHEL.L", "SHEL.L"),
    ("700.HK", "0700.HK"), ("BTCUSD", "Bitcoin"), ("EURUSD", "EUR/USD"), ("XAUUSD", "gold")])
def test_all_markets_use_resolved_search_subject(ticker, expected, fetched):
    with run_config({"_ashare_identity": IDENTITY}):
        evidence.retrieve_news(lambda: evidence.NewsText("empty"), ticker, "2026-04-01", "2026-04-03")
    assert expected in fetched.call_args.args[0][0]
    assert "中国能建" not in str(fetched.call_args)


def test_ashare_official_and_bilingual_evidence_are_separate_and_snapshot_immutable(monkeypatch, fetched):
    off = Mock(return_value={"status": "empty", "evidence": [], "diagnostics": {}})
    monkeypatch.setattr(official, "fetch_announcements", off)
    with run_config({"_ashare_identity": deepcopy(IDENTITY), "duckduckgo_news_max_queries": 4}):
        before = get_config()
        out = evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
        assert get_config() == before
    queries = fetched.call_args.args[0]
    assert queries[:2] == ['"中国能建"', '"China Energy Engineering Corporation Limited"']
    assert fetched.call_args.args[3]["duckduckgo_news_max_queries"] == 3
    assert off.call_count == 1
    assert '"evidence_retrieved": false' in out and '"evidence_retrieved": true' in out


def test_optional_official_block_does_not_discard_news_already_retrieved(monkeypatch, fetched):
    monkeypatch.setattr(official, "fetch_announcements", lambda *a: {
        "status": "unavailable", "evidence": [], "diagnostics": {"stop_search": True, "reason": "http_403_blocked"}})
    with run_config({"_ashare_identity": IDENTITY}):
        out = evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
    assert fetched.call_count == 1
    assert "Actual story" in out and '"evidence_retrieved": true' in out
    assert "http_403_blocked" in out


def test_global_fallback_respects_requested_limit(fetched):
    with run_config({"global_news_queries": ["central banks", "economy"]}):
        evidence.retrieve_news(lambda: evidence.NewsText("empty"), None, "2026-04-01", "2026-04-03", limit=3)
    assert fetched.call_args.args[0] == ["central banks", "economy"]
    assert fetched.call_args.args[3]["duckduckgo_news_max_results"] == 3
    assert fetched.call_args.args[3]["duckduckgo_news_aliases"] == []


def test_alpha_vantage_count_is_dated_and_not_arbitrary_json():
    row = {"title": "Headline", "summary": "text", "url": "https://reuters.com/a", "time_published": "20260402T120000"}
    assert evidence._primary_available({"feed": [row]}, "2026-04-01", "2026-04-03") is True
    for bad in ["20260404T000000", "", None]:
        assert evidence._primary_available({"feed": [{**row, "time_published": bad}]}, "2026-04-01", "2026-04-03") is False
    assert evidence._primary_available({"arbitrary": "data"}, "2026-04-01", "2026-04-03") is None


def _html(url="https://static.cninfo.com.cn/finalpage/2026-04-03/1225078207.PDF", title="中国能建公告", snippet="601868 中国能源建设股份有限公司公告摘要"):
    return f'<div class="result__body"><h2><a class="result__a" href="{url}">{title}</a></h2><a class="result__snippet">{snippet}</a></div>'


@pytest.fixture(autouse=True)
def clear_official_cache(monkeypatch):
    official._CACHE.clear()
    monkeypatch.setattr(ddg, "_BLOCK_UNTIL", 0.0)
    monkeypatch.setattr(ddg, "_BLOCK_REASON", "")


def test_official_retrieval_labels_url_date_snippet_and_caches(monkeypatch):
    read = Mock(return_value=_html())
    monkeypatch.setattr(official, "_read_response", read)
    result = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert result["status"] == "ok"
    item = result["evidence"][0]
    assert item["publication_date_source"] == "url_path" and item["publication_precision"] == "day"
    assert item["publication_time_verified"] is False and item["document_text_retrieved"] is False
    assert item["content_kind"] == "search_snippet"
    assert official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})["diagnostics"]["cache_hit"]
    assert read.call_count == 1


@pytest.mark.parametrize("html", [
    _html("http://["),
    _html("https://static.cninfo.com.cn.evil.test/finalpage/2026-04-03/1.PDF"),
    _html("https://static.cninfo.com.cn/finalpage/2026-04-04/1.PDF"),
    _html("https://static.cninfo.com.cn/finalpage/2026-04-03/not-an-id.PDF"),
    _html(title="Different company", snippet="Different issuer no matching name or code"),
    _html(snippet=""),
])
def test_official_rejects_wrong_domain_date_company_or_missing_excerpt(monkeypatch, html):
    monkeypatch.setattr(official, "_read_response", lambda *a: html)
    result = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert result["evidence"] == [] and result["status"] == "empty"


def test_official_malformed_protocol_is_unavailable_not_empty(monkeypatch):
    monkeypatch.setattr(official, "_read_response", lambda *a: "<html>unsupported</html>")
    result = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert result["status"] == "unavailable" and not result["evidence"]


def test_evidence_settings_change_checkpoint_signature():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**get_config()}
    graph.selected_analysts = ["news"]
    before = graph._run_signature("stock")
    graph.config["duckduckgo_news_allowed_domains"] = ["example.com"]
    assert graph._run_signature("stock") != before
    graph.config["duckduckgo_news_allowed_domains"] = []
    graph.config["duckduckgo_news_enabled"] = False
    assert graph._run_signature("stock") != before


def test_environment_flags_available_to_both_cli_modes(monkeypatch):
    from tradingagents.default_config import build_default_config
    monkeypatch.setenv("TRADINGAGENTS_DUCKDUCKGO_NEWS_ENABLED", "false")
    monkeypatch.setenv("TRADINGAGENTS_ASHARE_ANNOUNCEMENTS_ENABLED", "false")
    settings = build_default_config()
    assert settings["duckduckgo_news_enabled"] is False
    assert settings["ashare_announcements_enabled"] is False


def test_bad_primary_configuration_still_raises(fetched):
    with run_config({}), pytest.raises(ValueError, match="bad vendor"):
        evidence.retrieve_news(lambda: (_ for _ in ()).throw(ValueError("bad vendor")), "AAPL", "2026-04-01", "2026-04-03")
    fetched.assert_not_called()


def test_global_tool_uses_effective_configured_limit(monkeypatch):
    from tradingagents.agents import tools
    capture = Mock(return_value="evidence")
    monkeypatch.setattr(tools, "retrieve_news", capture)
    with run_config({"global_news_article_limit": 1}):
        assert tools.get_global_news.func("2026-04-03") == "evidence"
    assert capture.call_args.kwargs["limit"] == 1


def test_one_query_budget_prioritizes_news_over_official(monkeypatch, fetched):
    announcement = Mock()
    monkeypatch.setattr(official, "fetch_announcements", announcement)
    with run_config({"_ashare_identity": IDENTITY, "duckduckgo_news_max_queries": 1}):
        evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
    assert fetched.call_count == 1
    assert fetched.call_args.args[3]["duckduckgo_news_max_queries"] == 1
    announcement.assert_not_called()


@pytest.mark.parametrize(
    ("config", "news_queries", "official_calls"),
    [({}, 1, 1), ({"duckduckgo_news_max_queries": 3}, 2, 1),
     ({"ashare_announcements_enabled": False}, 2, 0)],
)
def test_default_and_explicit_query_caps_remain_shared(
    monkeypatch, fetched, config, news_queries, official_calls,
):
    announcement = Mock(return_value={"status": "empty", "evidence": [], "diagnostics": {}})
    monkeypatch.setattr(official, "fetch_announcements", announcement)
    with run_config({"_ashare_identity": IDENTITY, **config}):
        evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
    assert fetched.call_args.args[3]["duckduckgo_news_max_queries"] == news_queries
    assert announcement.call_count == official_calls


def test_official_challenge_sets_shared_cooldown(monkeypatch):
    read = Mock(side_effect=ddg._SearchFailure("http_403_blocked", stop=True))
    monkeypatch.setattr(official, "_read_response", read)
    result = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert result["diagnostics"]["stop_search"]
    assert ddg.block_status()["reason"] == "http_403_blocked"
    second = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert second["diagnostics"]["cooldown"] and read.call_count == 1


def test_official_and_news_share_one_search_deadline(monkeypatch, fetched):
    captured = []
    def announcements(identity, start, end, config):
        captured.append(config["_duckduckgo_news_deadline"])
        return {"status": "empty", "evidence": [], "diagnostics": {}}
    monkeypatch.setattr(official, "fetch_announcements", announcements)
    monkeypatch.setattr(evidence.time, "monotonic", lambda: 100.0)
    with run_config({"_ashare_identity": IDENTITY, "duckduckgo_news_total_timeout": 45}):
        evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
    assert captured == [145.0]
    assert fetched.call_args.args[3]["_duckduckgo_news_deadline"] == 145.0


def test_official_budget_exhaustion_does_not_claim_provider_block(monkeypatch):
    monkeypatch.setattr(official, "_read_response", Mock(side_effect=ddg._SearchFailure("budget_exhausted", stop=True)))
    result = official.fetch_announcements(IDENTITY, "2026-04-01", "2026-04-03", {})
    assert result["diagnostics"]["reason"] == "budget_exhausted"
    assert ddg.block_status() is None


def test_news_block_stops_optional_official_requests(monkeypatch, fetched):
    fetched.return_value = {"status": "unavailable", "evidence": [], "diagnostics": {"stop_search": True, "stop_reason": "http_403_blocked"}}
    announcement = Mock()
    monkeypatch.setattr(official, "fetch_announcements", announcement)
    with run_config({"_ashare_identity": IDENTITY}):
        result = evidence.retrieve_news(lambda: evidence.NewsText("empty"), "601868.SS", "2026-04-01", "2026-04-03")
    announcement.assert_not_called()
    assert "skipped_after_search_stop" in result


def test_standalone_diagnostic_never_calls_primary_or_official(monkeypatch, capsys):
    import json

    from tradingagents.extensions import news_diagnostics
    fetch = Mock(return_value={"status": "empty", "evidence": [], "diagnostics": {"excluded": {"out_of_window": 2}}})
    monkeypatch.setattr(news_diagnostics, "fetch_news", fetch)
    assert news_diagnostics.main(["--query", "中国能建", "--start-date", "2026-10-01", "--end-date", "2026-10-08"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["accepted_count"] == 0
    assert output["mode"] == "duckduckgo_news_only_no_llm_no_official_discovery"
    assert fetch.call_args.args[:3] == (["中国能建"], "2026-10-01", "2026-10-08")
    assert fetch.call_args.args[3]["duckduckgo_news_max_queries"] == 1


def test_standalone_diagnostic_rejects_unbounded_days():
    from tradingagents.extensions import news_diagnostics
    with pytest.raises(SystemExit):
        news_diagnostics.main(["--query", "Microsoft", "--days", "999999"])
