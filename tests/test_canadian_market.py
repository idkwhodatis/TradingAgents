"""Canadian adapter contract, offline including same-name US isolation."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.agents.context import build_instrument_context, resolve_instrument_identity
from tradingagents.dataflows import router
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo import fundamentals, market, news
from tradingagents.extensions.ashare_identity import prepare_instrument
from tradingagents.extensions.canadian_market import (
    compatible_vendors,
    fund_overview,
    market_context,
    validate_profile,
)


@pytest.mark.parametrize("raw,expected", [
    ("TSX:RY", "RY.TO"), ("TSXV:RCK", "RCK.V"), ("ry.to", "RY.TO"),
    ("ZSP.TO", "ZSP.TO"), ("REI-UN.TO", "REI-UN.TO"),
    ("TSX:REI.UN", "REI-UN.TO"), ("TSX:BBD.B", "BBD-B.TO"),
    ("TSX:DLR.U", "DLR-U.TO"), ("BBD.B.TO", "BBD-B.TO"),
    ("TSX:RY.TO", "RY.TO"), ("RY", "RY"), ("SPY", "SPY"),
])
def test_normalization(raw, expected):
    assert normalize_symbol(raw) == expected
    from cli.prompts import normalize_ticker_symbol
    assert normalize_ticker_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["TSX:RCK.V", "TSXV:RY.TO", "TSX:", "TSX:../RY"])
def test_invalid_explicit_listing(raw):
    with pytest.raises(ValueError):
        normalize_symbol(raw)


def test_library_and_headless_paths_preserve_canada():
    from cli.headless import resolve_analysis_inputs
    assert prepare_instrument("TSX:ZSP", "stock", {}) == ("ZSP.TO", {})
    assert prepare_instrument("TSXV:RCK", "stock", {}) == ("RCK.V", {})
    assert resolve_analysis_inputs("TSX:REI.UN", "2026-01-01", "all", "auto")[0] == "REI-UN.TO"


def test_canadian_context_currency_types_and_historical_guard():
    identity = {"company_name": "Example Fund", "quote_type": "ETF", "currency": "USD", "financial_currency": "CAD"}
    value = build_instrument_context("DLR-U.TO", identity=identity)
    for expected in ("TSX", "America/Toronto", "Provider quote currency: USD", "Bank of Canada",
                     "SEDAR+", "NAV", "FFO/AFFO", "US namesake", "Provider financial-reporting currency: CAD"):
        assert expected in value
    historic = build_instrument_context("DLR-U.TO", identity=identity, trade_date="2020-01-01")
    assert "Provider quote currency: USD" not in historic
    assert "TSXV" in market_context("RCK.V")
    assert "Provider quote currency:" not in market_context("RY.TO")
    assert market_context("RY") == ""


def test_company_profile_never_accepts_us_namesake(monkeypatch):
    monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: fn())
    ticker = Mock(return_value=Mock(info={"symbol": "RY", "longName": "Wrong listing"}))
    monkeypatch.setattr(fundamentals.yf, "Ticker", ticker)
    with pytest.raises(ValueError, match="does not match"):
        fundamentals.get_company_profile("RY.TO")
    ticker.assert_called_once_with("RY.TO")
    assert resolve_instrument_identity("RY.TO", {}) == {}
    assert validate_profile("RY", {"symbol": "RY"}) == {"symbol": "RY"}


def test_fund_fields_exclude_corporate_numbers(monkeypatch):
    profile = {"symbol": "ZSP.TO", "quoteType": "ETF", "longName": "BMO S&P 500 Index ETF",
               "currency": "CAD", "navPrice": 100, "totalAssets": 1000000,
               "trailingEps": 999, "totalRevenue": 123, "debtToEquity": 234}
    monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: fn())
    monkeypatch.setattr(fundamentals.yf, "Ticker", lambda symbol: Mock(info=profile))
    text = fundamentals.get_fundamentals("ZSP.TO")
    assert "Exchange-listed Fund Overview" in text and "CAD" in text and "1000000" in text
    assert "999" not in text and "123" not in text and "234" not in text
    assert "unavailable" in text
    assert fund_overview("RY.TO", {"quoteType": "EQUITY"}) is None


@pytest.mark.parametrize("method", ["get_balance_sheet", "get_income_statement", "get_cashflow"])
def test_fund_statements_are_not_corporate_statements(monkeypatch, method):
    monkeypatch.setattr(fundamentals, "get_company_profile", lambda _: {"quoteType": "ETF"})
    ticker = Mock(side_effect=AssertionError("No corporate statement request for a fund"))
    monkeypatch.setattr(fundamentals.yf, "Ticker", ticker)
    assert "NOT_APPLICABLE" in getattr(fundamentals, method)("ZSP.TO")
    ticker.assert_not_called()


def test_equity_and_reit_statements_still_work(monkeypatch):
    monkeypatch.setattr(fundamentals, "get_company_profile", lambda _: {"quoteType": "EQUITY"})
    monkeypatch.setattr(fundamentals, "yf_retry", lambda fn: fn())
    ticker = Mock(return_value=Mock(quarterly_balance_sheet=pd.DataFrame({"2026": [1]}, index=["Assets"])))
    monkeypatch.setattr(fundamentals.yf, "Ticker", ticker)
    for symbol in ("RY.TO", "REI-UN.TO", "RCK.V"):
        assert "Assets" in fundamentals.get_balance_sheet(symbol)
    assert [call.args[0] for call in ticker.call_args_list] == ["RY.TO", "REI-UN.TO", "RCK.V"]


def test_historical_fundamentals_do_not_fetch_current_profile(monkeypatch):
    monkeypatch.setattr(fundamentals.yf, "Ticker", Mock(side_effect=AssertionError("live request")))
    assert "withheld" in fundamentals.get_fundamentals("ZSP.TO", "2020-01-01").lower()
    assert "withheld" in fundamentals.get_balance_sheet("ZSP.TO", as_of_date="2020-01-01").lower()


@pytest.mark.parametrize("symbol", ["RY.TO", "ZSP.TO", "REI-UN.TO", "RCK.V"])
def test_routing_never_tries_incompatible_symbol_namespace(monkeypatch, symbol):
    yahoo = Mock(return_value="correct data")
    wrong = Mock(side_effect=AssertionError("US/vendor namesake route"))
    monkeypatch.setattr(router, "get_vendor", lambda *args: "alpha_vantage,sec_edgar,yfinance")
    monkeypatch.setitem(router.VENDOR_METHODS, "get_balance_sheet", {"alpha_vantage": wrong, "sec_edgar": wrong, "yfinance": yahoo})
    assert router.route_to_vendor("get_balance_sheet", symbol) == "correct data"
    yahoo.assert_called_once_with(symbol)
    wrong.assert_not_called()
    monkeypatch.setattr(router, "get_vendor", lambda *args: "sec_edgar")
    assert "DATA_UNAVAILABLE" in router.route_to_vendor("get_balance_sheet", ticker=symbol)
    wrong.assert_not_called()
    assert compatible_vendors("get_global_news", symbol, ["alpha_vantage"]) == ["alpha_vantage"]


def test_news_search_requires_exact_suffix(monkeypatch):
    search = Mock(return_value=Mock(quotes=[{"symbol": "RY.TO"}], news=[
        {"title": "US namesake", "relatedTickers": ["RY"]},
        {"title": "Canadian listing", "relatedTickers": ["RY.TO"]},
    ]))
    monkeypatch.setattr(news, "yf_retry", lambda fn: fn())
    monkeypatch.setattr(news.yf, "Search", search)
    monkeypatch.setattr(news, "news_queries", lambda _: [])
    articles, _ = news._search_news("RY.TO", 10)
    assert [a["title"] for a in articles] == ["Canadian listing"]
    search.assert_called_once_with("RY.TO", news_count=10)


@pytest.mark.parametrize("symbol", ["RY.TO", "ZSP.TO", "REI-UN.TO", "RCK.V"])
def test_ohlcv_exact_listing_and_timezone(monkeypatch, symbol):
    data = pd.DataFrame({"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
                        index=pd.DatetimeIndex(["2026-01-02"], tz="America/Toronto", name="Date"))
    ticker = Mock(return_value=Mock(history=Mock(return_value=data)))
    monkeypatch.setattr(market.yf, "Ticker", ticker)
    monkeypatch.setattr(market, "yf_retry", lambda fn: fn())
    text = market.get_YFin_data_online(symbol, "2026-01-01", "2026-01-02")
    assert symbol in text and "America/Toronto" in text and "10.5" in text
    ticker.assert_called_once_with(symbol)


def test_prefix_accepted_by_real_cli_validator():
    from cli.prompts import is_valid_ticker_input, parse_ticker
    for raw, expected in (("TSX:RY", "RY.TO"), ("TSXV:RCK", "RCK.V"), ("TSX:REI.UN", "REI-UN.TO")):
        assert is_valid_ticker_input(raw)
        assert parse_ticker(raw) == expected
    assert not is_valid_ticker_input("TSX:RCK.V")


@pytest.mark.parametrize("instant,expected", [
    ("2026-01-02T02:00:00+00:00", "2026-01-01"),
    ("2026-07-02T03:59:00+00:00", "2026-07-01"),
    ("2026-07-02T04:00:00+00:00", "2026-07-02"),
])
def test_toronto_calendar_date_handles_dst(instant, expected):
    from datetime import datetime

    from tradingagents.extensions.canadian_market import canadian_today
    assert canadian_today("ZSP.TO", datetime.fromisoformat(instant)) == expected
    assert canadian_today("RCK.V", datetime.fromisoformat(instant)) == expected
    assert canadian_today("AAPL", datetime.fromisoformat(instant)) is None


def test_canadian_current_date_guards_use_toronto(monkeypatch):
    from cli.headless import resolve_analysis_inputs
    from cli.prompts import parse_analysis_date
    from tradingagents.dataflows import date_window
    from tradingagents.extensions import canadian_market
    from tradingagents.graph.trading_graph import _validate_trade_date

    def local_today(ticker):
        return "2026-01-01" if ticker.endswith((".TO", ".V")) else None
    monkeypatch.setattr(canadian_market, "canadian_today", local_today)
    monkeypatch.setattr("cli.headless.canadian_today", local_today)
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-01-02")
    assert not date_window.is_historical("2026-01-01", "ZSP.TO")
    assert date_window.is_historical("2025-12-31", "ZSP.TO")
    assert date_window.is_historical("2026-01-01", "AAPL")
    assert date_window.withhold_live_profile("2026-01-01", "ZSP.TO") is None
    assert date_window.withhold_undated_statements("2026-01-01", "RY.TO", "Balance Sheet") is None
    assert resolve_analysis_inputs("TSX:ZSP", None, "all", "auto")[1] == "2026-01-01"
    with pytest.raises(ValueError, match="future"):
        parse_analysis_date("2026-01-02", "RY.TO")
    with pytest.raises(ValueError, match="future"):
        _validate_trade_date("2026-01-02", "RY.TO")


@pytest.mark.parametrize("symbol", ["RY.TO", "ZSP.TO", "REI-UN.TO", "RCK.V"])
def test_technical_calculation_uses_exact_canadian_symbol(monkeypatch, symbol):
    frame = pd.DataFrame({"Date": pd.date_range("2026-01-01", periods=30),
                          "Open": range(1, 31), "High": range(2, 32), "Low": range(30),
                          "Close": range(1, 31), "Volume": [100] * 30})
    loader = Mock(return_value=frame)
    monkeypatch.setattr(market, "load_ohlcv", loader)
    output = market._get_stock_stats_bulk(symbol, "close_10_sma", "2026-01-30")
    assert float(output["2026-01-30"]) == 25.5
    loader.assert_called_once_with(symbol, "2026-01-30")


def test_library_initial_state_keeps_etf_context_without_llm(monkeypatch):
    from tradingagents.graph import trading_graph
    from tradingagents.graph.propagation import Propagator

    graph = trading_graph.TradingAgentsGraph.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {}
    graph.propagator = Propagator()
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda *args: {
        "company_name": "BMO S&P 500 Index ETF", "quote_type": "ETF", "currency": "CAD",
    })
    state = graph.create_run_state("TSX:ZSP", "2026-01-01")
    assert state["company_of_interest"] == "ZSP.TO"
    assert "BMO S&P 500 Index ETF" in state["instrument_context"]
    assert "do not invent operating-company" in state["instrument_context"]
    assert "America/Toronto" in state["instrument_context"]
