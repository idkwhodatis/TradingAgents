"""Shared CLI execution contract at the upstream graph lifecycle boundary.

This module does not build a graph, implement parallelism, persist memory, or
render reports. Those remain graph responsibilities. Both CLI frontends execute
this one lifecycle through ``cli.run``; observers only consume stream events.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AnalysisRequest:
    ticker: str
    trade_date: str
    asset_type: str
    portfolio: object | None = None


class GraphArguments(Protocol):
    def get_graph_args(self, *, callbacks: list) -> dict: ...


class AnalysisGraph(Protocol):
    """Upstream-owned operations; record_decision owns logging and memory."""

    propagator: GraphArguments

    def create_run_state(self, ticker, trade_date, asset_type, portfolio) -> dict: ...
    def begin_checkpoint(self, ticker, trade_date, asset_type, portfolio) -> str | None: ...
    def checkpoint_input(self, state: dict) -> dict | None: ...
    def stream_run(self, graph_input, **kwargs) -> Iterable[tuple[list, dict | None]]: ...
    def record_decision(self, ticker, trade_date, final_state: dict) -> None: ...
    def clear_checkpoint_on_success(self, ticker, trade_date, asset_type, portfolio) -> None: ...
    def end_checkpoint(self) -> None: ...


@dataclass(frozen=True)
class AnalysisObserver:
    """Consume messages even without state; observe checkpoints before streaming.

    Callbacks run synchronously, before state merging/interrupt detection. The
    caller owns display locking and message deduplication across snapshots.
    """

    on_chunk: Callable[[list, dict | None], None]
    on_checkpoint: Callable[[], None]


def execute_graph(
    request: AnalysisRequest,
    graph: AnalysisGraph,
    *,
    callbacks: list,
    observer: AnalysisObserver,
) -> dict:
    """Run once, merge report deltas, commit once, and always close checkpoints.

    Messages-only streams and interrupted/failed runs must not record a decision
    or clear resumable state. Memory lifecycle stays inside the graph hooks.
    """
    ticker, trade_date = request.ticker, request.trade_date
    asset_type, portfolio = request.asset_type, request.portfolio
    try:
        init_state = graph.create_run_state(ticker, trade_date, asset_type, portfolio)
        # The graph verifies bare mainland codes before checkpoint identity is
        # chosen. Use that same canonical symbol for memory and cleanup too.
        ticker = init_state.get("company_of_interest", ticker)
        checkpoint_tid = graph.begin_checkpoint(ticker, trade_date, asset_type, portfolio)
        if checkpoint_tid is not None:
            observer.on_checkpoint()
        args = graph.propagator.get_graph_args(callbacks=callbacks)
        if checkpoint_tid is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_tid
        final_state = None
        for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
            observer.on_chunk(messages, chunk)
            if chunk is None:
                continue
            if chunk.get("__interrupt__"):
                raise RuntimeError("Analysis paused at an interrupt; checkpoint retained")
            # Parallel analyst report deltas and top-level values share one state.
            if final_state is None:
                final_state = {}
            final_state.update(chunk)
        if final_state is None:
            raise RuntimeError("Analysis produced no state; checkpoint retained")
        graph.record_decision(ticker, trade_date, final_state)
        graph.clear_checkpoint_on_success(ticker, trade_date, asset_type, portfolio)
        return final_state
    finally:
        graph.end_checkpoint()
