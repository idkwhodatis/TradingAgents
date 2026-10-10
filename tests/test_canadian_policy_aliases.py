"""Raw accepted aliases cannot bypass Canadian vendor or report policy gates."""

from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors.alpha_vantage import fundamentals as alpha
from tradingagents.dataflows.vendors.yahoo import fundamentals
from tradingagents.extensions import canadian_market
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph

ALIASES = [("RY.TO+", "RY.TO"), (" rck.v+ ", "RCK.V"),
           ("BBD.B.TO+", "BBD-B.TO"), ("ENB.PR.V.TO+", "ENB-PV.TO")]
METHODS = [
    ("get_stock_data", ("2026-01-01", "2026-01-02")),
    ("get_indicators", ("close_50_sma", "2026-01-02", 10)),
    ("get_fundamentals", ()),
    ("get_balance_sheet", ()),
    ("get_cashflow", ()),
    ("get_income_statement", ()),
    ("get_news", ("2026-01-01", "2026-01-02")),
    ("get_insider_transactions", ()),
]


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("method,remaining", METHODS)
def test_every_symbol_router_gate_uses_accepted_listing(monkeypatch, raw, canonical, method, remaining):
    unsupported = Mock(side_effect=AssertionError("Incompatible vendor requested"))
    yahoo = Mock(return_value="Yahoo exact-listing data")
    monkeypatch.setitem(router.VENDOR_METHODS, method, {"alpha_vantage": unsupported, "yfinance": yahoo})
    with run_config({"tool_vendors": {method: "alpha_vantage"}}):
        output = router.route_to_vendor(method, raw, *remaining)
    assert output.startswith("DATA_UNAVAILABLE:")
    unsupported.assert_not_called()
    yahoo.assert_not_called()
    with run_config({"tool_vendors": {method: "alpha_vantage,yfinance"}}):
        assert router.route_to_vendor(method, raw, *remaining) == "Yahoo exact-listing data"
    yahoo.assert_called_once_with(raw, *remaining)
    unsupported.assert_not_called()


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("method", ["get_balance_sheet", "get_cashflow", "get_income_statement"])
@pytest.mark.parametrize("freq", ["quarterly", "annual"])
def test_direct_router_real_statement_endpoint_preserves_qualifier_identity(
    monkeypatch, raw, canonical, method, freq,
):
    unsupported_http = Mock(side_effect=AssertionError("Alpha Vantage HTTP requested"))
    monkeypatch.setattr(alpha, "_make_api_request", unsupported_http)
    statement = pd.DataFrame({"2026-07-31": [12345]}, index=["Sourced row"])
    ticker = Mock(return_value=Mock(
        info={"symbol": canonical, "quoteType": "EQUITY"},
        quarterly_balance_sheet=statement, balance_sheet=statement,
        quarterly_cashflow=statement, cashflow=statement,
        quarterly_income_stmt=statement, income_stmt=statement,
    ))
    monkeypatch.setattr(fundamentals.yf, "Ticker", ticker)
    with run_config({"tool_vendors": {method: "alpha_vantage,yfinance"}}):
        text = router.route_to_vendor(method, ticker=raw, freq=freq)
    assert f"for {canonical} ({freq})" in text
    assert "Sourced row,12345" in text
    assert all(call.args == (canonical,) for call in ticker.call_args_list)
    unsupported_http.assert_not_called()


@pytest.mark.parametrize("ticker", ["RY", "nvda", "600519.SH", "000001.SZ", "0700.HK", "BTCUSD+"])
def test_direct_router_leaves_other_markets_and_raw_arguments_unchanged(monkeypatch, ticker):
    provider = Mock(return_value="Non-Canadian provider data")
    monkeypatch.setattr(alpha, "_make_api_request", provider)
    with run_config({"tool_vendors": {"get_balance_sheet": "alpha_vantage"}}):
        assert router.route_to_vendor("get_balance_sheet", ticker=ticker) == "Non-Canadian provider data"
    provider.assert_called_once_with("BALANCE_SHEET", {"symbol": ticker})


@pytest.mark.parametrize("ticker", ["TSX:RY+", "TSXV:RCK+", "TSX:RCK.V", "TSXV:RY.TO"])
def test_direct_router_does_not_repair_malformed_prefixes(monkeypatch, ticker):
    provider = Mock(side_effect=AssertionError("Invalid symbol reached provider"))
    monkeypatch.setattr(alpha, "_make_api_request", provider)
    with run_config({"tool_vendors": {"get_balance_sheet": "alpha_vantage"}}), pytest.raises(ValueError):
        router.route_to_vendor("get_balance_sheet", ticker=ticker)
    provider.assert_not_called()


@pytest.mark.parametrize("raw,canonical", ALIASES)
def test_shared_profile_and_context_gates_recognize_accepted_aliases(raw, canonical):
    profile = {"symbol": canonical, "quoteType": "ETF"}
    assert canadian_market.validate_profile(raw, profile) is profile
    with pytest.raises(ValueError, match="does not match"):
        canadian_market.validate_profile(raw, {"symbol": canonical.rsplit(".", 1)[0]})
    assert "Canadian listing context" in canadian_market.market_context(raw)
    assert "Exchange-listed Fund Overview" in canadian_market.fund_overview(raw, profile)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.propagator = Propagator()
    monkeypatch.setattr(graph, "resolve_instrument_context", lambda *args: "Offline identity")
    monkeypatch.setattr(graph, "run_settings", lambda: {})
    return graph


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("explicit_path", [False, True])
def test_sdk_reports_use_accepted_alias_canonical_identity(graph, tmp_path, raw, canonical, explicit_path):
    state = graph.create_run_state(raw, "2026-01-01")
    path = tmp_path / "chosen" if explicit_path else None
    report = graph.save_reports(state, raw, save_path=path)
    assert report.read_text().startswith(f"# Trading Analysis Report: {canonical}\n")
    assert f'<h1><span class="ticker">{canonical}</span></h1>' in report.with_suffix(".html").read_text()
    if not explicit_path:
        assert report.parent.name.startswith(canonical + "_")


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("state_symbol", ["RY", "SPY.TO", "../RY.TO+", "TSX:RY+"])
def test_report_qualifiers_cannot_bypass_listing_mismatch(graph, monkeypatch, raw, canonical, state_symbol):
    writer = Mock(side_effect=AssertionError("Mismatched report was written"))
    monkeypatch.setattr("tradingagents.graph.trading_graph.write_report_tree", writer)
    with pytest.raises(ValueError):
        graph.save_reports({"company_of_interest": state_symbol}, raw)
    writer.assert_not_called()
