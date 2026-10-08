"""The CLI observes an upstream graph lifecycle; it never owns memory or streaming."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cli import execution
from cli.execution import AnalysisObserver, AnalysisRequest, execute_graph
from tests.test_cli_sqlite_storage import _database, _selections, sqlite_setup as sqlite_setup

REQUEST = AnalysisRequest("NVDA", "2026-09-27", "stock", portfolio=object())


class ContractGraph:
    """Only graph hooks may touch the simulated memory lifecycle."""

    def __init__(self, chunks=(), *, thread_id=None, resume=False, failure=None):
        self.chunks = chunks
        self.thread_id, self.resume, self.failure = thread_id, resume, failure
        self.calls, self.observed, self.memory = [], [], []
        self.initial = {"initial_only": True}
        self.args = {"config": {"recursion_limit": 99, "configurable": {"other": "keep"}}}
        self.propagator = SimpleNamespace(get_graph_args=Mock(return_value=self.args))
        self.observer = AnalysisObserver(self.observe, lambda: self.calls.append("checkpoint"))

    def step(self, name):
        self.calls.append(name)
        if name == self.failure:
            raise RuntimeError(name)

    def create_run_state(self, *args):
        assert args == (REQUEST.ticker, REQUEST.trade_date, REQUEST.asset_type, REQUEST.portfolio)
        self.step("create")
        return self.initial

    def begin_checkpoint(self, *args):
        assert args == (REQUEST.ticker, REQUEST.trade_date, REQUEST.asset_type, REQUEST.portfolio)
        self.step("begin")
        return self.thread_id

    def checkpoint_input(self, state):
        assert state is self.initial
        self.step("input")
        return None if self.resume else state

    def stream_run(self, graph_input, **kwargs):
        self.step("stream")
        self.stream_input, self.stream_args = graph_input, kwargs
        yield from self.chunks

    def observe(self, messages, state):
        self.step("observe")
        self.observed.append((messages, state))

    def record_decision(self, ticker, trade_date, state):
        assert (ticker, trade_date) == (REQUEST.ticker, REQUEST.trade_date)
        self.step("record")
        self.recorded = state
        self.memory.append(state)  # The graph hook alone owns memory persistence.

    def clear_checkpoint_on_success(self, *args):
        assert args == (REQUEST.ticker, REQUEST.trade_date, REQUEST.asset_type, REQUEST.portfolio)
        self.step("clear")

    def end_checkpoint(self):
        self.step("end")

    def __getattr__(self, name):
        raise AssertionError(f"CLI accessed a non-contract graph operation: {name}")


def run(graph, callbacks=None):
    return execute_graph(REQUEST, graph, callbacks=callbacks or [], observer=graph.observer)


def test_messages_only_are_observed_and_parallel_report_deltas_merge_once():
    chunks = [(["token"], None), ([], {"market_report": "market"}),
              (["news"], {"news_report": "news"}),
              ([], {"market_report": "revised", "final_trade_decision": "Hold"})]
    graph = ContractGraph(chunks)
    result = run(graph)
    assert graph.observed == chunks
    assert all(actual[1] is original[1] for actual, original in zip(graph.observed, chunks, strict=True))
    assert result == {"market_report": "revised", "news_report": "news", "final_trade_decision": "Hold"}
    assert graph.recorded is result
    assert graph.memory == [result]
    assert graph.calls == ["create", "begin", "input", "stream", *(["observe"] * 4), "record", "clear", "end"]
    assert chunks[1][1] == {"market_report": "market"}  # No mutation of upstream deltas.
    assert graph.initial == {"initial_only": True}


@pytest.mark.parametrize("thread_id,resume", [(None, False), ("new-thread", False), ("saved-thread", True)])
def test_checkpoint_resume_input_thread_id_and_callbacks_are_forwarded(thread_id, resume):
    graph = ContractGraph([([], {})], thread_id=thread_id, resume=resume)
    callbacks = [object()]
    assert run(graph, callbacks) == {}
    assert graph.calls[-3:] == ["record", "clear", "end"]
    assert graph.memory == [{}]  # An empty state is valid, unlike no state at all.
    assert graph.stream_input is (None if resume else graph.initial)
    graph.propagator.get_graph_args.assert_called_once_with(callbacks=callbacks)
    assert graph.stream_args["config"]["recursion_limit"] == 99
    assert graph.stream_args["config"]["configurable"] == {
        "other": "keep", **({"thread_id": thread_id} if thread_id is not None else {})}
    assert graph.calls[:4] == (["create", "begin", "checkpoint", "input"] if thread_id is not None
                               else ["create", "begin", "input", "stream"])


@pytest.mark.parametrize("chunks,message", [
    ([], "produced no state"),
    ([(["token"], None)], "produced no state"),
    ([([], {"market_report": "partial"}), (["pause"], {"__interrupt__": ("wait",)})], "paused at an interrupt"),
])
def test_missing_state_and_interrupts_are_observed_but_never_committed(chunks, message):
    graph = ContractGraph(chunks, thread_id="retained")
    with pytest.raises(RuntimeError, match=message):
        run(graph)
    assert graph.observed == chunks  # Includes the interrupt event before raising.
    assert graph.memory == []
    assert "record" not in graph.calls and "clear" not in graph.calls
    assert graph.calls[-1] == "end"


@pytest.mark.parametrize("failure,expected", [
    ("create", ["create", "end"]),
    ("begin", ["create", "begin", "end"]),
    ("stream", ["create", "begin", "input", "stream", "end"]),
    ("observe", ["create", "begin", "input", "stream", "observe", "end"]),
    ("record", ["create", "begin", "input", "stream", "observe", "record", "end"]),
    ("clear", ["create", "begin", "input", "stream", "observe", "record", "clear", "end"]),
    ("end", ["create", "begin", "input", "stream", "observe", "record", "clear", "end"]),
])
def test_each_lifecycle_failure_closes_once_without_later_success_hooks(failure, expected):
    graph = ContractGraph([([], {"final_trade_decision": "Hold"})], failure=failure)
    with pytest.raises(RuntimeError, match=f"^{failure}$"):
        run(graph)
    assert graph.calls == expected
    assert len(graph.memory) == (1 if failure in {"clear", "end"} else 0)


@pytest.mark.parametrize("callback", ["checkpoint", "graph_args"])
def test_setup_callback_failure_retains_checkpoint_and_closes(callback):
    graph = ContractGraph([([], {})], thread_id="retained")
    failure = RuntimeError("callback failed")
    if callback == "checkpoint":
        graph.observer = AnalysisObserver(graph.observe, Mock(side_effect=failure))
    else:
        graph.propagator.get_graph_args.side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        run(graph)
    assert caught.value is failure
    assert graph.calls == ["create", "begin", *(["checkpoint"] if callback == "graph_args" else []), "end"]
    assert graph.memory == []


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
def test_both_frontends_use_shared_contract_with_real_persistence(sqlite_setup, monkeypatch, backend):
    from pathlib import Path

    import cli.headless as headless
    import cli.run as native

    config, graphs, _ = sqlite_setup
    config["storage_backend"] = backend
    if backend == "filesystem":
        from tests.native_cli_helpers import RecordingGraph
        from tests.test_cli_sqlite_storage import ArchiveRecordingGraph

        # SQLite's helper asserts archive attachment, which filesystem mode
        # intentionally does not have. Keep its real final-state persistence.
        monkeypatch.setattr(ArchiveRecordingGraph, "create_run_state", RecordingGraph.create_run_state)
    shared = Mock(wraps=execute_graph)
    monkeypatch.setattr(execution, "execute_graph", shared)
    summary = headless.run_headless_analysis("NVDA", config=config, save_report=False, progress_mode="off")
    interactive = native.run_analysis(config=config, selections=_selections(),
                                      flags={"save": False, "show": False}, progress_mode="off")
    assert shared.call_count == 2
    assert [call.args[1] for call in shared.call_args_list] == graphs
    assert shared.call_args_list[0].args[0] == shared.call_args_list[1].args[0]
    assert [call[0] for call in graphs[0].calls] == [call[0] for call in graphs[1].calls]
    assert [call[0] for call in graphs[0].calls][-3:] == ["record", "clear", "end"]
    expected = "NVDA market_report 中文"
    assert interactive.final_state["market_report"] == expected
    if backend == "sqlite":
        database = _database(config)
        for run_id in (summary["run_id"], graphs[1].run_store.run_id):
            assert database.get_run(run_id)["status"] == "completed"
            assert database.read_artifact(run_id, "reports/market_report.md") == expected
    else:
        assert (Path(summary["output_dir"]) / "reports" / "market_report.md").read_text(encoding="utf-8") == expected
        assert (interactive.directory / "reports" / "market_report.md").read_text(encoding="utf-8") == expected
