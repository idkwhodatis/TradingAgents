"""Real SDK/backtest orchestration with deterministic, offline provider boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from tradingagents.backtest import run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog, settlement

DATE = "2026-01-05"
DECISION = "Rating: Buy\nOffline decision"
ALIASES = [
    ("RY.TO", "RY.TO"),
    ("ry.to", "RY.TO"),
    ("BBD.B.TO", "BBD-B.TO"),
    ("TSX:RY", "RY.TO"),
    ("TSXV:RCK", "RCK.V"),
    ("rck.v", "RCK.V"),
    ("TSX:ENB.PR.V", "ENB-PV.TO"),
    ("ENB.PR.V.TO", "ENB-PV.TO"),
    (" ry.to+ ", "RY.TO"),
]


@pytest.fixture
def offline_graph(monkeypatch):
    calls = []

    def initialize(self, selected_analysts=None, config=None, **kwargs):
        # Avoid constructing paid clients. Everything after construction, including
        # run state, memory step, state files and decision persistence, is real.
        self.config = {**deepcopy(DEFAULT_CONFIG), **(config or {}),
                       "storage_backend": "filesystem", "ashare_identity_enabled": False}
        self.selected_analysts = selected_analysts or []
        self.memory_log = TradingMemoryLog(self.config)
        self.reflector = Mock(reflect_on_final_decision=Mock(return_value="Offline reflection"))
        self.propagator = Propagator()
        self.debug = False
        self._checkpointer_ctx = None
        self._resuming = False

        def invoke(state, **kwargs):
            calls.append((state["company_of_interest"], state["trade_date"]))
            return {**state, **self._memory_step(state), "final_trade_decision": DECISION,
                    "trader_investment_plan": "", "investment_plan": ""}

        self.graph = Mock(invoke=invoke)

    monkeypatch.setattr(TradingAgentsGraph, "__init__", initialize)
    monkeypatch.setattr(TradingAgentsGraph, "resolve_instrument_context", lambda *args: "Offline identity")
    returns = Mock(return_value=(0.05, 0.02, 5, "2026-01-12"))
    monkeypatch.setattr(settlement, "fetch_returns", returns)
    return calls, returns


def config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live.md")}


def entries(result):
    return TradingMemoryLog({"memory_log_path": str(result.log_path)}).load_entries()


@pytest.mark.parametrize("raw,canonical", ALIASES)
def test_backtest_settles_and_resumes_actual_sdk_decisions(tmp_path, offline_graph, raw, canonical):
    tickers, dates, seen = [raw], [DATE], []
    first = run_backtest(tickers, dates, config(tmp_path), run_id="same-run",
                         progress=lambda *args: seen.append(args))
    again = run_backtest(tickers, dates, config(tmp_path), run_id="same-run")
    canonical_again = run_backtest([canonical], dates, config(tmp_path), run_id="same-run")

    assert first.cells_run == 1 and first.skipped == 0
    assert again.cells_run == canonical_again.cells_run == 0
    assert again.skipped == canonical_again.skipped == 1
    assert not first.failures and not first.settlement_failures
    assert [(e["ticker"], e["pending"]) for e in entries(first)] == [(canonical, False)]
    assert summarize(first).resolved == 1 and summarize(first).pending == 0
    assert offline_graph[0] == [(canonical, DATE)]
    assert offline_graph[1].call_count == 1
    assert seen == [(1, 1, raw, DATE)]
    assert tickers == [raw] and dates == [DATE]
    assert not (tmp_path / "live.md").exists()


@pytest.mark.parametrize("raw,canonical", ALIASES)
def test_sdk_settlement_accepts_same_input_as_propagate(tmp_path, offline_graph, raw, canonical):
    graph = TradingAgentsGraph(config=config(tmp_path))
    state, signal = graph.propagate(raw, DATE)
    result = graph.settle_pending(raw)
    assert state["company_of_interest"] == canonical and signal == "Buy"
    assert result.settled == [(canonical, DATE)] and not result.failed
    assert not graph.memory_log.get_pending_entries()
    assert graph.settle_pending(canonical).settled == []
    assert offline_graph[1].call_count == 1


def test_aliases_are_one_cell_but_us_and_venture_listings_stay_distinct(tmp_path, offline_graph):
    tickers = ["ry.to", "TSX:RY", "RY.TO", "RY", "RY.V"]
    seen = []
    result = run_backtest(tickers, [DATE, DATE], config(tmp_path), run_id="aliases",
                          progress=lambda *args: seen.append(args))
    assert result.cells_run == 3 and result.skipped == 0
    assert offline_graph[0] == [("RY.TO", DATE), ("RY", DATE), ("RY.V", DATE)]
    assert [args[2] for args in seen] == ["ry.to", "RY", "RY.V"]
    assert all(args[1] == 3 for args in seen)
    assert summarize(result).resolved == 3
    assert tickers == ["ry.to", "TSX:RY", "RY.TO", "RY", "RY.V"]


@pytest.mark.parametrize("legacy,requested", [
    ("ry.to", "RY.TO"), ("BBD.B.TO", "TSX:BBD.B"), ("TSX:RY", "ry.to"),
])
@pytest.mark.parametrize("pending", [True, False])
def test_resume_preserves_legacy_log_keys(tmp_path, offline_graph, legacy, requested, pending):
    cfg = config(tmp_path)
    path = tmp_path / "results" / "backtest" / "legacy" / "trading_memory.md"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    log.store_decision(legacy, DATE, DECISION)
    if not pending:
        log.update_with_outcome(legacy, DATE, 0.05, 0.02, 5, "Prior reflection", "2026-01-12")
    result = run_backtest([requested], [DATE], cfg, run_id="legacy")
    assert result.cells_run == 0 and result.skipped == 1
    assert offline_graph[0] == []
    assert [(e["ticker"], e["pending"]) for e in entries(result)] == [(legacy, False)]
    assert summarize(result).resolved == 1
    assert offline_graph[1].call_count == int(pending)


@pytest.mark.parametrize("entrypoint", ["single", "all"])
def test_settlement_updates_legacy_and_canonical_records_by_their_stored_keys(
    tmp_path, offline_graph, entrypoint,
):
    graph = TradingAgentsGraph(config=config(tmp_path))
    rows = [("ry.to", DATE), ("TSX:RY", "2026-01-06"), ("RY.TO", "2026-01-07")]
    for ticker, date in rows:
        graph.memory_log.store_decision(ticker, date, DECISION)
    result = graph.settle_pending("TSX:RY") if entrypoint == "single" else graph.settle_all_pending()
    assert result.settled == rows and not result.failed
    assert [(e["ticker"], e["date"]) for e in graph.memory_log.load_entries()] == rows
    assert not graph.memory_log.get_pending_entries()
    assert [call.args[0] for call in offline_graph[1].call_args_list] == ["RY.TO"] * 3


def test_settlement_failure_identifies_persisted_record_and_can_retry(tmp_path, offline_graph):
    graph = TradingAgentsGraph(config=config(tmp_path))
    graph.memory_log.store_decision("BBD.B.TO", DATE, DECISION)
    offline_graph[1].side_effect = RuntimeError("prices unavailable")
    failed = graph.settle_pending("TSX:BBD.B")
    assert not failed.settled
    assert [item[:2] for item in failed.failed] == [("BBD.B.TO", DATE)]
    assert len(graph.memory_log.get_pending_entries()) == 1
    offline_graph[1].side_effect = None
    assert graph.settle_pending("BBD-B.TO").settled == [("BBD.B.TO", DATE)]


@pytest.mark.parametrize("ticker", ["RY", "nvda", "BRK.B", "600519.SS", "000001.SZ", "BTCUSD"])
def test_non_canadian_run_keys_are_unchanged(tmp_path, offline_graph, ticker):
    first = run_backtest([ticker], [DATE], config(tmp_path), run_id="other-markets")
    again = run_backtest([ticker], [DATE], config(tmp_path), run_id="other-markets")
    assert first.cells_run == 1 and again.cells_run == 0 and again.skipped == 1
    assert offline_graph[0] == [(ticker, DATE)]
    assert [(e["ticker"], e["pending"]) for e in entries(first)] == [(ticker, False)]


def test_invalid_alias_is_reported_without_aborting_unrelated_cells(tmp_path, offline_graph):
    result = run_backtest(["TSXV:RY.TO", "TSX:RY"], [DATE], config(tmp_path), run_id="bad-alias")
    assert result.cells_run == 1
    assert [item[:2] for item in result.failures] == [("TSXV:RY.TO", DATE)]
    assert summarize(result).resolved == 1


@pytest.mark.parametrize("invalid,valid", [
    ("TSX:RY+", "RY.TO"), ("TSXV:RCK+", "RCK.V"), ("TSX:ENB.PR.V+", "ENB-PV.TO"),
])
@pytest.mark.parametrize("invalid_first", [True, False])
def test_invalid_prefixed_qualifier_cannot_hide_a_valid_alias(
    tmp_path, offline_graph, invalid, valid, invalid_first,
):
    tickers = [invalid, valid] if invalid_first else [valid, invalid]
    first = run_backtest(tickers, [DATE], config(tmp_path), run_id="invalid-qualifier")
    again = run_backtest(tickers, [DATE], config(tmp_path), run_id="invalid-qualifier")
    assert first.cells_run == 1 and first.skipped == 0
    assert again.cells_run == 0 and again.skipped == 1
    assert [item[:2] for item in first.failures] == [(invalid, DATE)]
    assert [item[:2] for item in again.failures] == [(invalid, DATE)]
    assert offline_graph[0] == [(valid, DATE)]
    assert summarize(first).resolved == 1


def test_backtest_failure_and_progress_preserve_caller_labels(tmp_path, offline_graph, monkeypatch):
    seen = []
    monkeypatch.setattr(TradingAgentsGraph, "propagate", Mock(side_effect=RuntimeError("analysis failed")))
    monkeypatch.setattr(TradingAgentsGraph, "settle_pending", Mock(side_effect=RuntimeError("settlement failed")))
    result = run_backtest(["TSX:RY"], [DATE], config(tmp_path), run_id="failure",
                          progress=lambda *args: seen.append(args))
    assert result.failures == [("TSX:RY", DATE, "analysis failed")]
    assert result.settlement_failures == [("TSX:RY", "settlement failed")]
    assert seen == [(1, 1, "TSX:RY", DATE)]
