"""One CLI execution path: prompted and headless runs share streams and artifacts."""

from __future__ import annotations

import os
import sys
import time
import webbrowser
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import typer
from rich.console import Console
from rich.live import Live

from cli.display import (
    ANALYST_ORDER,
    AnalystWallTimeTracker,
    classify_message_type,
    console,
    create_layout,
    display_complete_report,
    message_buffer,
    update_analyst_statuses,
    update_display,
    update_research_team_status,
)
from cli.run_output import persist_run_buffer, run_directory
from cli.selections import depth_from_env, get_user_selections, unattended_gaps
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.storage import create_run

# The native dashboard uses a shared buffer. Serialize in-process CLI runs and
# protect rendering against concurrent deque updates. SDK work may use threads;
# the display remains an observer and never starts a second graph execution.
_RUN_LOCK = RLock()


@dataclass
class AnalysisResult:
    final_state: dict
    decision: str
    directory: Path | None
    report: Path | None = None
    progress: object | None = None
    graph: object | None = None


def _run_directory(config: dict, ticker: str, trade_date: str) -> Path:
    """Compatibility entry point for the native ticker/date directory helper."""
    return run_directory(config, ticker, trade_date)


def _announce_checkpoint_state(graph, ticker: str, trade_date: str) -> None:
    if getattr(graph, "_resuming", False):
        message_buffer.add_message("System", f"Resuming the saved run for {ticker} on {trade_date}")
    else:
        message_buffer.add_message("System", f"Starting fresh for {ticker} on {trade_date}")


