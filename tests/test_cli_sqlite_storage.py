"""SQLite archive integration through the real shared CLI runner, without APIs."""

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

import cli.headless as headless
import cli.main as main
import cli.run as native
from cli.models import AnalystType
from tests.native_cli_helpers import RecordingGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.storage import SQLiteStorage, create_run

TRADE_DATE = "2026-09-27"
REPORT_NAMES = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
)


class ArchiveRecordingGraph(RecordingGraph):
    """Keep the offline graph double, but exercise real final-state persistence."""

    def create_run_state(self, *args, **kwargs):
        # Attachment must precede all execution, including checkpoint setup.
        assert self.run_store.status == "running"
        return super().create_run_state(*args, **kwargs)

    def _log_state(self, trade_date, state):
        self.calls.append(("json",))
        # The shared fixture intentionally omits fields irrelevant to its own
        # file stub. Supply the canonical state fields required by the real SDK.
        state = deepcopy(state)
        state["investment_debate_state"].setdefault("history", "bull\nbear")
        state["investment_debate_state"].setdefault("current_response", "research")
        state["risk_debate_state"].setdefault("history", "risk 中文")
        TradingAgentsGraph._log_state(self, trade_date, state)


@pytest.fixture
def sqlite_setup(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("TRADINGAGENTS_"):
            monkeypatch.delenv(name)
    config = deepcopy(DEFAULT_CONFIG)
    config.update(
        results_dir=str(tmp_path / "results"),
        data_cache_dir=str(tmp_path / "cache"),
        memory_log_path=str(tmp_path / "memory.md"),
        llm_provider="openai",
        quick_think_llm="gpt-4.1-mini",
        deep_think_llm="gpt-4.1",
        backend_url=None,
        llm_headers=None,
        max_tokens=None,
        llm_max_retries=None,
        temperature=None,
        output_language="Chinese",
        checkpoint_enabled=False,
        storage_backend="sqlite",
        storage_db_path=None,
        storage_max_artifact_bytes=64 * 1024 * 1024,
    )
    for module in (main, native):
        monkeypatch.setattr(module, "DEFAULT_CONFIG", config)
    for module in (headless, native):
        monkeypatch.setattr(module, "get_current_date", lambda: TRADE_DATE)
    graphs = []

    def factory(analysts, settings, callbacks=None):
        graph = ArchiveRecordingGraph(analysts, settings, callbacks)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(headless, "_create_graph", factory)
    monkeypatch.setattr(native, "_default_graph_factory", factory)
    monkeypatch.setattr(
        native, "get_user_selections", lambda: pytest.fail("resolved analysis opened the wizard")
    )
    return config, graphs, CliRunner()


def _database_path(config):
    return Path(
        config.get("storage_db_path") or Path(config["results_dir"]) / "runs.sqlite3"
    ).resolve()


def _database(config):
    return SQLiteStorage(_database_path(config))


def _json_artifact(database, run_id, name):
    return json.loads(database.read_artifact(run_id, name))


def _log(database, run_id):
    return "\n".join(row["line"] for row in database.read_logs(run_id))


def _selections(ticker="NVDA"):
    return {
        "ticker": ticker,
        "analysis_date": TRADE_DATE,
        "asset_type": "stock",
        "analysts": list(AnalystType),
    }


def _assert_no_native_directories(config, ticker="NVDA"):
    root = Path(config["results_dir"])
    assert not (root / ticker).exists()
    assert not (root / "runs").exists()
    assert not list(root.rglob("message_tool.log"))
    assert not list(root.rglob("run.json"))
    assert not list(root.rglob("full_states_log_*.json"))


def test_sqlite_headless_summary_unicode_and_existing_export_behavior(sqlite_setup):
    config, graphs, runner = sqlite_setup
    outcome = runner.invoke(main.app, ["analyze", "nvda", "--json"])
    assert outcome.exit_code == 0, outcome.output
    summary = json.loads(outcome.stdout)
    assert summary["storage_backend"] == "sqlite"
    assert Path(summary["storage_db"]) == _database_path(config)
    assert UUID(summary["run_id"]).version == 4
    assert summary["output_dir"] is None
    assert summary["log_file"] is None
    assert summary["status"] == "completed"
    assert summary["decision"] == "Hold"
    assert summary["needs_review"] is False
    assert summary["date"] == TRADE_DATE
    assert (summary["debate_rounds"], summary["risk_rounds"]) == (3, 3)

    database = _database(config)
    run_id = summary["run_id"]
    assert _json_artifact(database, run_id, "run.json") == summary
    assert database.get_run(run_id)["status"] == "completed"
    for name in REPORT_NAMES:
        assert database.read_artifact(run_id, f"reports/{name}.md")
    assert database.read_artifact(run_id, "reports/market_report.md") == "NVDA market_report 中文"
    assert (
        _json_artifact(database, run_id, "full_state.json")["market_report"]
        == "NVDA market_report 中文"
    )
    assert _json_artifact(database, run_id, "report_state.json")["final_trade_decision"] == "Hold"

    log = _log(database, run_id)
    assert log.count("[Tool Call] get_stock_data(symbol=NVDA)") == 1
    assert log.count("[Agent] NVDA market_report 中文") == 1
    assert "Save report?" not in outcome.output
    assert [call[0] for call in graphs[0].calls] == [
        "create",
        "begin",
        "stream",
        "json",
        "record",
        "clear",
        "end",
    ]
    _assert_no_native_directories(config)
    # --save-report remains the default, even with SQLite canonical storage.
    exported = Path(summary["report"])
    assert exported.is_file()
    assert exported.parent.parent == Path(config["results_dir"]) / "reports"
    assert "中文" in exported.read_text(encoding="utf-8")
    assert exported.with_suffix(".html").is_file()


def test_no_save_report_keeps_reports_in_database_only(sqlite_setup):
    config, _, runner = sqlite_setup
    outcome = runner.invoke(main.app, ["analyze", "NVDA", "--no-save-report", "--json"])
    assert outcome.exit_code == 0, outcome.output
    summary = json.loads(outcome.stdout)
    assert summary["report"] is None
    assert summary["status"] == "completed"
    database = _database(config)
    assert "中文" in database.read_artifact(summary["run_id"], "reports/market_report.md")
    assert _json_artifact(database, summary["run_id"], "report_state.json")["market_report"]
    _assert_no_native_directories(config)
    assert not (Path(config["results_dir"]) / "reports").exists()
    assert not list(Path(config["results_dir"]).rglob("*.md"))


def test_explicit_export_directory_does_not_relocate_database(sqlite_setup, tmp_path):
    config, graphs, runner = sqlite_setup
    export = tmp_path / "export 中文"
    export.mkdir()
    (export / "keep.txt").write_text("keep", encoding="utf-8")
    outcome = runner.invoke(main.app, ["analyze", "NVDA", "--output-dir", str(export), "--json"])
    assert outcome.exit_code == 0, outcome.output
    summary = json.loads(outcome.stdout)
    assert Path(summary["report"]) == export / "complete_report.md"
    assert "中文" in (export / "complete_report.md").read_text(encoding="utf-8")
    assert (export / "complete_report.html").is_file()
    assert (export / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert Path(summary["storage_db"]) == _database_path(config)
    assert graphs[0].config["results_dir"] == config["results_dir"]
    assert not (export / "runs.sqlite3").exists()
    _assert_no_native_directories(config)


def test_custom_database_path_needs_no_native_results_tree(sqlite_setup, tmp_path):
    config, _, runner = sqlite_setup
    config["storage_db_path"] = str(tmp_path / "archive 中文" / "analysis.sqlite3")
    outcome = runner.invoke(main.app, ["analyze", "NVDA", "--no-save-report", "--json"])
    assert outcome.exit_code == 0, outcome.output
    summary = json.loads(outcome.stdout)
    assert Path(summary["storage_db"]) == _database_path(config)
    assert _database_path(config).is_file()
    assert not (Path(config["results_dir"]) / "runs.sqlite3").exists()
    _assert_no_native_directories(config)
    assert not (Path(config["results_dir"]) / "reports").exists()


def test_results_override_moves_default_database_and_preserves_old_files(sqlite_setup, tmp_path):
    config, _, runner = sqlite_setup
    old_file = Path(config["results_dir"]) / "NVDA" / TRADE_DATE / "reports" / "keep.md"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("older filesystem run 中文", encoding="utf-8")
    root = tmp_path / "new-results"
    outcome = runner.invoke(
        main.app, ["analyze", "NVDA", "--results-dir", str(root), "--no-save-report", "--json"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert Path(json.loads(outcome.stdout)["storage_db"]) == root / "runs.sqlite3"
    assert not (root / "NVDA").exists()
    assert old_file.read_text(encoding="utf-8") == "older filesystem run 中文"
    assert not _database_path(config).exists()


def test_same_day_reruns_have_unique_ids_and_do_not_leak(sqlite_setup):
    config, _, runner = sqlite_setup
    database = None
    summaries = []
    first_log = None
    for ticker in ("GOOG", "MSFT", "GOOG"):
        outcome = runner.invoke(main.app, ["analyze", ticker, "--no-save-report", "--json"])
        assert outcome.exit_code == 0, outcome.output
        summaries.append(json.loads(outcome.stdout))
        database = _database(config)
        if first_log is None:
            first_log = _log(database, summaries[0]["run_id"])
        else:
            assert _log(database, summaries[0]["run_id"]) == first_log

    assert len({summary["run_id"] for summary in summaries}) == 3
    assert len(database.list_runs()) == 3
    assert len(database.list_runs(ticker="GOOG")) == 2
    for summary in summaries:
        ticker = summary["symbol"]
        other_ticker = "MSFT" if ticker == "GOOG" else "GOOG"
        log = _log(database, summary["run_id"])
        assert log.count(f"Selected ticker: {ticker}") == 1
        assert other_ticker not in log
        assert _json_artifact(database, summary["run_id"], "run.json") == summary
        assert (
            database.read_artifact(summary["run_id"], "reports/market_report.md")
            == f"{ticker} market_report 中文"
        )
        _assert_no_native_directories(config, ticker)
    assert not {"add_message", "add_tool_call", "update_report_section"}.intersection(
        vars(native.message_buffer)
    )


def test_tui_and_headless_share_archived_reports_and_lifecycle(sqlite_setup, monkeypatch):
    config, graphs, _ = sqlite_setup
    summary = headless.run_headless_analysis("NVDA", config=config, save_report=False)
    monkeypatch.setattr(
        native.typer, "prompt", lambda *args, **kwargs: pytest.fail("flagged run prompted")
    )
    result = native.run_analysis(
        config=config,
        selections=_selections(),
        flags={"save": False, "show": False},
        progress_mode="off",
    )
    database = _database(config)
    interactive_id = result.graph.run_store.run_id
    assert result.directory is None
    assert result.report is None
    assert interactive_id != summary["run_id"]
    assert database.get_run(interactive_id)["status"] == "completed"
    for name in (
        *[f"reports/{section}.md" for section in REPORT_NAMES],
        "report_state.json",
        "settings.json",
        "full_state.json",
    ):
        assert database.read_artifact(interactive_id, name) == database.read_artifact(
            summary["run_id"], name
        )
    assert [call[0] for call in graphs[0].calls] == [call[0] for call in graphs[1].calls]
    _assert_no_native_directories(config)


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_partial_failure_and_ctrl_c_persist_before_raising(
    sqlite_setup, monkeypatch, interactive, interrupted
):
    config, graphs, _ = sqlite_setup
    config["checkpoint_enabled"] = True
    original_factory = headless._create_graph
    failure = (
        KeyboardInterrupt() if interrupted else RuntimeError("synthetic-private-provider-error")
    )
    observed = []

    def factory(*args, **kwargs):
        graph = original_factory(*args, **kwargs)
        graph.failure = failure

        def probe(current, state):
            database = _database(config)
            run_id = current.run_store.run_id
            assert database.get_run(run_id)["status"] == "running"
            assert (
                database.read_artifact(run_id, "reports/market_report.md") == state["market_report"]
            )
            assert "[Tool Call]" in _log(database, run_id)
            observed.append(run_id)

        graph.probe = probe
        return graph

    monkeypatch.setattr(headless, "_create_graph", factory)
    monkeypatch.setattr(native, "_default_graph_factory", factory)
    monkeypatch.setattr(
        native.typer, "prompt", lambda *args, **kwargs: pytest.fail("failed run prompted")
    )
    with pytest.raises(type(failure)):
        if interactive:
            native.run_analysis(config=config, selections=_selections(), progress_mode="off")
        else:
            headless.run_headless_analysis("NVDA", config=config, progress_mode="off")

    assert observed
    assert [call[0] for call in graphs[0].calls] == ["create", "begin", "stream", "end"]
    database = _database(config)
    run_id = observed[0]
    status = "interrupted" if interrupted else "failed"
    assert database.get_run(run_id)["status"] == status
    assert database.read_artifact(run_id, "reports/market_report.md") == "NVDA market_report 中文"
    log = _log(database, run_id)
    assert "partial reports retained" in log
    assert "synthetic-private-provider-error" not in log
    assert not {"add_message", "add_tool_call", "update_report_section"}.intersection(
        vars(native.message_buffer)
    )
    if not interactive:
        summary = _json_artifact(database, run_id, "run.json")
        assert summary["status"] == status
        assert summary["decision"] is None
        assert summary["needs_review"] is True
        assert summary["report"] is None
        assert summary["output_dir"] is None
        assert summary["log_file"] is None
        assert "synthetic-private-provider-error" not in json.dumps(summary)
    _assert_no_native_directories(config)


def test_graph_constructor_failure_leaves_sanitized_failed_run(sqlite_setup, monkeypatch):
    config, graphs, _ = sqlite_setup

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-constructor-error")

    monkeypatch.setattr(headless, "_create_graph", fail)
    with pytest.raises(RuntimeError, match="synthetic-private-constructor-error"):
        headless.run_headless_analysis("NVDA", config=config)
    assert graphs == []
    database = _database(config)
    [row] = database.list_runs()
    assert row["status"] == "failed"
    summary = _json_artifact(database, row["run_id"], "run.json")
    assert summary["status"] == "failed"
    assert summary["decision"] is None
    assert "Selected ticker: NVDA" in _log(database, row["run_id"])
    assert "synthetic-private-constructor-error" not in json.dumps(summary) + _log(
        database, row["run_id"]
    )
    _assert_no_native_directories(config)


def test_export_failure_does_not_downgrade_completed_analysis(sqlite_setup, monkeypatch):
    config, graphs, runner = sqlite_setup

    def fail(*args, **kwargs):
        raise OSError("synthetic-private-export-error")

    monkeypatch.setattr("tradingagents.graph.trading_graph.write_report_tree", fail)
    outcome = runner.invoke(main.app, ["analyze", "NVDA", "--json"])
    assert outcome.exit_code == 1
    assert outcome.stdout == ""
    database = _database(config)
    [row] = database.list_runs()
    assert row["status"] == "completed"
    summary = _json_artifact(database, row["run_id"], "run.json")
    assert summary["status"] == "completed"
    assert summary["export_status"] == "failed"
    assert summary["decision"] == "Hold"
    assert summary["needs_review"] is False
    assert summary["report"] is None
    assert "synthetic-private-export-error" not in json.dumps(summary)
    assert (
        _json_artifact(database, row["run_id"], "report_state.json")["final_trade_decision"]
        == "Hold"
    )
    assert (
        _json_artifact(database, row["run_id"], "full_state.json")["final_trade_decision"] == "Hold"
    )
    assert [call[0] for call in graphs[0].calls][-3:] == ["record", "clear", "end"]
    _assert_no_native_directories(config)


def test_summary_and_settings_exclude_credentials_endpoints_and_holdings(sqlite_setup, tmp_path):
    config, graphs, runner = sqlite_setup
    config.update(
        backend_url="https://endpoint-secret.example.test/v1?token=synthetic-url-secret",
        llm_headers={"Authorization": "Bearer synthetic-header-secret"},
        api_key="synthetic-config-secret",
    )
    book = tmp_path / "private-portfolio.json"
    book.write_text(
        json.dumps(
            {
                "cash": 9876543.21,
                "currency": "CAD",
                "positions": [
                    {"ticker": "PRIVATE-HOLDING", "quantity": 12345, "average_price": 678}
                ],
            }
        ),
        encoding="utf-8",
    )
    outcome = runner.invoke(
        main.app, ["analyze", "NVDA", "--portfolio", str(book), "--no-save-report", "--json"]
    )
    assert outcome.exit_code == 0, outcome.output
    summary = json.loads(outcome.stdout)
    assert graphs[0].calls[0][4].positions[0].ticker == "PRIVATE-HOLDING"
    database = _database(config)
    stored = "\n".join(
        str(database.read_artifact(summary["run_id"], name))
        for name in ("run.json", "settings.json", "report_state.json", "full_state.json")
    )
    exposed = (
        outcome.output
        + stored
        + json.dumps(database.get_run(summary["run_id"]), ensure_ascii=False)
    )
    for secret in (
        "synthetic-url-secret",
        "synthetic-header-secret",
        "synthetic-config-secret",
        "endpoint-secret",
        "PRIVATE-HOLDING",
        "9876543.21",
    ):
        assert secret not in exposed
    for name in ("run.json", "settings.json", "report_state.json"):
        artifact = _json_artifact(database, summary["run_id"], name)
        assert not {"llm_headers", "backend_url", "api_key", "portfolio", "messages"}.intersection(
            artifact
        )


def test_injected_run_store_is_reused_without_creating_a_second_run(sqlite_setup, monkeypatch):
    config, graphs, _ = sqlite_setup
    store = create_run(config, "NVDA", TRADE_DATE)
    monkeypatch.setattr(
        native, "create_run", lambda *args, **kwargs: pytest.fail("injected store was replaced")
    )
    result = native.run_analysis(
        config=config,
        selections=_selections(),
        interactive=False,
        save_report=False,
        progress_mode="off",
        run_store=store,
    )
    assert graphs[0].run_store is store
    assert result.graph.run_store is store
    assert store.status == "completed"
    [row] = _database(config).list_runs()
    assert row["run_id"] == store.run_id
    assert result.directory is None
    _assert_no_native_directories(config)


@pytest.mark.parametrize("secondary_failure", ["append_log", "status"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_original_exception_survives_secondary_storage_failure(
    sqlite_setup, monkeypatch, secondary_failure, interrupted
):
    config, graphs, _ = sqlite_setup
    store = create_run(config, "NVDA", TRADE_DATE)
    original_factory = headless._create_graph
    failure = KeyboardInterrupt() if interrupted else RuntimeError("original-provider-error")
    unwinding = False
    secondary_seen = []

    class UnreliableStore:
        def __getattr__(self, name):
            return getattr(store, name)

        @property
        def status(self):
            if secondary_failure == "status" and unwinding:
                secondary_seen.append("status")
                raise RuntimeError("secondary-storage-read-error")
            return store.status

        def append_log(self, line):
            if secondary_failure == "append_log" and (
                "[System] Failed (" in line or "[System] Interrupted (" in line
            ):
                secondary_seen.append("append_log")
                raise RuntimeError("secondary-storage-write-error")
            return store.append_log(line)

    def factory(*args, **kwargs):
        graph = original_factory(*args, **kwargs)
        graph.failure = failure

        def probe(*args):
            nonlocal unwinding
            unwinding = True

        graph.probe = probe
        return graph

    monkeypatch.setattr(headless, "create_run", lambda *args, **kwargs: UnreliableStore())
    monkeypatch.setattr(headless, "_create_graph", factory)
    with pytest.raises(type(failure)) as caught:
        headless.run_headless_analysis("NVDA", config=config)
    assert caught.value is failure
    assert secondary_seen == [secondary_failure]
    assert [call[0] for call in graphs[0].calls] == ["create", "begin", "stream", "end"]
    assert store.status == ("interrupted" if interrupted else "failed")
    assert (
        _database(config).read_artifact(store.run_id, "reports/market_report.md")
        == "NVDA market_report 中文"
    )
    assert not {"add_message", "add_tool_call", "update_report_section"}.intersection(
        vars(native.message_buffer)
    )


def test_original_export_exception_survives_failed_metadata_read(sqlite_setup, monkeypatch):
    config, _, _ = sqlite_setup
    store = create_run(config, "NVDA", TRADE_DATE)
    failure = OSError("original-export-error")
    secondary_seen = []

    class UnreliableStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def get_metadata(self):
            secondary_seen.append(True)
            raise RuntimeError("secondary-metadata-read-error")

    def fail_export(*args, **kwargs):
        raise failure

    monkeypatch.setattr(headless, "create_run", lambda *args, **kwargs: UnreliableStore())
    monkeypatch.setattr("tradingagents.graph.trading_graph.write_report_tree", fail_export)
    with pytest.raises(OSError) as caught:
        headless.run_headless_analysis("NVDA", config=config)
    assert caught.value is failure
    assert secondary_seen == [True]
    assert store.status == "completed"
    assert (
        _json_artifact(_database(config), store.run_id, "report_state.json")["final_trade_decision"]
        == "Hold"
    )


def test_initial_archive_write_failure_is_not_silently_ignored(sqlite_setup, monkeypatch):
    config, graphs, _ = sqlite_setup
    store = create_run(config, "NVDA", TRADE_DATE)
    failure = RuntimeError("initial-log-write-error")

    class UnreliableStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def append_log(self, line):
            raise failure

    monkeypatch.setattr(headless, "create_run", lambda *args, **kwargs: UnreliableStore())
    with pytest.raises(RuntimeError) as caught:
        headless.run_headless_analysis("NVDA", config=config)
    assert caught.value is failure
    assert graphs == []
    assert store.status == "failed"
    assert _json_artifact(_database(config), store.run_id, "run.json")["status"] == "failed"
    assert not {"add_message", "add_tool_call", "update_report_section"}.intersection(
        vars(native.message_buffer)
    )
