"""Canadian statements stay usable when Yahoo's separate profile request fails.

Exercise the public SDK lifecycle, injected LangChain tools, real vendor router
and retry/error handling with only the provider boundary replaced offline.
"""

from copy import deepcopy
from unittest.mock import Mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from yfinance.exceptions import YFRateLimitError

from tradingagents.agents import context, tools
from tradingagents.agents.state import AgentState
from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.vendors.yahoo import common, fundamentals
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.extensions import canadian_market
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog

TODAY = "2026-10-09"
STATEMENTS = [
    ("get_balance_sheet", "Balance Sheet", "quarterly_balance_sheet", "balance_sheet"),
    ("get_cashflow", "Cash Flow", "quarterly_cashflow", "cashflow"),
    ("get_income_statement", "Income Statement", "quarterly_income_stmt", "income_stmt"),
]
ALIASES = [("TSX:RY", "RY.TO"), ("TSX:REI.UN", "REI-UN.TO"), ("TSXV:RCK", "RCK.V")]


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(date_window, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(canadian_market, "canadian_today", lambda ticker: TODAY)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    context._identity.cache_clear()
    # Put incompatible vendors first to catch suffix stripping or unsafe fallback.
    wrong_vendor = Mock(side_effect=AssertionError("Canadian statement sent to an incompatible vendor"))
    for method, *_ in STATEMENTS:
        for vendor in router.VENDOR_METHODS[method]:
            if vendor != "yfinance":
                monkeypatch.setitem(router.VENDOR_METHODS[method], vendor, wrong_vendor)

    def run(symbol, method, freq="quarterly", trade_date=TODAY):
        graph = object.__new__(TradingAgentsGraph)
        graph.config = deepcopy(DEFAULT_CONFIG)
        graph.config.update(
            results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
            memory_log_path=str(tmp_path / "memory.md"), checkpoint_enabled=False,
            storage_backend="filesystem", ashare_identity_enabled=False,
            tool_vendors={method: "alpha_vantage,sec_edgar,yfinance"},
        )
        graph.selected_analysts = ["fundamentals"]
        graph.propagator = Propagator()
        graph.memory_log = TradingMemoryLog(graph.config)
        graph.debug = False
        graph._checkpointer_ctx = None
        graph._resuming = False

        def ask(state):
            # The model cannot redirect the symbol/date: the ToolNode injects both.
            return {"messages": [AIMessage("", tool_calls=[{
                "name": method, "id": "statement", "args": {"freq": freq},
            }])]}

        def finish(state):
            return {"fundamentals_report": state["messages"][-1].content,
                    "investment_plan": "", "trader_investment_plan": "",
                    "final_trade_decision": "Rating: Hold", "final_rating": "Hold"}

        workflow = StateGraph(AgentState)
        workflow.add_node("ask", ask)
        workflow.add_node("tools", ToolNode([getattr(tools, method)], handle_tool_errors=False))
        workflow.add_node("finish", finish)
        workflow.add_edge(START, "ask")
        workflow.add_edge("ask", "tools")
        workflow.add_edge("tools", "finish")
        workflow.add_edge("finish", END)
        graph.workflow = workflow
        graph.graph = workflow.compile()
        state, signal = graph.propagate(symbol, trade_date)
        assert signal == "Hold"
        wrong_vendor.assert_not_called()
        return state

    yield run
    context._identity.cache_clear()


def provider(monkeypatch, *, profile_error=None, profile=None, statement_error=None, empty=False):
    """Keep yf_retry real: profile and statements are separate provider endpoints."""
    calls = []
    statement_attrs = {attr for _, _, quarterly, annual in STATEMENTS for attr in (quarterly, annual)}

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            calls.append((self.symbol, "info"))
            if profile_error is not None:
                raise profile_error
            return profile if profile is not None else {"symbol": self.symbol, "quoteType": "EQUITY"}

        def __getattr__(self, attr):
            if attr not in statement_attrs:
                raise AssertionError(f"Unexpected provider endpoint {attr}")
            calls.append((self.symbol, attr))
            if statement_error is not None:
                raise statement_error
            if empty:
                return pd.DataFrame()
            return pd.DataFrame({"2026-07-31": [12345]}, index=["Sourced statement row"])

    monkeypatch.setattr(fundamentals.yf, "Ticker", Ticker)
    return calls


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("freq", ["quarterly", "annual"])
@pytest.mark.parametrize("error", [ConnectionError("profile connection failed"),
                                   TimeoutError("profile timed out"), YFRateLimitError()],
                         ids=["connection", "timeout", "rate_limit"])
def test_profile_outage_does_not_suppress_sourced_statements(
    monkeypatch, sdk, raw, canonical, method, title, quarterly, annual, freq, error,
):
    calls = provider(monkeypatch, profile_error=error)
    state = sdk(raw, method, freq)
    text = state["fundamentals_report"]
    assert text.startswith(f"# {title} data for {canonical} ({freq})\n")
    assert "Sourced statement row,12345" in text
    assert "UNAVAILABLE" not in text and "NOT_APPLICABLE" not in text
    assert state["company_of_interest"] == canonical
    assert {symbol for symbol, _ in calls} == {canonical}
    assert (canonical, "info") in calls
    assert [(symbol, attr) for symbol, attr in calls if attr != "info"] == [
        (canonical, quarterly if freq == "quarterly" else annual),
    ]


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND"])
def test_known_fund_still_withholds_corporate_statements(
    monkeypatch, sdk, method, title, quarterly, annual, quote_type,
):
    calls = provider(monkeypatch, profile={"symbol": "ZSP.TO", "quoteType": quote_type})
    text = sdk("TSX:ZSP", method)["fundamentals_report"]
    assert text.startswith("NOT_APPLICABLE:")
    assert title.lower() in text and "ZSP.TO" in text
    assert calls and all(attr == "info" for _, attr in calls)


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("quote_type", ["EQUITY", "ETF"])
def test_profile_identity_mismatch_does_not_fail_open(
    monkeypatch, sdk, method, title, quarterly, annual, quote_type,
):
    calls = provider(monkeypatch, profile={"symbol": "RY", "quoteType": quote_type})
    with pytest.raises(ValueError, match="does not match requested listing RY.TO"):
        sdk("TSX:RY", method)
    assert calls and all(attr == "info" for _, attr in calls)


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("raw,canonical", ALIASES)
def test_historical_statements_stay_withheld_before_statement_profile_probe(
    monkeypatch, sdk, method, title, quarterly, annual, raw, canonical,
):
    calls = provider(monkeypatch, profile_error=ConnectionError("profile connection failed"))
    text = sdk(raw, method, trade_date="2026-10-08")["fundamentals_report"]
    assert "withheld" in text.lower() and "2026-10-08" in text and canonical in text
    # One best-effort identity lookup at SDK startup, no statement profile probe.
    assert calls == [(canonical, "info")]


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("failure,expected", [
    ("request", "DATA_UNAVAILABLE:"),
    ("empty_unreachable", "DATA_UNAVAILABLE:"),
    ("empty_reachable", "NO_DATA_AVAILABLE:"),
])
def test_statement_failure_keeps_its_own_error_classification(
    monkeypatch, sdk, method, title, quarterly, annual, failure, expected,
):
    calls = provider(monkeypatch, profile_error=ConnectionError("profile connection failed"),
                     statement_error=TimeoutError("statement timed out") if failure == "request" else None,
                     empty=failure.startswith("empty"))
    monkeypatch.setattr(common, "vendor_reachable", lambda url: failure != "empty_unreachable")
    text = sdk("TSX:RY", method)["fundamentals_report"]
    assert text.startswith(expected), text
    assert "ConnectionError" not in text  # The optional profile must not mask this result.
    if failure == "request":
        assert "TimeoutError" in text
    else:
        assert title.lower() in text
    assert "NOT_APPLICABLE" not in text and "Sourced statement row" not in text
    assert ("RY.TO", quarterly) in calls


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
@pytest.mark.parametrize("profile", [{}, {"symbol": "RY.TO"}, {"symbol": "RY.TO", "quoteType": "EQUITY"}])
def test_missing_or_non_fund_classification_still_serves_statements(
    monkeypatch, sdk, method, title, quarterly, annual, profile,
):
    provider(monkeypatch, profile=profile)
    text = sdk("TSX:RY", method)["fundamentals_report"]
    assert "Sourced statement row,12345" in text


@pytest.mark.parametrize("method,title,quarterly,annual", STATEMENTS)
def test_non_canadian_statement_does_not_gain_profile_dependency(
    monkeypatch, method, title, quarterly, annual,
):
    calls = provider(monkeypatch, profile_error=ConnectionError("profile connection failed"))
    text = getattr(fundamentals, method)("AAPL")
    assert text.startswith(f"# {title} data for AAPL")
    assert calls == [("AAPL", quarterly)]
