"""Real SDK/backtest orchestration with deterministic, offline provider boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from tradingagents.backtest import run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog, settlement
from tradingagents.portfolio import PortfolioContext

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


@pytest.mark.parametrize("invalid", ["ENB.PF.V.TO", "ENB.PR.V", "TSXV:RY.TO"])
@pytest.mark.parametrize("entrypoint", ["all", "propagate"])
def test_rejected_legacy_tickers_do_not_starve_other_pending_decisions(
    tmp_path, offline_graph, invalid, entrypoint,
):
    graph = TradingAgentsGraph(config=config(tmp_path))
    log = graph.memory_log
    failed_rows = [(invalid, DATE), (invalid, "2026-01-06")]
    valid_rows = [("AAPL", DATE), ("ry.to", DATE)]
    for ticker, date in failed_rows + valid_rows:
        log.store_decision(ticker, date, DECISION)

    if entrypoint == "all":
        result = graph.settle_all_pending()
        assert result.settled == valid_rows
        assert [item[:2] for item in result.failed] == failed_rows
        assert all("prices unavailable:" in item[2] for item in result.failed)
    else:
        state, _ = graph.propagate("AAPL", "2026-02-01")
        assert "Past analyses of AAPL" in state["past_context"]
        assert "Offline reflection" in state["past_context"]
        assert state["memory_note"].startswith("2 past decision(s) could not be settled")
    assert [(e["ticker"], e["date"]) for e in log.load_entries() if not e["pending"]] == valid_rows
    # The single-ticker SDK reports each rejected record too; it does not raise
    # or rewrite it, and those failures cannot prevent later valid runs.
    again = graph.settle_pending(invalid)
    assert not again.settled
    assert [item[:2] for item in again.failed] == failed_rows
    assert [call.args[0] for call in offline_graph[1].call_args_list] == ["AAPL", "RY.TO"]
    assert [(e["ticker"], e["date"]) for e in log.load_entries()[:4]] == failed_rows + valid_rows


def test_invalid_benchmark_is_reported_for_each_record_and_can_retry(tmp_path, offline_graph):
    graph = TradingAgentsGraph(config={**config(tmp_path), "benchmark_ticker": "ENB.PF.V.TO"})
    rows = [("ry.to", DATE), ("RY.TO", "2026-01-06"), ("AAPL", DATE)]
    for ticker, date in rows:
        graph.memory_log.store_decision(ticker, date, DECISION)
    result = graph.settle_all_pending()
    assert not result.settled
    assert [item[:2] for item in result.failed] == rows
    offline_graph[1].assert_not_called()
    graph.config["benchmark_ticker"] = "SPY"
    retried = graph.settle_all_pending()
    assert retried.settled == rows and not retried.failed


@pytest.mark.parametrize("legacy,canonical", ALIASES)
def test_sdk_keeps_legacy_same_listing_decisions_and_reflections(
    tmp_path, offline_graph, legacy, canonical,
):
    graph = TradingAgentsGraph(config=config(tmp_path))
    log = graph.memory_log
    log.store_decision(legacy, DATE, "Rating: Buy\nPrior Canadian decision")
    log.update_with_outcome(legacy, DATE, 0.1, 0.05, 5, "Prior Canadian lesson", "2026-01-12")
    # Fill the cross-ticker limit, so a wrongly classified lesson disappears.
    for day in ["2026-01-06", "2026-01-07", "2026-01-08"]:
        log.store_decision("AAPL", day, DECISION)
        log.update_with_outcome("AAPL", day, 0.1, 0.05, 5, "Other lesson", "2026-01-15")
    log.store_decision(canonical, "2026-01-20", "Rating: Buy\nFuture Canadian decision")
    log.update_with_outcome(canonical, "2026-01-20", 0.1, 0.05, 5, "Future lesson", "2026-03-01")
    before = log.load_entries()

    # Public memory reads and propagation use the same key on both sides.
    direct = log.get_past_context(legacy, as_of="2026-02-01")
    state, _ = graph.propagate(legacy, "2026-02-01")
    assert state["company_of_interest"] == canonical
    assert f"Past analyses of {canonical}" in state["past_context"]
    for context in [direct, state["past_context"]]:
        same, cross = context.split("Recent cross-ticker lessons:")
        assert "Prior Canadian decision" in same and "Prior Canadian lesson" in same
        assert "Prior Canadian lesson" not in cross
        assert cross.count("Other lesson") == 3
        assert "Future" not in context
    assert log.load_entries()[:len(before)] == before


@pytest.mark.parametrize("stored,requested", [
    ("RY", "RY.TO"), ("RY.V", "RY.TO"), ("RY.TO", "RY"),
    ("nvda", "NVDA"), ("BRK.B", "BRK-B"), ("BTCUSD", "BTC-USD"),
    ("600519.SH", "600519.SS"), ("TSX:RY+", "RY.TO"),
])
def test_memory_does_not_merge_other_listing_or_non_canadian_keys(tmp_path, stored, requested):
    log = TradingMemoryLog(config(tmp_path))
    log.store_decision(stored, DATE, "Rating: Buy\nDistinct decision")
    log.update_with_outcome(stored, DATE, 0.1, 0.05, 5, "Distinct lesson", "2026-01-12")
    before = log.load_entries()
    context = log.get_past_context(requested, as_of="2026-02-01")
    assert "Past analyses" not in context
    assert "Distinct decision" not in context and "Distinct lesson" in context
    assert "Distinct decision" in log.get_past_context(stored, as_of="2026-02-01")
    assert log.load_entries() == before


@pytest.mark.parametrize("raw,canonical", [
    ("ry.to+", "RY.TO"), ("RCK.V+", "RCK.V"), ("BBD.B.TO+", "BBD-B.TO"),
    (" ENB.PR.V.TO+ ", "ENB-PV.TO"),
])
@pytest.mark.parametrize("canonical_holding", [False, True])
def test_sdk_qualifier_keeps_held_position_without_mutating_book(
    tmp_path, offline_graph, raw, canonical, canonical_holding,
):
    graph = TradingAgentsGraph(config=config(tmp_path))
    holding = canonical if canonical_holding else raw
    book = PortfolioContext.model_validate({"currency": "CAD", "positions": [
        {"ticker": holding, "quantity": 120, "average_price": 150},
        {"ticker": "RY", "quantity": 7},
        {"ticker": "TSX:RY+", "quantity": 3},
    ]})
    before, fingerprint = book.model_dump(), book.fingerprint()
    state, _ = graph.propagate(raw, "2026-02-01", portfolio=book)
    assert f"Current position in {canonical}: 120 units, average price 150.00" in state["portfolio_context"]
    assert "Other positions: RY 7, TSX:RY+ 3" in state["portfolio_context"]
    assert book.position_in(canonical) is book.positions[0]
    assert book.position_in(raw) is book.positions[0]
    assert book.position_in("RY") is book.positions[1]
    assert book.model_dump() == before and book.fingerprint() == fingerprint


@pytest.mark.parametrize("raw,other", [
    (" aapl ", "AAPL+"), ("btc-usd", "BTCUSD"), ("brk.b", "BRK-B"),
    ("600519.sh", "600519.SS"), ("ry", "RY.TO"),
])
def test_portfolio_keeps_existing_non_canadian_comparison_rules(raw, other):
    book = PortfolioContext.model_validate({"positions": [{"ticker": raw, "quantity": 7}]})
    assert book.position_in(raw.strip().upper()) is book.positions[0]
    assert book.position_in(other) is None


@pytest.mark.parametrize("invalid", ["ENB.PF.V.TO", "ENB.PR.V", "TSXV:RY.TO", "TSX:RY+"])
def test_legacy_comparison_does_not_relax_new_sdk_input_validation(tmp_path, offline_graph, invalid):
    graph = TradingAgentsGraph(config=config(tmp_path))
    with pytest.raises(ValueError):
        graph.propagate(invalid, DATE)
    assert not graph.memory_log.load_entries()
    assert not offline_graph[0]
