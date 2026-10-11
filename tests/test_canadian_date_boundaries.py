"""Accepted Canadian SDK aliases share exchange-local date validation."""

from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from tradingagents.backtest import run_backtest
from tradingagents.dataflows import date_window
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.extensions import canadian_market
from tradingagents.graph import trading_graph
from tradingagents.graph.propagation import Propagator
from tradingagents.memory import TradingMemoryLog, settlement

ALIASES = [
    ("RY.TO", "RY.TO"), (" ry.to ", "RY.TO"), ("RY.TO+", "RY.TO"),
    (" ry.to+ ", "RY.TO"), ("TSX:RY", "RY.TO"),
    ("TSXV:RCK", "RCK.V"), (" rck.v+ ", "RCK.V"),
    ("BBD.B.TO+", "BBD-B.TO"), ("ENB.PR.V.TO+", "ENB-PV.TO"),
]


@pytest.fixture(params=[
    ("2026-01-02T02:00:00+00:00", "2026-01-02", "2026-01-01"),
    ("2026-07-02T03:59:00+00:00", "2026-07-02", "2026-07-01"),
    ("2026-07-02T04:00:00+00:00", "2026-07-02", "2026-07-02"),
    # A host west of Toronto can still be on the previous calendar day.
    ("2026-01-02T06:00:00+00:00", "2026-01-01", "2026-01-02"),
], ids=["winter", "summer-before-midnight", "summer-midnight", "host-behind"])
def dates(request, monkeypatch):
    instant, host_today, toronto_today = request.param

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(instant).astimezone(tz)

    monkeypatch.setattr(canadian_market, "datetime", FixedDatetime)
    monkeypatch.setattr(date_window, "get_current_date", lambda: host_today)
    monkeypatch.setattr(trading_graph, "get_current_date", lambda: host_today)
    return host_today, toronto_today


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    calls = []
    config = {"results_dir": str(tmp_path / "results"),
              "data_cache_dir": str(tmp_path / "cache"),
              "memory_log_path": str(tmp_path / "memory.md")}

    def initialize(self, selected_analysts=None, config=None, **kwargs):
        self.config = {**deepcopy(DEFAULT_CONFIG), **(config or {}),
                       "checkpoint_enabled": False, "storage_backend": "filesystem",
                       "ashare_identity_enabled": False}
        self.selected_analysts = selected_analysts or []
        self.propagator = Propagator()
        self.memory_log = TradingMemoryLog(self.config)
        self.reflector = Mock()
        self.debug = False
        self._checkpointer_ctx = None
        self._resuming = False

        def invoke(state, **kwargs):
            calls.append((state["company_of_interest"], state["trade_date"]))
            return {**state, "investment_plan": "", "trader_investment_plan": "",
                    "final_trade_decision": "Rating: Hold", "final_rating": "Hold"}

        self.graph = Mock(invoke=invoke)

    # Keep propagation, normalization, persistence and backtest orchestration
    # real; no paid clients or network requests are needed for date validation.
    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "__init__", initialize)
    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "resolve_instrument_context",
                        lambda *args: "Offline identity")
    monkeypatch.setattr(settlement, "fetch_returns", lambda *args, **kwargs: (None,) * 4)
    return trading_graph.TradingAgentsGraph(config=config), calls, config


@pytest.mark.parametrize("raw,canonical", ALIASES)
def test_public_sdk_uses_same_toronto_date_for_every_accepted_alias(sdk, dates, raw, canonical):
    graph, calls, _ = sdk
    _, today = dates
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
    with pytest.raises(ValueError, match="future"):
        graph.propagate(raw, tomorrow)
    assert calls == []
    assert graph.memory_log.load_entries() == []

    state, signal = graph.propagate(raw, today)
    assert signal == "Hold"
    assert state["company_of_interest"] == canonical
    assert calls == [(canonical, today)]
    assert [(e["ticker"], e["date"]) for e in graph.memory_log.load_entries()] == [(canonical, today)]
    assert not date_window.is_historical(today, raw)
    assert graph._memory_as_of(today, raw) is None
    assert date_window.withhold_undated_statements(today, raw, "Balance Sheet") is None


@pytest.mark.parametrize("raw,canonical", [(" ry.to+ ", "RY.TO"), ("rck.v+", "RCK.V")])
def test_backtest_rejects_future_alias_cell_and_keeps_valid_day(sdk, dates, raw, canonical):
    _, calls, config = sdk
    _, today = dates
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
    result = run_backtest([raw], [tomorrow, today], config, run_id="exchange-date")
    assert result.cells_run == 1 and result.skipped == 0
    assert [item[:2] for item in result.failures] == [(raw, tomorrow)]
    assert "future" in result.failures[0][2]
    assert calls == [(canonical, today)]


@pytest.mark.parametrize("ticker", ["AAPL", "aapl", "600519.SH", "000001.SZ", "0700.HK", "BTCUSD"])
def test_other_market_sdk_dates_keep_host_date_convention(sdk, dates, ticker):
    graph, calls, _ = sdk
    host_today, _ = dates
    tomorrow = (datetime.fromisoformat(host_today) + timedelta(days=1)).date().isoformat()
    with pytest.raises(ValueError, match="future"):
        graph.propagate(ticker, tomorrow)
    graph.propagate(ticker, host_today)
    assert len(calls) == 1 and calls[0][1] == host_today
    assert canadian_market.canadian_today(ticker) is None


@pytest.mark.parametrize("ticker", ["TSX:RY+", "TSXV:RCK+", "TSX:ENB.PR.V+",
                                   "TSX:RCK.V", "TSXV:RY.TO"])
def test_rejected_prefixes_are_not_repaired_by_date_classification(sdk, dates, ticker):
    graph, calls, _ = sdk
    with pytest.raises(ValueError):
        graph.propagate(ticker, dates[1])
    assert calls == []
