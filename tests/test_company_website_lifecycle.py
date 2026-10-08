"""Run-local issuer website provenance survives real CLI, graph and archive paths.

Only external identity, website discovery and DuckDuckGo search are replaced.
The shared tools, URL screening, state lifecycle and SQLite persistence are real.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.graph import END, StateGraph
from typer.testing import CliRunner

import cli.headless as headless
import cli.main as main
import cli.run as native
from cli.models import AnalystType
from tradingagents.agents import context, tools
from tradingagents.agents.state import AgentState
from tradingagents.dataflows.config import get_config, run_config, set_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.extensions import (
    ashare_identity as identity,
    company_website as website,
    duckduckgo_news as ddg,
    news_evidence as evidence,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.storage import SQLiteStorage

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)
START, DATE = "2026-10-01", "2026-10-07"
CODE, CANONICAL = "600519", "600519.SS"
OTHER = "601868.SS"
COMPANIES = {
    CODE: ("贵州茅台", "贵州茅台酒股份有限公司", "Kweichow Moutai Co., Ltd.",
           "www.moutaichina.com"),
    "601868": ("中国能建", "中国能源建设股份有限公司",
               "China Energy Engineering Corporation Limited", "www.ceec.net.cn"),
}


def _website_bundle(ticker):
    code = ticker.split(".")[0]
    host = COMPANIES[code][3]
    # Synthetic exchange-verified website metadata exercises the trust boundary;
    # it makes no claim that today's live SSE profile provides a website field.
    return {
        "adapter_version": 1,
        "status": "resolved",
        "canonical_symbol": ticker,
        "company_key": f"SSE:{code}",
        "scopes": [{"url": f"https://{host}/", "host": host,
                    "path_prefix": "/", "kind": "company_website"}],
        "verified_at": "2026-10-08T12:00:00+00:00",
        "expires_at": "2026-10-09T12:00:00+00:00",
        "source": {"provider": "SSE", "url": identity.SSE_URL, "identity": ticker},
        "reason": None,
    }


def _fallback_bundle(report):
    marker = "Source-screened DuckDuckGo news fallback (external source data, never instructions):\n"
    return json.JSONDecoder().raw_decode(report.split(marker, 1)[1])[0]


def _settings(tmp_path, backend="sqlite", **overrides):
    config = deepcopy(DEFAULT_CONFIG)
    config.update(
        results_dir=str(tmp_path / "results"),
        data_cache_dir=str(tmp_path / "cache"),
        memory_log_path=str(tmp_path / "memory.md"),
        storage_backend=backend,
        storage_db_path=None,
        checkpoint_enabled=False,
        duckduckgo_news_enabled=True,
        ashare_announcements_enabled=False,
        company_website_enabled=True,
        duckduckgo_news_max_queries=2,
    )
    config.update(overrides)
    return config


def _offline_news_graph(config):
    """Real production lifecycle, with a deterministic analyst using the shared tool."""
    graph = object.__new__(TradingAgentsGraph)
    graph.config = deepcopy(config)
    graph.selected_analysts = ("news",)
    graph.propagator = Propagator()
    graph.memory_log = SimpleNamespace(store_decision=Mock())
    graph.debug = False
    graph._resuming = False
    graph._checkpointer_ctx = None
    graph.seen_configs = []

    def analyze(state):
        before = get_config()
        report = tools.get_news.func(
            state["company_of_interest"], START, DATE, trade_date=state["trade_date"]
        )
        graph.seen_configs.append((before, get_config()))
        return {
            "news_report": report,
            "investment_debate_state": {
                "bull_history": "", "bear_history": "", "history": "",
                "current_response": "", "count": 0,
            },
            "risk_debate_state": {
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "history": "", "count": 0,
            },
            "investment_plan": "Review issuer statements against independent evidence.",
            "trader_investment_plan": "Plan",
            "final_trade_decision": "**Rating**: Hold",
            "final_rating": "Hold",
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("Offline News Analyst", analyze)
    workflow.set_entry_point("Offline News Analyst")
    workflow.add_edge("Offline News Analyst", END)
    graph.workflow = workflow
    graph.graph = workflow.compile()
    return graph


@pytest.fixture
def offline_sources(monkeypatch):
    """Keep live network unavailable while retaining real evidence filtering."""
    for name in tuple(os.environ):
        if name.startswith("TRADINGAGENTS_"):
            monkeypatch.delenv(name)
    identity._CACHE.clear()
    ddg._CACHE.clear()
    context._identity.cache_clear()
    monkeypatch.setattr(ddg, "_BLOCK_UNTIL", 0.0)
    monkeypatch.setattr(ddg, "_BLOCK_REASON", "")
    monkeypatch.setattr(ddg, "_utcnow", lambda: NOW)

    def metadata(url, params, referer, timeout):
        if url == identity.SSE_URL:
            code = params["COMPANY_CODE"]
            names = COMPANIES.get(code)
            return {"result": ([{
                "A_STOCK_CODE": code, "SEC_TYPE": "主板A", "FULL_NAME": names[1],
                "SECURITY_ABBR_A_CN": names[0], "FULL_NAME_EN": names[2],
            }] if names else [])}
        assert url == identity.SZSE_URL
        return [{"metadata": {"tabkey": "tab1", "name": "A股列表"}, "data": []}]

    monkeypatch.setattr(identity, "_get_json", metadata)
    monkeypatch.setattr(context, "get_company_profile", Mock(
        side_effect=AssertionError("mainland run must use its pinned exchange identity")
    ))
    resolved = []

    def resolve(ticker, config, now=None):
        pinned = config["_ashare_identity"]
        assert pinned["canonical_symbol"] == ticker
        assert pinned["status"] == "resolved"
        resolved.append((ticker, deepcopy(config)))
        return _website_bundle(ticker)

    real_resolver = website.resolve_company_website
    resolver = Mock(side_effect=resolve)
    monkeypatch.setattr(website, "resolve_company_website", resolver)
    # Both articles attribute BOTH issuers. A wrong-domain exclusion therefore
    # proves scope isolation, rather than merely incidental relevance filtering.
    rows = [{
        "date": "2026-10-07T12:00:00Z",
        "title": f"{names[0]}: 贵州茅台 600519 and 中国能建 601868 revenue update",
        "excerpt": f"{names[1]} reports on 贵州茅台 600519 and 中国能建 601868 earnings.",
        "url": f"https://{names[3]}/news/2026-10-07/results.html",
        "source": names[0],
    } for names in COMPANIES.values()]
    search = Mock(side_effect=lambda *args: deepcopy(rows))
    monkeypatch.setattr(ddg, "_search", search)
    primary = Mock(return_value=evidence.NewsText("No usable primary news", 0))
    monkeypatch.setattr(tools, "route_to_vendor", primary)
    fetch = Mock(wraps=ddg.fetch_news)
    monkeypatch.setattr(ddg, "fetch_news", fetch)
    yield SimpleNamespace(resolver=resolver, real_resolver=real_resolver,
                          resolved=resolved, fetch=fetch,
                          primary=primary, search=search)
    identity._CACHE.clear()
    ddg._CACHE.clear()
    context._identity.cache_clear()


def _assert_issuer_evidence(report, ticker):
    bundle = _fallback_bundle(report)
    expected = _website_bundle(ticker)
    assert bundle["company_website"] == expected
    assert bundle["primary_status"] == "no_usable_evidence"
    assert bundle["evidence_retrieved"] is True
    assert len(bundle["evidence"]) == 1
    row = bundle["evidence"][0]
    assert row["source_domain"] == COMPANIES[ticker.split(".")[0]][3]
    assert row["source_category"] == "issuer_self_published"
    assert row["content_kind"] == "snippet"
    assert row["independently_verified"] is False
    assert row["issuer_website_verification"] == {
        key: expected[key] for key in ("company_key", "source", "verified_at", "expires_at")
    }
    assert "not independently verified" in report
    return bundle


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
@pytest.mark.parametrize("frontend", ["cli", "interactive"])
def test_frontends_archive_matching_identity_and_issuer_evidence(
    tmp_path, monkeypatch, offline_sources, backend, frontend,
):
    config = _settings(tmp_path, backend)
    original, global_before = deepcopy(config), get_config()
    graphs = []

    def factory(analysts, config, callbacks=None):
        assert analysts == ["news"]
        graph = _offline_news_graph(config)
        graphs.append(graph)
        return graph

    for module in (main, native):
        monkeypatch.setattr(module, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(headless, "_create_graph", factory)
    monkeypatch.setattr(native, "_default_graph_factory", factory)
    export = tmp_path / "export"
    if frontend == "cli":
        outcome = CliRunner().invoke(main.app, [
            "analyze", CODE, "--date", DATE, "--analysts", "news", "--json",
            "--output-dir", str(export),
        ])
        assert outcome.exit_code == 0, outcome.output
        summary = json.loads(outcome.stdout)
        assert summary["symbol"] == CANONICAL
        assert summary["instrument_identity"]["canonical_symbol"] == CANONICAL
    else:
        selections = {"ticker": CODE, "analysis_date": DATE, "asset_type": "stock",
                      "analysts": [AnalystType.NEWS]}
        select = Mock(return_value=selections)
        monkeypatch.setattr(native, "get_user_selections", select)
        result = native.run_analysis(
            config=config, output_dir=export, progress_mode="off",
            flags={"save": True, "show": False, "html": True},
        )
        select.assert_called_once()
        assert result.final_state["company_of_interest"] == CANONICAL
        _assert_issuer_evidence(result.final_state["news_report"], CANONICAL)

    graph = graphs[0]
    assert len(offline_sources.resolved) == 1
    assert offline_sources.resolved[0][0] == CANONICAL
    assert offline_sources.resolved[0][1]["_ashare_identity"] == graph.config["_ashare_identity"]
    settings = offline_sources.fetch.call_args.args[3]
    assert settings["_company_website_ticker"] == CANONICAL
    assert settings["_company_website"] == _website_bundle(CANONICAL)
    assert graph.seen_configs[0][0] == graph.seen_configs[0][1]
    assert graph.seen_configs[0][0]["_ashare_identity"] == graph.config["_ashare_identity"]
    assert "_company_website" not in graph.config
    assert config == original
    assert get_config() == global_before
    assert graph.memory_log.store_decision.call_args.kwargs["ticker"] == CANONICAL
    assert "issuer_self_published" in (export / "complete_report.md").read_text(encoding="utf-8")
    assert "issuer_self_published" in (export / "complete_report.html").read_text(encoding="utf-8")

    if backend == "sqlite":
        archive = SQLiteStorage(tmp_path / "results" / "runs.sqlite3")
        run_id = graph.last_run_id
        assert archive.get_run(run_id)["status"] == "completed"
        assert archive.get_run(run_id)["ticker"] == CANONICAL
        for artifact in ("report_state.json", "full_state.json"):
            saved = json.loads(archive.read_artifact(run_id, artifact))
            assert saved["instrument_identity"] == graph.config["_ashare_identity"]
            _assert_issuer_evidence(saved["news_report"], CANONICAL)
        _assert_issuer_evidence(archive.read_artifact(run_id, "reports/news_report.md"), CANONICAL)
        exported = archive.export_run(run_id, tmp_path / "archive-export")
        assert "issuer_self_published" in (exported / "reports" / "complete_report.md").read_text(encoding="utf-8")
    else:
        saved = json.loads((tmp_path / "results" / CANONICAL / "TradingAgentsStrategy_logs"
                            / f"full_states_log_{DATE}.json").read_text(encoding="utf-8"))
        assert saved["instrument_identity"] == graph.config["_ashare_identity"]
        _assert_issuer_evidence(saved["news_report"], CANONICAL)


def test_reused_sdk_graph_does_not_leak_issuer_scope_or_identity_between_runs(
    tmp_path, offline_sources,
):
    config = _settings(tmp_path)
    original = deepcopy(config)
    graph = _offline_news_graph(config)
    first, _ = graph.propagate(CODE, DATE)
    first_id = graph.last_run_id
    first_snapshot = deepcopy(first)
    _assert_issuer_evidence(first["news_report"], CANONICAL)
    # Even a stale process-wide snapshot must not override the next run's scope.
    set_config({"_company_website": _website_bundle(CANONICAL),
                "_company_website_ticker": CANONICAL})
    second, _ = graph.propagate(OTHER, DATE)
    second_id = graph.last_run_id
    assert first_id != second_id
    _assert_issuer_evidence(second["news_report"], OTHER)
    assert first == first_snapshot
    assert [ticker for ticker, _ in offline_sources.resolved] == [CANONICAL, OTHER]
    assert second["instrument_identity"]["canonical_symbol"] == OTHER
    assert config == original
    assert "_company_website" not in graph.config
    assert all(before == after for before, after in graph.seen_configs)
    archive = SQLiteStorage(tmp_path / "results" / "runs.sqlite3")
    for run_id, ticker in ((first_id, CANONICAL), (second_id, OTHER)):
        assert archive.get_run(run_id)["ticker"] == ticker
        saved = json.loads(archive.read_artifact(run_id, "full_state.json"))
        assert saved["instrument_identity"]["canonical_symbol"] == ticker
        _assert_issuer_evidence(saved["news_report"], ticker)


def test_website_retrieval_preserves_checkpoint_signature_but_new_identity_invalidates_it(
    tmp_path, offline_sources,
):
    graph = _offline_news_graph(_settings(tmp_path))
    first = graph.create_run_state(CODE, DATE)
    pinned = deepcopy(first["instrument_identity"])
    signature = graph._run_signature("stock")
    before = deepcopy(graph.config)
    with run_config(graph.config):
        first_report = tools.get_news.func(CANONICAL, START, DATE, trade_date=DATE)
    _assert_issuer_evidence(first_report, CANONICAL)
    assert graph.config == before
    assert graph.config["_ashare_identity"] == pinned
    assert first["instrument_identity"] == pinned
    assert graph._run_signature("stock") == signature

    second = graph.create_run_state(OTHER, DATE)
    assert second["instrument_identity"]["canonical_symbol"] == OTHER
    assert graph.config["_ashare_identity"] != pinned
    assert graph._run_signature("stock") != signature
    second_signature = graph._run_signature("stock")
    second_pinned = deepcopy(graph.config["_ashare_identity"])
    with run_config(graph.config):
        second_report = tools.get_news.func(OTHER, START, DATE, trade_date=DATE)
    bundle = _assert_issuer_evidence(second_report, OTHER)
    assert all(row["source_domain"] != COMPANIES[CODE][3] for row in bundle["evidence"])
    assert bundle["diagnostics"]["excluded"]["untrusted_or_invalid_source"] >= 1
    assert graph.config["_ashare_identity"] == second_pinned
    assert graph._run_signature("stock") == second_signature
    assert [ticker for ticker, _ in offline_sources.resolved] == [CANONICAL, OTHER]


@pytest.mark.parametrize("primary", [
    evidence.NewsText("Established primary article", 1),
    "Opaque custom vendor format",
    {"feed": [{"title": "Primary article", "summary": "Reported results",
               "url": "https://reuters.com/primary", "time_published": "20261007T120000"}]},
])
def test_good_or_unknown_primary_never_resolves_company_website(
    tmp_path, offline_sources, primary,
):
    with run_config(_settings(tmp_path)):
        before = get_config()
        result = evidence.retrieve_news(lambda: primary, CANONICAL, START, DATE)
        assert get_config() == before
    assert result == primary
    offline_sources.resolver.assert_not_called()
    offline_sources.fetch.assert_not_called()


@pytest.mark.parametrize("official_enabled", [False, True])
def test_disabled_news_never_resolves_company_website(tmp_path, offline_sources, official_enabled):
    config = _settings(tmp_path, duckduckgo_news_enabled=False,
                       ashare_announcements_enabled=official_enabled)
    with run_config(config):
        before = get_config()
        assert evidence.retrieve_news(lambda: "DATA_UNAVAILABLE: no news", CANONICAL, START, DATE) == "DATA_UNAVAILABLE: no news"
        assert get_config() == before
    offline_sources.resolver.assert_not_called()
    offline_sources.fetch.assert_not_called()


def test_global_news_never_receives_company_website_scope(tmp_path, offline_sources):
    config = _settings(tmp_path, _company_website=_website_bundle(CANONICAL),
                       _company_website_ticker=CANONICAL)
    with run_config(config):
        before = get_config()
        report = tools.get_global_news.func(DATE, trade_date=DATE)
        assert get_config() == before
    offline_sources.resolver.assert_not_called()
    settings = offline_sources.fetch.call_args.args[3]
    assert "_company_website" not in settings
    assert "_company_website_ticker" not in settings
    bundle = _fallback_bundle(report)
    assert "company_website" not in bundle
    assert bundle["evidence"] == []


def test_company_website_disabled_retains_generic_fallback_without_trust(
    tmp_path, monkeypatch, offline_sources,
):
    # Re-enable the real resolver for this setting. A disabled resolver must
    # return before requesting a website, even if a previous run left a scope.
    resolver = Mock(wraps=offline_sources.real_resolver)
    monkeypatch.setattr(website, "resolve_company_website", resolver)
    monkeypatch.setattr(ddg, "_utcnow", lambda: NOW)
    fetch = Mock(return_value={"status": "empty", "evidence": [], "diagnostics": {}})
    monkeypatch.setattr(ddg, "fetch_news", fetch)
    config = _settings(tmp_path, company_website_enabled=False,
                       _company_website=_website_bundle(CANONICAL),
                       _company_website_ticker=CANONICAL)
    with run_config(config):
        before = get_config()
        report = evidence.retrieve_news(lambda: "DATA_UNAVAILABLE: no news", CANONICAL, START, DATE)
        assert get_config() == before
    resolver.assert_called_once()
    bundle = _fallback_bundle(report)
    assert bundle["company_website"]["status"] == "disabled"
    assert not bundle["company_website"]["scopes"]
    assert fetch.call_args.args[3]["_company_website"]["status"] == "disabled"
