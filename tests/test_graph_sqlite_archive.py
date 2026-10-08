"""Programmatic archiving is independent from execution/checkpoint identity."""

import json
from contextlib import nullcontext
from copy import deepcopy

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.storage import SQLiteStorage, create_run
from tradingagents.storage.integration import persist_report


def graph(config):
    result = object.__new__(TradingAgentsGraph)
    result.config = config
    result.selected_analysts = ["market"]
    return result


def test_storage_changes_do_not_invalidate_checkpoint_signature(tmp_path):
    config = deepcopy(DEFAULT_CONFIG)
    instance = graph(config)
    before = instance._run_signature("stock")
    config.update(storage_backend="sqlite", storage_db_path=str(tmp_path / "archive.sqlite3"),
                  storage_max_artifact_bytes=1024)
    assert instance._run_signature("stock") == before


@pytest.mark.parametrize("failure,status", [(None, "completed"), (RuntimeError, "failed"),
                                            (KeyboardInterrupt, "interrupted")])
def test_propagate_allocates_unique_archive_per_invocation(tmp_path, failure, status):
    config = {"storage_backend": "sqlite", "results_dir": str(tmp_path)}
    instance = graph(config)
    instance.checkpoint_scope = lambda *args: nullcontext(None)
    seen = []

    def run(*args, **kwargs):
        seen.append(instance.run_store.run_id)
        instance.run_store.write_artifact("partial.txt", "already durable 中文")
        if failure:
            raise failure("private error detail")
        return {}, "Hold"

    instance._run_graph = run
    for _ in range(2):
        if failure:
            with pytest.raises(failure):
                instance.propagate("NVDA", "2026-01-01")
        else:
            assert instance.propagate("NVDA", "2026-01-01") == ({}, "Hold")
        assert instance.run_store is None
        assert instance.last_run_id == seen[-1]
    assert len(set(seen)) == 2
    archive = SQLiteStorage(tmp_path / "runs.sqlite3")
    for run_id in seen:
        assert archive.get_run(run_id)["status"] == status
        assert archive.read_artifact(run_id, "partial.txt") == "already durable 中文"


def test_renderer_snapshot_keeps_memory_note_without_runtime_or_raw_config(tmp_path):
    store = create_run({"storage_backend": "sqlite", "results_dir": str(tmp_path)}, "NVDA", "2026-01-01")
    state = {"company_of_interest": "NVDA", "trade_date": "2026-01-01", "market_report": "报告",
             "final_trade_decision": "Hold", "memory_note": "Prior lesson 中文", "final_rating": "Hold",
             "messages": [object()], "portfolio": {"secret": "private holdings"}}
    persist_report(store, state, {"analysts": ["market"]})
    result = json.loads(store.read_artifact("report_state.json"))
    assert result["memory_note"] == state["memory_note"]
    assert result["market_report"] == "报告"
    assert "messages" not in result
    assert "portfolio" not in result
    destination = store.export_run(tmp_path / "re-export")
    markdown = (destination / "reports" / "complete_report.md").read_text()
    html = (destination / "reports" / "complete_report.html").read_text()
    assert state["memory_note"] in markdown
    assert "报告" in markdown and "报告" in html
    assert "private holdings" not in markdown
