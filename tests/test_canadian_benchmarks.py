"""Canadian benchmark selection and real, offline settlement arithmetic."""

from copy import deepcopy
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.backtest import summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import TradingMemoryLog, settlement
from tradingagents.memory.reflection import Reflector

DATE = "2026-01-05"
TSXV_ALIASES = ["RCK.V", "rck.v", "TSXV:RCK", "TSXV:RCK.V", " rck.v+ "]


@pytest.mark.parametrize("ticker,benchmark", [
    *((ticker, "^SPCDNX") for ticker in TSXV_ALIASES),
    ("RY.TO", "^GSPTSE"), ("TSX:RY", "^GSPTSE"),
    ("TSX:ENB.PR.V", "^GSPTSE"), ("ENB.PR.V.TO", "^GSPTSE"),
    ("AAPL", "SPY"), ("BRK.B", "SPY"), ("FAKE.XX", "SPY"),
])
def test_default_canadian_benchmarks(ticker, benchmark):
    assert settlement.resolve_benchmark(ticker, DEFAULT_CONFIG) == benchmark


@pytest.mark.parametrize("ticker", TSXV_ALIASES)
@pytest.mark.parametrize("override,expected", [
    ("SPY", "SPY"), ("XIC.TO", "XIC.TO"), ("SPX500", "^GSPC"),
])
def test_tsxv_explicit_benchmark_still_wins(ticker, override, expected):
    config = {**DEFAULT_CONFIG, "benchmark_ticker": override}
    assert settlement.resolve_benchmark(ticker, config) == expected


@pytest.mark.parametrize("ticker", TSXV_ALIASES)
def test_tsxv_custom_suffix_map_still_wins(ticker):
    config = {**DEFAULT_CONFIG,
              "benchmark_map": {**DEFAULT_CONFIG["benchmark_map"], ".V": "XIC.TO"}}
    assert settlement.resolve_benchmark(ticker, config) == "XIC.TO"


@pytest.fixture
def offline_prices(monkeypatch):
    days = pd.bdate_range(DATE, periods=6)
    prices = {
        "RCK.V": pd.Series([100, 101, 102, 103, 104, 105], index=days),
        "^SPCDNX": pd.Series([100, 100.4, 100.8, 101.2, 101.6, 102], index=days),
        "SPY": pd.Series([100, 102, 104, 106, 108, 110], index=days),
    }
    closes = Mock(side_effect=lambda ticker, *_: prices[ticker].copy())
    monkeypatch.setattr(settlement, "get_closes", closes)
    return prices, closes


@pytest.mark.parametrize("stored_ticker", ["RCK.V", "TSXV:RCK"])
@pytest.mark.parametrize("override,benchmark,alpha,hit_rate", [
    (None, "^SPCDNX", "+3.0%", 1.0),
    ("SPY", "SPY", "-5.0%", 0.0),
])
def test_tsxv_settlement_scores_and_reflects_selected_benchmark(
    tmp_path, offline_prices, stored_ticker, override, benchmark, alpha, hit_rate,
):
    config = {**deepcopy(DEFAULT_CONFIG), "memory_log_path": str(tmp_path / "memory.md"),
              "benchmark_ticker": override}
    log = TradingMemoryLog(config)
    log.store_decision(stored_ticker, DATE, "Rating: Buy\nOffline decision")
    llm = Mock()
    llm.invoke.return_value.content = "Offline reflection."

    done = settlement.settle_pending("TSXV:RCK", log, Reflector(llm), config)

    assert done.settled == [(stored_ticker, DATE)] and not done.failed
    assert [call.args[0] for call in offline_prices[1].call_args_list] == ["RCK.V", benchmark]
    entry, = log.load_entries()
    assert entry["ticker"] == stored_ticker and not entry["pending"]
    assert entry["raw"] == "+5.0%" and entry["alpha"] == alpha
    assert entry["resolved"] == "2026-01-12"
    summary = summarize(config["memory_log_path"])
    assert summary.by_rating["Buy"].mean_alpha == pytest.approx(float(alpha.rstrip("%")) / 100)
    assert summary.by_rating["Buy"].hit_rate == hit_rate
    human_prompt = llm.invoke.call_args.args[0][1][1]
    assert f"Alpha vs {benchmark}: {alpha}" in human_prompt
    assert alpha in log.get_past_context("RCK.V", as_of="2026-01-12")

    # A config change must not silently rewrite outcomes already in the user's log.
    before = (tmp_path / "memory.md").read_bytes()
    config["benchmark_ticker"] = "SPY" if override is None else "^SPCDNX"
    assert not settlement.settle_pending("RCK.V", log, Reflector(llm), config).settled
    assert (tmp_path / "memory.md").read_bytes() == before
    assert llm.invoke.call_count == 1 and offline_prices[1].call_count == 2


@pytest.mark.parametrize("unavailable", ["empty", "short", "error"])
def test_missing_tsxv_benchmark_stays_pending_without_fallback(tmp_path, offline_prices, unavailable):
    prices, closes = offline_prices
    if unavailable == "error":
        def unavailable_closes(ticker, *_):
            if ticker == "^SPCDNX":
                raise OSError("Offline benchmark failure")
            return prices[ticker].copy()

        closes.side_effect = unavailable_closes
    else:
        prices["^SPCDNX"] = prices["^SPCDNX"].iloc[:0 if unavailable == "empty" else 5]
    config = {**deepcopy(DEFAULT_CONFIG), "memory_log_path": str(tmp_path / "memory.md")}
    log = TradingMemoryLog(config)
    log.store_decision("TSXV:RCK", DATE, "Rating: Buy\nOffline decision")
    reflector = Mock()
    before = (tmp_path / "memory.md").read_bytes()

    done = settlement.settle_pending("RCK.V", log, reflector, config)

    assert not done.settled
    assert bool(done.failed) == (unavailable == "error")
    assert [call.args[0] for call in closes.call_args_list] == ["RCK.V", "^SPCDNX"]
    assert log.load_entries()[0]["pending"]
    assert (tmp_path / "memory.md").read_bytes() == before
    reflector.reflect_on_final_decision.assert_not_called()
