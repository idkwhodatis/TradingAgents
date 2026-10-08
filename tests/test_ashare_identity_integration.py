"""Mainland identity stays pinned across CLI, SDK, checkpoints and archives."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.graph import END, StateGraph

import cli.headless as headless
import cli.run as native
from cli.execution import AnalysisObserver, AnalysisRequest, execute_graph
from cli.models import AnalystType
from tradingagents.agents import context
from tradingagents.agents.state import AgentState
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.extensions import ashare_identity as identity
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.storage import SQLiteStorage

DATE = "2026-09-27"
CODE = "600519"
CANONICAL = CODE + ".SS"


@pytest.fixture
def official_metadata(monkeypatch):
    identity._CACHE.clear()
    calls = []

    def answer(url, params, referer, timeout):
        calls.append((url, params.copy()))
        if url == identity.SSE_URL:
            code = params["COMPANY_CODE"]
            return {"result": ([{
                "A_STOCK_CODE": CODE, "SEC_TYPE": "主板A", "FULL_NAME": "贵州茅台酒股份有限公司",
                "SECURITY_ABBR_A_CN": "贵州茅台", "FULL_NAME_EN": "Kweichow Moutai Co., Ltd.",
                "COMPANY_ABBR_EN": "MOUTAI",
            }] if code == CODE else [])}
        return [{"metadata": {"tabkey": "tab1", "name": "A股列表"}, "data": []}]

    monkeypatch.setattr(identity, "_get_json", answer)
    # Any accidental mainland fallback to Yahoo is an integration regression.
    monkeypatch.setattr(context, "get_company_profile", Mock(side_effect=AssertionError("unexpected Yahoo identity")))
    context._identity.cache_clear()
    yield calls
    identity._CACHE.clear()
    context._identity.cache_clear()


def settings(tmp_path, backend="filesystem", **overrides):
    config = deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), storage_backend=backend,
                  storage_db_path=None, checkpoint_enabled=False)
    config.update(overrides)
    return config


def offline_graph(config):
    """Real graph lifecycle and state schema; no LLMs, vendors or trading work."""
    graph = object.__new__(TradingAgentsGraph)
    graph.config = deepcopy(config)
    graph.selected_analysts = ("market",)
    graph.propagator = Propagator()
    graph.memory_log = SimpleNamespace(store_decision=Mock())
    graph.debug = False
    graph._resuming = False
    graph._checkpointer_ctx = None
    graph.seen_configs = []

    def done(state):
        graph.seen_configs.append(get_config())
        return {"market_report": state["instrument_context"],
                "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "",
                                            "current_response": "", "count": 0},
                "risk_debate_state": {"aggressive_history": "", "conservative_history": "",
                                      "neutral_history": "", "history": "", "count": 0},
                "investment_plan": "Research", "trader_investment_plan": "Plan",
                "final_trade_decision": "**Rating**: Hold", "final_rating": "Hold"}

    workflow = StateGraph(AgentState)
    workflow.add_node("Offline Analyst", done)
    workflow.set_entry_point("Offline Analyst")
    workflow.add_edge("Offline Analyst", END)
    graph.workflow = workflow
    graph.graph = workflow.compile()
    return graph


def selections(ticker=CODE):
    return {"ticker": ticker, "analysis_date": DATE, "asset_type": "stock",
            "analysts": [AnalystType.MARKET]}


def test_state_canonicalizes_once_and_anchors_bilingual_current_names(tmp_path, official_metadata):
    config = settings(tmp_path)
    graph = offline_graph(config)
    state = graph.create_run_state(CODE, DATE)
    assert state["company_of_interest"] == CANONICAL
    assert "贵州茅台酒股份有限公司" in state["instrument_context"]
    assert "Kweichow Moutai Co., Ltd." in state["instrument_context"]
    assert "current" in state["instrument_context"].lower()
    assert "not point-in-time" in state["instrument_context"]
    assert state["instrument_identity"]["english_name_kind"] == "official"
    assert identity.SSE_URL in state["instrument_context"]
    assert "Official announcements are not retrieved" in state["instrument_identity_report"]
    assert len(official_metadata) == 2
    assert "_ashare_identity" not in config and "_ashare_identity" not in get_config()
    assert context.resolve_instrument_identity(CANONICAL, graph.config)["company_name"] == "贵州茅台酒股份有限公司"
    assert "company_name" not in graph.config["_ashare_identity"]
    # State fallback uses the pinned snapshot and does not make a new lookup.
    state.pop("instrument_context")
    assert "贵州茅台" in context.get_instrument_context_from_state(state)
    assert len(official_metadata) == 2


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
@pytest.mark.parametrize("frontend", ["headless", "interactive"])
def test_cli_resolves_before_storage_and_retains_report_snapshot(
    tmp_path, monkeypatch, official_metadata, backend, frontend,
):
    config = settings(tmp_path, backend)
    original = deepcopy(config)
    graphs = []

    def factory(analysts, config, callbacks=None):
        graph = offline_graph(config)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(headless, "_create_graph", factory)
    monkeypatch.setattr(native, "_default_graph_factory", factory)
    output = tmp_path / "export"
    if frontend == "headless":
        summary = headless.run_headless_analysis(CODE, config=config, analysis_date=DATE, analysts="market",
                                                 output_dir=output, progress_mode="off")
        assert summary["symbol"] == CANONICAL
        assert summary["instrument_identity"]["canonical_symbol"] == CANONICAL
    else:
        result = native.run_analysis(selections=selections(), config=config, output_dir=output,
                                     progress_mode="off", flags={"save": True, "show": False, "html": True})
        assert result.final_state["company_of_interest"] == CANONICAL
    graph = graphs[0]
    assert graph.memory_log.store_decision.call_args.kwargs["ticker"] == CANONICAL
    assert len(official_metadata) == 2  # Headless, CLI and graph reused one snapshot.
    assert config == original
    assert graph.seen_configs[0]["_ashare_identity"] == graph.config["_ashare_identity"]
    assert "_ashare_identity" not in get_config()
    assert not (tmp_path / "results" / CODE).exists()
    markdown = (output / "complete_report.md").read_text(encoding="utf-8")
    html = (output / "complete_report.html").read_text(encoding="utf-8")
    assert "贵州茅台" in markdown and "Kweichow Moutai" in markdown and "贵州茅台" in html
    assert (output / "0_identity" / "identity.md").exists()
    if backend == "sqlite":
        archive = SQLiteStorage(tmp_path / "results" / "runs.sqlite3")
        run_id = graph.last_run_id
        assert archive.get_run(run_id)["ticker"] == CANONICAL
        for artifact in ("report_state.json", "full_state.json"):
            saved = json.loads(archive.read_artifact(run_id, artifact))
            assert saved["instrument_identity"]["chinese_short_name"] == "贵州茅台"
            assert saved["instrument_identity_report"]
        exported = archive.export_run(run_id, tmp_path / "archive-export")
        assert "贵州茅台" in (exported / "reports" / "complete_report.md").read_text(encoding="utf-8")
    else:
        saved = json.loads((tmp_path / "results" / CANONICAL / "TradingAgentsStrategy_logs"
                            / f"full_states_log_{DATE}.json").read_text(encoding="utf-8"))
        assert saved["instrument_identity"]["canonical_symbol"] == CANONICAL


def test_sdk_propagate_resolves_before_archive_and_refreshes_new_run(tmp_path, monkeypatch, official_metadata):
    graph = offline_graph(settings(tmp_path, "sqlite", ashare_identity_cache_ttl=0))
    first, signal = graph.propagate(CODE, DATE)
    first_id = graph.last_run_id
    assert signal == "Hold"
    assert first["company_of_interest"] == CANONICAL
    assert len(official_metadata) == 2
    second, _ = graph.propagate(CANONICAL, DATE)
    assert len(official_metadata) == 3  # New SDK run checks identity again; nested state does not.
    archive = SQLiteStorage(tmp_path / "results" / "runs.sqlite3")
    assert graph.last_run_id != first_id
    assert archive.get_run(first_id)["ticker"] == CANONICAL
    assert archive.get_run(graph.last_run_id)["ticker"] == CANONICAL
    assert second["instrument_identity"]["canonical_symbol"] == CANONICAL


def test_direct_execution_uses_canonical_for_checkpoint_and_memory(tmp_path, official_metadata):
    graph = offline_graph(settings(tmp_path))
    graph.begin_checkpoint = Mock(return_value=None)
    graph.clear_checkpoint_on_success = Mock()
    state = execute_graph(AnalysisRequest(CODE, DATE, "stock"), graph, callbacks=[],
                          observer=AnalysisObserver(lambda *a: None, lambda: None))
    assert state["company_of_interest"] == CANONICAL
    graph.begin_checkpoint.assert_called_once_with(CANONICAL, DATE, "stock", None)
    graph.clear_checkpoint_on_success.assert_called_once_with(CANONICAL, DATE, "stock", None)
    assert graph.memory_log.store_decision.call_args.kwargs["ticker"] == CANONICAL


def test_checkpoint_signature_excludes_observation_time_but_tracks_identity(tmp_path, official_metadata):
    graph = offline_graph(settings(tmp_path))
    graph.create_run_state(CODE, DATE)
    before = graph._run_signature("stock")
    graph.config["_ashare_identity"]["retrieved_at"] = "2099-01-01T00:00:00Z"
    graph.config.update(ashare_identity_timeout=0.5, ashare_identity_cache_ttl=2,
                        data_cache_dir="/other/cache", storage_backend="sqlite")
    assert graph._run_signature("stock") == before
    graph.config["_ashare_identity"]["english_name"] = "Changed official company name"
    assert graph._run_signature("stock") != before
    graph.config["_ashare_identity"]["english_name"] = "Kweichow Moutai Co., Ltd."
    graph.config["ashare_identity_enabled"] = False
    assert graph._run_signature("stock") != before


@pytest.mark.parametrize("ticker,asset_type", [("NVDA", "stock"), ("0700.HK", "stock"),
                                              ("7203.T", "stock"), ("BTC-USD", "crypto")])
def test_mixed_market_runs_keep_legacy_identity_and_vendor_chain(
    tmp_path, monkeypatch, official_metadata, ticker, asset_type,
):
    config = settings(tmp_path)
    graph = offline_graph(config)
    graph.create_run_state(CODE, DATE)
    yahoo = Mock(return_value={"longName": "Legacy vendor name", "sector": "Technology"})
    monkeypatch.setattr(context, "get_company_profile", yahoo)
    state = graph.create_run_state(ticker, DATE, asset_type)
    assert state["company_of_interest"] == ticker
    assert "Legacy vendor name" in state["instrument_context"]
    assert "instrument_identity" not in state and "_ashare_identity" not in graph.config
    assert graph.config["data_vendors"] == config["data_vendors"]
    assert graph.config["tool_vendors"] == config["tool_vendors"]
    assert len(official_metadata) == 2
    yahoo.assert_called_once_with(ticker)


def test_explicit_non_a_symbol_preserves_legacy_yahoo_identity(tmp_path, monkeypatch, official_metadata):
    graph = offline_graph(settings(tmp_path))
    yahoo = Mock(return_value={"longName": "SSE Composite Index", "quoteType": "INDEX"})
    monkeypatch.setattr(context, "get_company_profile", yahoo)
    before = graph._run_signature("stock")
    state = graph.create_run_state("000001.SS", DATE)
    assert state["company_of_interest"] == "000001.SS"
    assert "instrument_identity" not in state
    assert "instrument_identity_report" not in state
    assert "Ping An" not in state["instrument_context"]
    assert "SSE Composite Index" in state["instrument_context"]
    yahoo.assert_called_once_with("000001.SS")
    assert graph._run_signature("stock") == before


def test_disabled_bare_code_fails_before_any_storage_or_graph(tmp_path, monkeypatch, official_metadata):
    config = settings(tmp_path, ashare_identity_enabled=False)
    create = Mock(side_effect=AssertionError("created a run before validation"))
    monkeypatch.setattr(headless, "create_run", create)
    monkeypatch.setattr(native, "create_run", create)
    with pytest.raises(ValueError, match="Bare six-digit"):
        headless.run_headless_analysis(CODE, config=config, analysis_date=DATE)
    with pytest.raises(ValueError, match="Bare six-digit"):
        native.run_analysis(config=config, selections=selections(), interactive=False)
    assert not (tmp_path / "results").exists()
    assert official_metadata == []


def test_two_graphs_do_not_read_each_others_global_identity(tmp_path, monkeypatch, official_metadata):
    first = offline_graph(settings(tmp_path))
    first.create_run_state(CODE, DATE)
    stale = deepcopy(first.config["_ashare_identity"])
    stale["chinese_short_name"] = "Incorrect global name"
    set_config({"_ashare_identity": stale})
    assert "Incorrect global name" not in first.resolve_instrument_context(CANONICAL, trade_date=DATE)
    assert "贵州茅台" in first.resolve_instrument_context(CANONICAL, trade_date=DATE)


def test_checkpoint_resume_uses_stable_snapshot_and_changed_names_start_fresh(tmp_path, official_metadata):
    config = settings(tmp_path, checkpoint_enabled=True)
    first = offline_graph(config)
    initial = first.create_run_state(CODE, DATE)
    tid = first.begin_checkpoint(CANONICAL, DATE)
    try:
        first.graph.invoke(initial, config={"configurable": {"thread_id": tid}})
    finally:
        first.end_checkpoint()

    later_config = deepcopy(first.config)
    later_config["_ashare_identity"]["retrieved_at"] = "2026-10-08T12:00:00Z"
    second = offline_graph(later_config)
    second.create_run_state(CANONICAL, DATE)
    try:
        assert second.begin_checkpoint(CANONICAL, DATE) == tid
        assert second._resuming is True
    finally:
        second.end_checkpoint()

    changed_config = deepcopy(later_config)
    changed_config["_ashare_identity"]["english_name"] = "New official issuer name"
    third = offline_graph(changed_config)
    third.create_run_state(CANONICAL, DATE)
    try:
        assert third.begin_checkpoint(CANONICAL, DATE) != tid
        assert third._resuming is False
    finally:
        third.end_checkpoint()


def test_sdk_save_reports_uses_verified_state_symbol(tmp_path, official_metadata):
    graph = offline_graph(settings(tmp_path))
    state = graph.create_run_state(CODE, DATE)
    report = graph.save_reports(state, CODE, html=False)
    assert report.parent.name.startswith(CANONICAL + "_")
    assert report.read_text(encoding="utf-8").startswith(f"# Trading Analysis Report: {CANONICAL}")


def test_non_a_checkpoint_signature_ignores_all_identity_configuration(tmp_path):
    graph = offline_graph(settings(tmp_path))
    for key in ("ashare_identity_enabled", "ashare_identity_timeout", "ashare_identity_cache_ttl"):
        graph.config.pop(key, None)
    baseline = graph._run_signature("stock")
    graph.config.update(ashare_identity_enabled=True, ashare_identity_timeout=3,
                        ashare_identity_cache_ttl=300)
    assert graph._run_signature("stock") == baseline
    graph.config["ashare_identity_enabled"] = False
    assert graph._run_signature("stock") == baseline


def test_unavailable_explicit_identity_is_unknown_without_yahoo(tmp_path, monkeypatch, official_metadata):
    import requests

    monkeypatch.setattr(identity, "_get_json", Mock(side_effect=requests.Timeout))
    graph = offline_graph(settings(tmp_path))
    state = graph.create_run_state(CANONICAL, DATE)
    assert state["instrument_identity"]["status"] == "unavailable"
    assert state["instrument_identity"]["chinese_short_name"] is None
    assert "identity unavailable" in state["instrument_context"]
    assert "贵州茅台" not in state["instrument_context"]


def test_repeated_direct_execution_refreshes_identity_after_lifecycle_end(
    tmp_path, monkeypatch, official_metadata,
):
    # A CLI-provided snapshot is reused on the first lifecycle even with no
    # provider cache, and remains inspectable for reports after it completes.
    _, config = identity.prepare_instrument(
        CANONICAL, "stock", settings(tmp_path, ashare_identity_cache_ttl=0),
    )
    graph = offline_graph(config)
    request = AnalysisRequest(CANONICAL, DATE, "stock")
    observer = AnalysisObserver(lambda *a: None, lambda: None)
    first = execute_graph(request, graph, callbacks=[], observer=observer)
    first_signature = graph._run_signature("stock")
    assert len(official_metadata) == 1
    assert graph.config["_ashare_identity"] == first["instrument_identity"]

    source = identity._get_json

    def renamed_source(*args, **kwargs):
        payload = source(*args, **kwargs)
        if args[0] == identity.SSE_URL:
            payload["result"][0]["SECURITY_ABBR_A_CN"] = "新的官方简称"
        return payload

    monkeypatch.setattr(identity, "_get_json", renamed_source)
    second = execute_graph(request, graph, callbacks=[], observer=observer)
    assert len(official_metadata) == 2
    assert second["instrument_identity"]["chinese_short_name"] == "新的官方简称"
    assert "新的官方简称" in second["instrument_context"]
    assert graph._run_signature("stock") != first_signature
    assert graph.config["_ashare_identity"] == second["instrument_identity"]
    assert first["instrument_identity"]["chinese_short_name"] == "贵州茅台"
    assert config["_ashare_identity"]["chinese_short_name"] == "贵州茅台"