def _build_run_config(selections: dict, checkpoint: bool | None) -> dict:
    """Interactive selections, with explicit environment/flag precedence."""
    config = DEFAULT_CONFIG.copy()
    for env_var, key in (("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "max_debate_rounds"),
                         ("TRADINGAGENTS_MAX_RISK_ROUNDS", "max_risk_discuss_rounds")):
        if not os.environ.get(env_var):
            config[key] = selections["research_depth"]
        elif not depth_from_env():
            console.print(
                f"[green]✓ {key} from environment:[/green] {config[key]} "
                f"(set by {env_var}, so the research depth you chose does not apply to it)"
            )
    config["quick_think_llm"] = selections["quick_think_llm"]
    config["deep_think_llm"] = selections["deep_think_llm"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    if config["llm_provider"] != str(DEFAULT_CONFIG.get("llm_provider", "")).lower():
        # Match the prompt-free path: custom authentication/routing headers
        # belong to the configured provider and must not follow a menu switch.
        config["llm_headers"] = None
        for tier in ("quick", "deep"):
            if not DEFAULT_CONFIG.get(f"{tier}_think_provider"):
                config[f"{tier}_think_backend_url"] = None
                config[f"{tier}_think_llm_headers"] = None
    # A provider without its own reasoning prompt (e.g. Go) must not erase
    # a reasoning option that DEFAULT_CONFIG already read from the environment.
    for key in ("google_thinking_level", "openai_reasoning_effort", "anthropic_effort"):
        if selections.get(key) is not None:
            config[key] = selections[key]
    for key in ("opencode_go_api", "commandcode_api"):
        if selections.get(key) is not None:
            config[key] = selections[key]
    config["output_language"] = selections.get("output_language", "English")
    if checkpoint is not None:
        config["checkpoint_enabled"] = checkpoint
    return config


def _consume_state(chunk: dict, tracker, seen_without_ids: set) -> None:
    """Update the native messages, report panels and incremental section files.

    Called for full `values` snapshots by BOTH CLI entry points. Canonical
    completed sections supersede temporary research/risk debate previews.
    """
    _consume_messages(chunk.get("messages", []), seen_without_ids)
    _consume_reports(chunk, tracker)


def _consume_messages(messages, seen_without_ids: set) -> None:
    """Journal top-level and analyst subgraph messages without duplicate snapshots."""
    for index, message in enumerate(messages):
        msg_id = getattr(message, "id", None)
        if msg_id is not None:
            if msg_id in message_buffer._processed_message_ids:
                continue
            message_buffer._processed_message_ids.add(msg_id)
        else:
            # LangGraph normally supplies IDs. This fallback avoids re-logging
            # identical snapshots from custom message implementations.
            signature = (index, type(message).__name__, repr(getattr(message, "content", None)),
                         repr(getattr(message, "tool_calls", None)))
            if signature in seen_without_ids:
                continue
            seen_without_ids.add(signature)
        msg_type, content = classify_message_type(message)
        if content and content.strip():
            message_buffer.add_message(msg_type, content)
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, dict):
                message_buffer.add_tool_call(call["name"], call["args"])
            else:
                message_buffer.add_tool_call(call.name, call.args)


def _consume_reports(chunk: dict, tracker) -> None:
    update_analyst_statuses(message_buffer, chunk, wall_time_tracker=tracker)
    debate = chunk.get("investment_debate_state") or {}
    bull, bear = (debate.get(key, "").strip() for key in ("bull_history", "bear_history"))
    judge = (chunk.get("investment_plan") or debate.get("judge_decision") or "").strip()
    if judge:
        update_research_team_status("completed")
        if message_buffer.agent_status.get("Trader") == "pending":
            message_buffer.update_agent_status("Trader", "in_progress")
    elif bull or bear:
        update_research_team_status("in_progress")
    research = chunk.get("investment_plan") or (
        f"### Research Manager Decision\n{judge}" if judge else
        f"### Bear Researcher Analysis\n{bear}" if bear else
        f"### Bull Researcher Analysis\n{bull}" if bull else None
    )
    if research:
        message_buffer.update_report_section("investment_plan", research)

    if chunk.get("trader_investment_plan"):
        message_buffer.update_report_section("trader_investment_plan", chunk["trader_investment_plan"])
        message_buffer.update_agent_status("Trader", "completed")
        if message_buffer.agent_status.get("Aggressive Analyst") == "pending":
            message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

    risk = chunk.get("risk_debate_state") or {}
    preview = None
    for key, agent in (("aggressive_history", "Aggressive Analyst"),
                       ("conservative_history", "Conservative Analyst"),
                       ("neutral_history", "Neutral Analyst")):
        history = risk.get(key, "").strip()
        if history:
            if message_buffer.agent_status.get(agent) != "completed":
                message_buffer.update_agent_status(agent, "in_progress")
            preview = f"### {agent} Analysis\n{history}"
    judge = (chunk.get("final_trade_decision") or risk.get("judge_decision") or "").strip()
    if judge:
        preview = f"### Portfolio Manager Decision\n{judge}"
        for agent in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"):
            message_buffer.update_agent_status(agent, "completed")
    decision = chunk.get("final_trade_decision") or preview
    if decision:
        message_buffer.update_report_section("final_trade_decision", decision)


def _default_graph_factory(analysts, config, callbacks):
    return TradingAgentsGraph(analysts, config=config, debug=False, callbacks=callbacks)


def _execute_analysis(selections, config, portfolio, mode, graph_factory, run_store=None) -> AnalysisResult:
    selected_set = {getattr(analyst, "value", analyst) for analyst in selections["analysts"]}
    if not selected_set or selected_set.difference(ANALYST_ORDER):
        raise ValueError("Select at least one known analyst")
    selected = [key for key in ANALYST_ORDER if key in selected_set]
    ticker, trade_date = selections["ticker"], selections["analysis_date"]
    directory = _run_directory(config, ticker, trade_date).resolve()
    if trade_date > get_current_date():
        raise ValueError("analysis date cannot be in the future")
    if mode not in {"live", "plain", "off"}:
        raise ValueError("progress mode must be live, plain or off")

    # Pin stderr before Live installs its proxies, preserving --json stdout.
    output_console = Console(file=sys.stderr)
    if mode == "plain":
        from cli.progress import AnalysisProgress

        stats = AnalysisProgress(ticker, trade_date, selected, console=output_console, plain=True)
    else:
        stats = StatsCallbackHandler()
    plan = build_analyst_execution_plan(selected)
    tracker = AnalystWallTimeTracker(plan)
    start_time = time.time()
    if mode == "plain":
        stats.stage("Preparing analysis")
    lock = RLock()
    with _RUN_LOCK, run_config(config):
        message_buffer.init_for_analysis(selected)
        with persist_run_buffer(message_buffer, directory, lock, run_store):
            # Journaling also works with --no-progress and --json, and starts
            # before SDK construction so a setup failure has a run log too.
            message_buffer.add_message("System", f"Selected ticker: {ticker}")
            if selections["asset_type"] != "stock":
                message_buffer.add_message("System", f"Detected asset type: {selections['asset_type']}")
            message_buffer.add_message("System", f"Analysis date: {trade_date}")
            message_buffer.add_message("System", f"Selected analysts: {', '.join(selected)}")
            try:
                graph = graph_factory(selected, config, [stats])
                if run_store is not None:
                    graph.run_store = run_store
                layout = create_layout() if mode == "live" else None

                def render():
                    with lock:
                        update_display(layout, stats_handler=stats, start_time=start_time)
                        return layout

                live = (Live(console=output_console, get_renderable=render, screen=True,
                             refresh_per_second=4, transient=True)
                        if mode == "live" else nullcontext())
                with live:
                    for spec in plan.specs:
                        message_buffer.update_agent_status(spec.agent_node, "in_progress")
                        tracker.mark_started(spec.key)
                    try:
                        init_state = graph.create_run_state(ticker, trade_date, selections["asset_type"], portfolio)
                        checkpoint_tid = graph.begin_checkpoint(ticker, trade_date, selections["asset_type"], portfolio)
                        if checkpoint_tid is not None:
                            _announce_checkpoint_state(graph, ticker, trade_date)
                        args = graph.propagator.get_graph_args(callbacks=[stats])
                        if checkpoint_tid is not None:
                            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_tid
                        final_state = None
                        seen_without_ids = set()
                        for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                            with lock:
                                _consume_messages(messages, seen_without_ids)
                                if chunk is not None:
                                    _consume_reports(chunk, tracker)
                            if chunk is None:
                                continue
                            if chunk.get("__interrupt__"):
                                raise RuntimeError("Analysis paused at an interrupt; checkpoint retained")
                            # stream_run includes analyst report deltas as well as
                            # top-level values; keep one merged state, not a trace.
                            if final_state is None:
                                final_state = {}
                            final_state.update(chunk)
                        if final_state is None:
                            raise RuntimeError("Analysis produced no state; checkpoint retained")
                        # One shared completion path: JSON state, memory, checkpoint.
                        graph.record_decision(ticker, trade_date, final_state)
                        graph.clear_checkpoint_on_success(ticker, trade_date, selections["asset_type"], portfolio)
                    finally:
                        graph.end_checkpoint()
                    with lock:
                        for agent in message_buffer.agent_status:
                            message_buffer.update_agent_status(agent, "completed")
                        for section in message_buffer.report_sections:
                            if final_state.get(section):
                                message_buffer.update_report_section(section, final_state[section])
                        message_buffer.add_message("System", f"Completed analysis for {trade_date}")
                        message_buffer.add_message("System", tracker.format_summary())
                decision = run_rating(final_state)
                if run_store is not None:
                    run_store.update_metadata({"decision": decision, "needs_review": decision == "REVIEW"})
                    run_store.finish("completed")
                return AnalysisResult(final_state, decision, directory if run_store is None else None,
                                      progress=stats if mode == "plain" else None, graph=graph)
            except BaseException as exc:
                # Preserve partial reports; log the failure type, not potentially
                # credential-bearing provider exception text. CLI reports it.
                phase = "Interrupted" if isinstance(exc, KeyboardInterrupt) else "Failed"
                with suppress(Exception):
                    message_buffer.add_message("System", f"{phase} ({type(exc).__name__}); partial reports retained")
                if mode == "plain":
                    stats.fail(exc)
                raise


def run_analysis(checkpoint: bool | None = None, portfolio=None, flags=None, *, selections=None,
                 config=None, interactive=True, output_dir: Path | None = None,
                 progress_mode: str | None = None, show_report=False,
                 save_report=True, html: bool | None = None, clear_checkpoints=False,
                 graph_factory=None, run_store=None) -> AnalysisResult:
    """Use the same native CLI workflow, optionally with pre-resolved inputs.

    Headless callers supply both selections and config. Only input collection
    and post-run questions differ: headless exports automatically, optionally
    prints the complete report, and never reads/persists interactive preferences.
    """
    if selections is None:
        if not interactive:
            raise ValueError("Headless analysis requires resolved selections")
        if not (sys.stdin and sys.stdin.isatty()):
            gaps = unattended_gaps(flags or {})
            if gaps:
                console.print("[red]No terminal to answer the setup questions. Set:[/red]")
                for gap in gaps:
                    console.print(f"  {gap}")
                raise typer.Exit(code=1)
        selections = get_user_selections(flags) if flags is not None else get_user_selections()
    if config is None:
        config = _build_run_config(selections, checkpoint)
    mode = progress_mode if progress_mode is not None else ("live" if interactive else "off")
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints

        count = clear_all_checkpoints(config["data_cache_dir"])
        typer.echo(f"Cleared {count} checkpoint(s).", err=True)
    if run_store is None:
        run_store = create_run(config, selections["ticker"], selections["analysis_date"])
    try:
        result = _execute_analysis(selections, config, portfolio, mode,
                                   graph_factory or _default_graph_factory, run_store)
    except BaseException as exc:
        if run_store is not None:
            with suppress(Exception):
                run_store.finish("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed")
        raise

    flags = flags or {}
    if interactive:
        console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")
        if run_store is not None:
            console.print(f"Archive: {run_store.database_path} (run {run_store.run_id})")
        if is_review(result.decision):
            console.print("[yellow]No rating could be read. Review the saved decision text rather than treating it as a position.[/yellow]")
    result.report = _offer_reports(
        result.final_state, result.graph, selections["ticker"],
        save=flags.get("save") if interactive else save_report,
        show=flags.get("show") if interactive else show_report,
        html=flags.get("html", html) if interactive else html,
        output_dir=output_dir, progress=result.progress, strict=not interactive,
        announce=interactive,
    )
    return result


def _yes(question: str) -> bool:
    return typer.prompt(question, default="Y").strip().upper() in ("Y", "YES", "")


def _graphical_browser():
    """A browser that opens a page in its own window on this machine, or None.

    Over SSH the page sits on the remote machine, and a terminal browser (lynx,
    w3m, elinks) would take over the terminal, so neither is offered.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return None
    if type(browser) is webbrowser.GenericBrowser or isinstance(browser, webbrowser.Elinks):
        return None
    return browser


def _open_page(page: Path) -> None:
    """Offer to open the saved page in a browser window, and say where it is if opening fails."""
    browser = _graphical_browser()
    if browser is None or not _yes("Open it in your browser?"):
        return
    try:
        opened = browser.open(page.as_uri())
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        console.print(f"  [dim]Could not open a browser; the page is at:[/dim] {page}")


def _offer_reports(final_state, graph, ticker, save=None, show=None, html=None, *,
                   output_dir=None, progress=None, strict=False, announce=True):
    """Save the report tree and show it; ``save``/``show``/``html`` answer the questions when given.

    A saved report includes the HTML page unless ``html`` is False. Someone
    answering the save question at the prompt is also asked about the page and
    offered to open it; a run whose flags answer the save question asks neither.
    """
    report_file = None
    asked = save is None
    if asked:
        save = typer.prompt("Save report?", default="Y").strip().upper() in ("Y", "YES", "")
    if save:
        # Under results_dir, not the working directory: in Docker the working
        # directory is inside the container and the report goes with it, while
        # results_dir is the mounted volume the rest of the run already writes to.
        save_path = (Path(output_dir).expanduser() if output_dir is not None
                     else graph.default_report_path(ticker).expanduser())
        if asked:   # someone at the prompt may pick another folder
            save_path = Path(typer.prompt(
                "Save path (press Enter for default)", default=str(save_path)
            ).strip()).expanduser()
        if html is None:
            html = _yes("Also save it as an HTML page?") if asked else True
        saved = False
        if progress is not None:
            progress.stage("Saving reports")
        try:
            report_file = graph.save_reports(final_state, ticker, save_path, html=html).resolve()
            saved = True
            if progress is not None and not strict:
                progress.complete()
            if announce:
                console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
                console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as exc:
            if progress is not None:
                progress.fail(exc)
            if strict:
                raise
            console.print(f"[red]Error saving report: {exc}[/red]")
        if saved and html:
            page = (save_path / "complete_report.html").resolve()
            if announce:
                console.print(f"  [dim]HTML report:[/dim] {page.name}")
            if asked:
                _open_page(page)

    if not save and progress is not None:
        progress.stage("Completed; incremental reports saved")
    if show is None:
        show = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper() in ("Y", "YES", "")
    if show:
        display_complete_report(final_state)
    return report_file
