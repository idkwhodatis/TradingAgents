"""Offline regression coverage for Canadian aliases across SDK and CLI boundaries."""

import json
from unittest.mock import Mock

import pytest

from cli.headless import resolve_analysis_inputs
from cli.models import AssetType
from cli.prompts import is_valid_ticker_input, parse_ticker
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import PortfolioContext, load_portfolio


@pytest.fixture
def graph(tmp_path, monkeypatch):
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.propagator = Propagator()
    monkeypatch.setattr(graph, "resolve_instrument_context", lambda *args: "Offline identity")
    monkeypatch.setattr(graph, "run_settings", lambda: {})
    return graph


ALIASES = [
    ("BBD.B.TO", "BBD-B.TO"),  # Already accepted before the Canadian adapter.
    ("TSX:BBD.B", "BBD-B.TO"),
    ("TSX:RY", "RY.TO"),
    ("TSX:RY.TO", "RY.TO"),
    ("TSXV:RCK", "RCK.V"),
    ("TSXV:RCK.V", "RCK.V"),
    ("TSX:ENB.PR.V", "ENB-PV.TO"),
    ("ENB.PR.V.TO", "ENB-PV.TO"),
]


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("kind", ["stock", AssetType.STOCK])
@pytest.mark.parametrize("entrypoint", ["sdk", "headless", "tui"])
def test_book_alias_survives_every_run_entrypoint(graph, tmp_path, raw, canonical, kind, entrypoint):
    data = {"cash": 25000, "currency": "CAD", "positions": [
        {"ticker": raw, "quantity": 120, "average_price": 150},
        {"ticker": "RY", "quantity": 7},
    ]}
    if entrypoint == "sdk":
        book = PortfolioContext.model_validate(data)
        ticker = raw
    else:
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps(data))
        book = load_portfolio(path)  # Same loader used by both CLI modes.
        ticker = (resolve_analysis_inputs(raw, "2026-01-01", "all", "auto")[0]
                  if entrypoint == "headless" else parse_ticker(raw))
    before = book.model_dump()
    fingerprint = book.fingerprint()
    state = graph.create_run_state(ticker, "2026-01-01", kind, book)
    assert state["company_of_interest"] == canonical
    assert f"Current position in {canonical}: 120 units, average price 150.00" in state["portfolio_context"]
    assert "Other positions: RY 7" in state["portfolio_context"]
    assert book.position_in(canonical) is book.positions[0]
    assert book.position_in(raw) is book.positions[0]
    assert book.model_dump() == before
    assert book.fingerprint() == fingerprint


def test_portfolio_comparison_is_symmetric_and_does_not_merge_us_listing():
    book = PortfolioContext.model_validate({"positions": [
        {"ticker": "RY.TO", "quantity": 120}, {"ticker": "RY", "quantity": 7},
        {"ticker": "TSX:../UNSUPPORTED", "quantity": 1},
    ]})
    assert book.position_in("TSX:RY") is book.positions[0]
    assert book.position_in("RY") is book.positions[1]
    assert "Current position in RY: 7 units" in book.render("RY")
    assert book.position_in("RCK.V") is None


@pytest.mark.parametrize("raw,canonical", ALIASES)
@pytest.mark.parametrize("explicit_path", [False, True])
def test_sdk_analyze_then_save_uses_canonical_listing(graph, tmp_path, raw, canonical, explicit_path):
    state = graph.create_run_state(raw, "2026-01-01")
    path = tmp_path / "explicit" if explicit_path else None
    report = graph.save_reports(state, raw, save_path=path)
    assert report.exists()
    assert f"Trading Analysis Report: {canonical}" in report.read_text()
    if explicit_path:
        assert report.parent == path
    else:
        assert report.parent.parent == tmp_path / "reports"
        assert report.parent.name.startswith(canonical + "_")


@pytest.mark.parametrize("raw", ["../RY.TO", "TSX:../RY", "TSXV:../../RCK", "TSX:RY/../../x",
                                  "TSX:RY\\..\\x", "TSX:RY\x00", "TSX:RCK.V", "TSXV:RY.TO",
                                  "..", "/tmp/RY.TO", "A" * 33 + ".TO"])
def test_export_never_relaxes_path_or_exchange_guards(graph, monkeypatch, raw):
    writer = Mock(side_effect=AssertionError("Unsafe report writer call"))
    monkeypatch.setattr("tradingagents.graph.trading_graph.write_report_tree", writer)
    state = graph.create_run_state("TSX:RY", "2026-01-01")
    with pytest.raises(ValueError):
        graph.save_reports(state, raw)
    writer.assert_not_called()


@pytest.mark.parametrize("state_symbol", ["RY", "RCK.V", "../RY.TO", "TSX:../RY"])
def test_export_rejects_different_or_unsafe_state_listing(graph, state_symbol):
    with pytest.raises(ValueError):
        graph.save_reports({"company_of_interest": state_symbol}, "TSX:RY")


@pytest.mark.parametrize("raw", ["TSX:ENB.PR.V", "ENB.PR.V.TO", "TSX:ENB.PR.V.TO", "ENB-PV.TO"])
def test_native_preferred_v_is_series_not_venture_suffix(raw):
    # Issuer: enbridge.com/reports/annual-letter-to-shareholders-2026/investor-information
    # Provider spelling: ca.finance.yahoo.com/quote/ENB-PV.TO/
    assert normalize_symbol(raw) == "ENB-PV.TO"
    assert is_valid_ticker_input(raw)
    assert parse_ticker(raw) == "ENB-PV.TO"


@pytest.mark.parametrize("raw,message", [
    ("TSX:RCK.V", "disagree"), ("TSXV:RY.TO", "disagree"),
    ("TSXV:ENB.PR.V", "requires TSX"), ("ENB.PR.V", "Ambiguous preferred"),
    ("TSX:ENB.PF.V", "Unsupported preferred"), ("ENB.PF.V.TO", "Unsupported preferred"),
    ("TSX:ENB.PR", "Unsupported preferred"),
])
def test_conflicts_and_unsupported_preferred_forms_fail_precisely(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_symbol(raw)
    assert not is_valid_ticker_input(raw)
