"""Current BSE identity, provenance and lifecycle checks without network or LLMs."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import cli.headless as headless
import cli.run as native
from cli.execution import AnalysisObserver, AnalysisRequest, execute_graph
from cli.models import AnalystType
from tests.test_ashare_identity_integration import offline_graph, settings
from tradingagents.agents import context
from tradingagents.dataflows.config import get_config, run_config
from tradingagents.extensions import ashare_identity as adapter
from tradingagents.storage import SQLiteStorage

FIXTURES = Path(__file__).parent / "fixtures" / "ashare_identity"
CODE = "920819"
CANONICAL = f"{CODE}.BJ"
DATE = "2026-09-27"


@pytest.fixture(autouse=True)
def isolated_identity_cache():
    adapter._CACHE.clear()
    context._identity.cache_clear()
    yield
    adapter._CACHE.clear()
    context._identity.cache_clear()


@pytest.fixture
def bse_payload():
    return json.loads((FIXTURES / "eastmoney_920819.json").read_text(encoding="utf-8"))


@pytest.fixture
def metadata(monkeypatch, bse_payload):
    """Return recorded exact matches; all other lookups are synthetic empties."""
    calls = []
    sse = json.loads((FIXTURES / "sse_600519.json").read_text(encoding="utf-8"))
    szse = json.loads((FIXTURES / "szse_000001.json").read_text(encoding="utf-8"))

    def answer(url, params, referer, timeout):
        calls.append((url, deepcopy(params), referer, timeout))
        if url == adapter.SSE_URL:
            return deepcopy(sse if params["COMPANY_CODE"] == "600519" else {"result": []})
        if url == adapter.SZSE_URL:
            result = deepcopy(szse)
            if params["txtDMorJC"] != "000001":
                result[0]["data"] = []
            return result
        if url == adapter.YAHOO_SEARCH_URL:
            return {"quotes": []}
        assert url == adapter.BSE_URL
        return deepcopy(bse_payload if params["code"] == f"BJ{CODE}" else {"jbzl": []})

    monkeypatch.setattr(adapter, "_get_json", answer)
    monkeypatch.setattr(context, "get_company_profile", Mock(side_effect=AssertionError("unexpected Yahoo identity")))
    return calls


@pytest.mark.parametrize("ticker,requests_count", [(CODE, 3), (CANONICAL, 1), (f"{CODE}.bj", 1)])
def test_current_bse_bare_and_explicit_exact_identity(ticker, requests_count, metadata):
    config = {"data_vendors": {"news": "yfinance"}, "private_setting": "SECRET"}
    original = deepcopy(config)
    symbol, prepared = adapter.prepare_instrument(ticker, "stock", config)
    identity = adapter.identity_for(symbol, prepared)
    assert symbol == CANONICAL
    assert config == original
    assert identity["status"] == "resolved"
    assert identity["exchange"] == "BSE"
    assert identity["security_type"] == "A-share"
    assert identity["confidence"] == "verified"
    assert identity["identity_temporality"] == "current"
    assert identity["chinese_short_name"] == "颖泰生物"
    assert identity["chinese_full_name"] == "北京颖泰嘉和生物科技股份有限公司"
    assert identity["english_name"] == "Nutrichem Company Limited"
    assert "SECRET" not in json.dumps(identity)
    assert len(metadata) == requests_count
    assert metadata[-1] == (adapter.BSE_URL, {"code": f"BJ{CODE}"}, None, 5)


@pytest.mark.parametrize("code", ["920799", "920002", "920123"])
def test_other_verified_current_bse_fixture_subsets(monkeypatch, code):
    payload = json.loads((FIXTURES / f"eastmoney_{code}.json").read_text(encoding="utf-8"))
    get = Mock(return_value=payload)
    monkeypatch.setattr(adapter, "_get_json", get)
    symbol, prepared = adapter.prepare_instrument(f"{code}.BJ", "stock", {})
    identity = prepared["_ashare_identity"]
    row = payload["jbzl"][0]
    assert symbol == f"{code}.BJ"
    assert identity["status"] == "resolved"
    assert identity["chinese_short_name"] == row["SECURITY_NAME_ABBR"]
    assert identity["chinese_full_name"] == row["ORG_NAME"]
    assert identity["english_name"] == row["ORG_NAME_EN"]
    assert {alias["kind"] for alias in identity["aliases"]} == {"provider_label"}
    get.assert_called_once_with(adapter.BSE_URL, {"code": f"BJ{code}"}, None, 5)


def test_explicit_bse_does_not_depend_on_sse_or_szse(monkeypatch, metadata):
    sse = Mock(side_effect=AssertionError("explicit BSE queried SSE"))
    szse = Mock(side_effect=AssertionError("explicit BSE queried SZSE"))
    monkeypatch.setattr(adapter, "_sse", sse)
    monkeypatch.setattr(adapter, "_szse", szse)
    symbol, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
    assert symbol == CANONICAL
    assert prepared["_ashare_identity"]["status"] == "resolved"
    sse.assert_not_called()
    szse.assert_not_called()
    assert len(metadata) == 1


def test_bse_names_aliases_and_queries_keep_provider_provenance(metadata):
    symbol, prepared = adapter.prepare_instrument(CODE, "stock", {})
    identity = prepared["_ashare_identity"]
    assert identity["english_name_kind"] == "provider_label"
    assert identity["sources"] == [{"provider": "Eastmoney", "url": adapter.BSE_URL + f"?code=BJ{CODE}"}]
    assert {alias["source"] for alias in identity["aliases"]} == {"Eastmoney"}
    assert {alias["kind"] for alias in identity["aliases"]} == {"provider_label"}
    assert {alias["name"] for alias in identity["aliases"]} == {
        "颖泰生物", "北京颖泰嘉和生物科技股份有限公司", "Nutrichem Company Limited",
    }
    assert identity["retrieval_queries"]["official_announcements"] == {
        "queries": [f"颖泰生物 {CODE} 公告 site:bse.cn"], "evidence_retrieved": False,
    }
    assert identity["retrieval_queries"]["overseas_news"]["evidence_retrieved"] is False
    with run_config(prepared):
        assert adapter.news_queries(symbol) == [CANONICAL, f"颖泰生物 {CODE}",
                                                f"北京颖泰嘉和生物科技股份有限公司 {CODE}",
                                                f"Nutrichem Company Limited {CODE}"]
    rendered = adapter.render_identity(identity)
    assert "Eastmoney provider labels, not official" in rendered
    assert "does not establish historical ticker or price availability" in rendered
    assert "Official announcements are not retrieved" in rendered
    assert "independent perspectives" in rendered


@pytest.mark.parametrize("english", [None, "", "-", "null"])
def test_missing_bse_english_stays_unknown_without_translation(metadata, bse_payload, english):
    bse_payload["jbzl"][0]["ORG_NAME_EN"] = english
    _, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
    identity = prepared["_ashare_identity"]
    assert identity["status"] == "resolved"
    assert identity["english_name"] is None
    assert identity["english_name_kind"] is None
    assert all(alias["language"] != "en" for alias in identity["aliases"])
    assert "english_name unavailable; no translation inferred." in identity["coverage"]
    assert len(metadata) == 1  # No extra vendor or model translates a missing name.


@pytest.mark.parametrize("changes", [
    {"SECURITY_CODE": "920001"},
    {"SECURITY_CODE": 920819},
    {"STR_CODEA": "920001"},
    {"SECUCODE": "920819.SZ"},
    {"SECUCODE": "920819.SS"},
    {"TRADE_MARKET": "深圳证券交易所"},
    {"TRADE_MARKET": None},
    {"SECURITY_TYPE": "ETF"},
    {"SECURITY_TYPE": "指数"},
    {"SECURITY_TYPE": "全国股转系统挂牌公司"},
    {"SECURITY_TYPE": None},
])
def test_rejects_wrong_code_market_type_and_missing_eligibility(metadata, bse_payload, changes):
    bse_payload["jbzl"][0].update(changes)
    symbol, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
    identity = prepared["_ashare_identity"]
    assert symbol == CANONICAL
    assert identity["status"] == "unavailable"
    assert identity["confidence"] == "unknown"
    assert identity["security_type"] is None
    assert identity["chinese_short_name"] is None
    assert identity["english_name"] is None
    assert not identity["aliases"]
    assert not identity["sources"]


@pytest.mark.parametrize("payload", [None, [], {}, {"jbzl": None}, {"jbzl": {}}, {"jbzl": "bad"}])
def test_malformed_bse_payload_is_unavailable_and_not_cached(monkeypatch, payload):
    get = Mock(return_value=payload)
    monkeypatch.setattr(adapter, "_get_json", get)
    for _ in range(2):
        symbol, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
        assert symbol == CANONICAL
        assert prepared["_ashare_identity"]["status"] == "unavailable"
    assert get.call_count == 2
    assert not adapter._CACHE


def test_duplicate_exact_bse_matches_are_unavailable_and_not_cached(metadata, bse_payload):
    bse_payload["jbzl"].append(deepcopy(bse_payload["jbzl"][0]))
    _, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
    assert prepared["_ashare_identity"]["status"] == "unavailable"
    assert not adapter._CACHE


def test_empty_bse_response_is_a_bounded_negative_result(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    get = Mock(return_value={"jbzl": []})
    monkeypatch.setattr(adapter, "_get_json", get)
    for _ in range(2):
        symbol, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
        assert symbol == CANONICAL
        assert prepared["_ashare_identity"]["status"] == "unavailable"
        assert "Legacy codes are not automatically converted" in prepared["_ashare_identity"]["reason"]
    assert get.call_count == 1
    assert len(adapter._CACHE) == 1
    clock[0] += 61
    adapter.prepare_instrument(CANONICAL, "stock", {})
    assert get.call_count == 2


@pytest.mark.parametrize("error", [requests.Timeout("SECRET"), requests.HTTPError("SECRET"), ValueError("SECRET")])
def test_provider_failure_is_sanitized_uncached_and_not_retried(monkeypatch, error):
    get = Mock(side_effect=error)
    monkeypatch.setattr(adapter, "_get_json", get)
    for _ in range(2):
        _, prepared = adapter.prepare_instrument(CANONICAL, "stock", {})
        assert prepared["_ashare_identity"]["status"] == "unavailable"
        assert "SECRET" not in json.dumps(prepared)
    assert get.call_count == 2
    assert not adapter._CACHE


def test_bse_request_has_bounded_timeout_and_no_custom_headers_cookies(monkeypatch, bse_payload):
    response = SimpleNamespace(raise_for_status=Mock(), json=Mock(return_value=bse_payload))
    get = Mock(return_value=response)
    monkeypatch.setattr(adapter.requests, "get", get)
    _, prepared = adapter.prepare_instrument(CANONICAL, "stock", {"ashare_identity_timeout": 0.7})
    assert prepared["_ashare_identity"]["status"] == "resolved"
    get.assert_called_once_with(adapter.BSE_URL, params={"code": f"BJ{CODE}"}, headers=None, timeout=0.7)
    response.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("ticker,canonical", [("600519", "600519.SS"), ("000001", "000001.SZ"),
                                              ("600519.SS", "600519.SS"), ("000001.SZ", "000001.SZ")])
def test_bse_failure_cannot_break_existing_sse_szse_resolution(monkeypatch, metadata, ticker, canonical):
    bse = Mock(side_effect=requests.Timeout("provider down"))
    monkeypatch.setattr(adapter, "_bse", bse)
    symbol, prepared = adapter.prepare_instrument(ticker, "stock", {})
    assert symbol == canonical
    assert prepared["_ashare_identity"]["status"] == "resolved"
    bse.assert_not_called()
    assert all(call[0] != adapter.BSE_URL for call in metadata)


@pytest.mark.parametrize("failed_exchange", ["_sse", "_szse"])
def test_bare_bse_still_requires_both_official_responses(monkeypatch, metadata, failed_exchange):
    monkeypatch.setattr(adapter, failed_exchange, Mock(side_effect=requests.Timeout("SECRET")))
    with pytest.raises(ValueError, match="Cannot uniquely verify") as error:
        adapter.prepare_instrument(CODE, "stock", {})
    assert "SECRET" not in str(error.value)
    assert all(call[0] != adapter.BSE_URL for call in metadata)


def test_bare_unavailable_bse_fails_before_any_identity_is_guessed(monkeypatch, metadata):
    monkeypatch.setattr(adapter, "_bse", Mock(side_effect=requests.Timeout("SECRET")))
    with pytest.raises(ValueError, match="Legacy BSE codes are not automatically converted") as error:
        adapter.prepare_instrument(CODE, "stock", {})
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("legacy_code,unsafe_replacement", [("837023", "920023"), ("831396", "920396")])
def test_legacy_codes_do_not_use_prefix_arithmetic(metadata, legacy_code, unsafe_replacement):
    symbol, prepared = adapter.prepare_instrument(f"{legacy_code}.BJ", "stock", {})
    assert symbol == f"{legacy_code}.BJ"
    assert prepared["_ashare_identity"]["status"] == "unavailable"
    assert "Legacy codes are not automatically converted" in prepared["_ashare_identity"]["reason"]
    with pytest.raises(ValueError, match="Cannot uniquely verify"):
        adapter.prepare_instrument(legacy_code, "stock", {})
    assert not any(unsafe_replacement in str(call[1]) for call in metadata)
    assert [call[1]["code"] for call in metadata if call[0] == adapter.BSE_URL] == [f"BJ{legacy_code}"]


def test_provider_redirect_to_current_code_does_not_silently_remap_legacy(monkeypatch, bse_payload):
    get = Mock(return_value=bse_payload)
    monkeypatch.setattr(adapter, "_get_json", get)
    symbol, prepared = adapter.prepare_instrument("833819.BJ", "stock", {})
    assert symbol == "833819.BJ"
    assert prepared["_ashare_identity"]["status"] == "unavailable"
    assert prepared["_ashare_identity"]["chinese_short_name"] is None
    assert get.call_args.args[1] == {"code": "BJ833819"}


def test_bse_cache_is_exchange_scoped_copied_and_expires(metadata, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    config = {"ashare_identity_cache_ttl": 10}
    _, first = adapter.prepare_instrument(CANONICAL, "stock", config)
    first["_ashare_identity"]["aliases"].clear()
    _, second = adapter.prepare_instrument(CANONICAL, "stock", config)
    assert second["_ashare_identity"]["aliases"]
    _, other = adapter.prepare_instrument(f"{CODE}.SS", "stock", config)
    assert other["_ashare_identity"]["status"] == "not_a_share"
    assert len(metadata) == 2
    clock[0] += 11
    adapter.prepare_instrument(CANONICAL, "stock", config)
    assert len(metadata) == 3
    monkeypatch.setattr(adapter, "_bse", Mock(side_effect=requests.Timeout("SECRET")))
    clock[0] += 11
    _, expired = adapter.prepare_instrument(CANONICAL, "stock", config)
    assert expired["_ashare_identity"]["status"] == "unavailable"
    assert expired["_ashare_identity"]["chinese_short_name"] is None


@pytest.mark.parametrize("ticker,asset_type", [("NVDA", "stock"), ("0700.HK", "stock"),
                                              ("BTC-USD", "crypto"), (CODE, "crypto")])
def test_unrelated_markets_make_no_new_requests(monkeypatch, ticker, asset_type):
    get = Mock(side_effect=AssertionError("unrelated market request"))
    monkeypatch.setattr(adapter, "_get_json", get)
    assert adapter.prepare_instrument(ticker, asset_type, {"_ashare_identity": {"stale": True}}) == (ticker, {})
    get.assert_not_called()


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
@pytest.mark.parametrize("frontend", ["headless", "interactive"])
def test_bse_cli_paths_and_saved_reports_use_one_canonical_snapshot(
    tmp_path, monkeypatch, metadata, backend, frontend,
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
        summary = headless.run_headless_analysis(CODE, config=config, analysis_date=DATE,
                                                 analysts="market", output_dir=output, progress_mode="off")
        assert summary["symbol"] == CANONICAL
        assert summary["instrument_identity"]["english_name_kind"] == "provider_label"
    else:
        result = native.run_analysis(
            selections={"ticker": CODE, "analysis_date": DATE, "asset_type": "stock", "analysts": [AnalystType.MARKET]},
            config=config, output_dir=output, progress_mode="off", flags={"save": True, "show": False, "html": True},
        )
        assert result.final_state["company_of_interest"] == CANONICAL
    graph = graphs[0]
    assert config == original
    assert len(metadata) == 3
    assert graph.memory_log.store_decision.call_args.kwargs["ticker"] == CANONICAL
    assert graph.seen_configs[0]["_ashare_identity"] == graph.config["_ashare_identity"]
    assert "_ashare_identity" not in get_config()
    assert not (tmp_path / "results" / CODE).exists()
    for filename in ("complete_report.md", "complete_report.html"):
        report = (output / filename).read_text(encoding="utf-8")
        assert "颖泰生物" in report and "Nutrichem Company Limited" in report
        assert "Eastmoney provider labels" in report
        assert "historical ticker or price availability" in report
    assert (output / "0_identity" / "identity.md").exists()
    if backend == "sqlite":
        archive = SQLiteStorage(tmp_path / "results" / "runs.sqlite3")
        assert archive.get_run(graph.last_run_id)["ticker"] == CANONICAL
        for artifact in ("report_state.json", "full_state.json"):
            saved = json.loads(archive.read_artifact(graph.last_run_id, artifact))
            assert saved["instrument_identity"]["canonical_symbol"] == CANONICAL
            assert saved["instrument_identity"]["sources"][0]["provider"] == "Eastmoney"
    else:
        saved = json.loads((tmp_path / "results" / CANONICAL / "TradingAgentsStrategy_logs"
                            / f"full_states_log_{DATE}.json").read_text(encoding="utf-8"))
        assert saved["instrument_identity"]["canonical_symbol"] == CANONICAL


@pytest.mark.parametrize("frontend", ["headless", "interactive"])
def test_unverified_bare_bse_stops_cli_before_storage(tmp_path, monkeypatch, metadata, frontend):
    config = settings(tmp_path)
    create = Mock(side_effect=AssertionError("created a run before identity validation"))
    monkeypatch.setattr(headless, "create_run", create)
    monkeypatch.setattr(native, "create_run", create)
    with pytest.raises(ValueError, match="Legacy BSE codes are not automatically converted"):
        if frontend == "headless":
            headless.run_headless_analysis("837023", config=config, analysis_date=DATE,
                                           analysts="market", progress_mode="off")
        else:
            native.run_analysis(
                selections={"ticker": "837023", "analysis_date": DATE, "asset_type": "stock",
                            "analysts": [AnalystType.MARKET]},
                config=config, progress_mode="off",
            )
    create.assert_not_called()
    assert not (tmp_path / "results").exists()


def test_bse_checkpoint_and_context_preserve_current_provider_identity(tmp_path, metadata):
    graph = offline_graph(settings(tmp_path))
    graph.begin_checkpoint = Mock(return_value=None)
    graph.clear_checkpoint_on_success = Mock()
    state = execute_graph(AnalysisRequest(CODE, "2020-01-02", "stock"), graph, callbacks=[],
                          observer=AnalysisObserver(lambda *a: None, lambda: None))
    assert state["company_of_interest"] == CANONICAL
    assert "not point-in-time" in state["instrument_context"]
    assert "does not establish historical ticker or price availability" in state["instrument_context"]
    assert state["instrument_identity"]["identity_temporality"] == "current"
    graph.begin_checkpoint.assert_called_once_with(CANONICAL, "2020-01-02", "stock", None)
    graph.clear_checkpoint_on_success.assert_called_once_with(CANONICAL, "2020-01-02", "stock", None)
    assert graph.memory_log.store_decision.call_args.kwargs["ticker"] == CANONICAL
    signature = graph._run_signature("stock")
    graph.config["_ashare_identity"]["retrieved_at"] = "2099-01-01T00:00:00Z"
    assert graph._run_signature("stock") == signature
    graph.config["_ashare_identity"]["english_name"] = "Changed provider name"
    assert graph._run_signature("stock") != signature
    assert len(metadata) == 3
