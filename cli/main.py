import sys
from typing import Annotated

import typer

from cli.analyze_command import register_analyze_command
from cli.display import console
from cli.models import AnalystType, AssetType
from cli.progress import resolve_progress_mode
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: Annotated[
        bool | None,
        typer.Option(
            "--checkpoint/--no-checkpoint",
            help="Enable/disable checkpoint-resume (save state after each node so a "
            "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
        ),
    ] = None,
    clear_checkpoints: Annotated[
        bool,
        typer.Option(
            "--clear-checkpoints",
            help="Delete all saved checkpoints before running (force fresh start).",
        ),
    ] = False,
    portfolio: Annotated[
        str,
        typer.Option(
            "--portfolio",
            help="JSON file with current holdings and cash, so the trader, risk and "
            "portfolio agents size against your actual position.",
        ),
    ] = None,
    ticker: Annotated[str | None, typer.Option("--ticker", help="Ticker to analyze; skips the prompt.")] = None,
    date: Annotated[str | None, typer.Option("--date", help="Analysis date, YYYY-MM-DD; skips the prompt.")] = None,
    analysts: Annotated[str | None, typer.Option("--analysts", help="Comma-separated analysts; skips the prompt.")] = None,
    save: Annotated[bool | None, typer.Option("--save/--no-save", help="Save the report without asking.")] = None,
    show: Annotated[bool | None, typer.Option("--show/--no-show", help="Show the full report without asking.")] = None,
    html: Annotated[bool | None, typer.Option("--html/--no-html", help="Also save complete_report.html (default: yes).")] = None,
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        if ctx.invoked_subcommand == "analyze" and (
            checkpoint is not None or clear_checkpoints or portfolio is not None
            or any(value is not None for value in (ticker, date, analysts, save, show, html))
        ):
            raise typer.BadParameter(
                "Put analysis options after 'analyze'; use its SYMBOL argument and --save-report/--show-report flags."
            )
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints

        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: Annotated[str, typer.Argument(help="Comma-separated tickers, e.g. NVDA,AAPL")],
    start: Annotated[str, typer.Option("--start", help="First analysis date, YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="Last analysis date, YYYY-MM-DD")],
    every: Annotated[int, typer.Option("--every", help="Days between analysis dates")] = 7,
    analysts: Annotated[
        str, typer.Option("--analysts", help="Comma-separated analysts to run; omit for all four")
    ] = None,
    asset_type: Annotated[str, typer.Option("--asset-type", help="stock or crypto")] = "stock",
    portfolio: Annotated[
        str,
        typer.Option(
            "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
        ),
    ] = None,
    run_id: Annotated[
        str,
        typer.Option(
            "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
        ),
    ] = None,
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(
        f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}"
    )
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


# Fork-only command: keep its option surface out of upstream routing changes.
analyze_headless = register_analyze_command(
    app,
    get_config=lambda: DEFAULT_CONFIG,
    resolve_progress=lambda progress, json_output: resolve_progress_mode(progress, json_output),
)


if __name__ == "__main__":
    app()
