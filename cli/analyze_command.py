"""Fork-only, prompt-free command registration; keep upstream main routing small."""

import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated

import typer

from cli.headless import (
    AssetMode,
    ProviderAPI,
    ResearchEffort,
    build_headless_config,
    run_headless_analysis,
)


def register_analyze_command(
    app: typer.Typer,
    *,
    get_config: Callable[[], dict],
    resolve_progress: Callable[[bool | None, bool], str],
):
    """Register with explicit late-bound dependencies, also usable by another app.

    The command owns its flags and output. The hosting entry point owns runtime
    defaults and terminal policy; neither module reaches into the other's globals.
    """
    @app.command("analyze")
    def analyze_headless(
        symbol: Annotated[str, typer.Argument(help="Required ticker, e.g. NVDA, 0700.HK, BTC-USD.")],
        date: Annotated[
            str | None,
            typer.Option("--date", help="Analysis date (YYYY-MM-DD); default: today, local time."),
        ] = None,
        analysts: Annotated[
            str,
            typer.Option("--analysts", help="all, or comma-separated market,social,news,fundamentals."),
        ] = "all",
        effort: Annotated[
            ResearchEffort | None,
            typer.Option(
                "--effort",
                "--depth",
                case_sensitive=False,
                help="Research depth: shallow=1, medium=3, deep=5 debate/risk rounds. Default: medium unless env rounds are set.",
            ),
        ] = None,
        debate_rounds: Annotated[
            int | None, typer.Option("--debate-rounds", min=1, help="Override research debate rounds.")
        ] = None,
        risk_rounds: Annotated[
            int | None, typer.Option("--risk-rounds", min=1, help="Override risk discussion rounds.")
        ] = None,
        asset_type: Annotated[
            AssetMode, typer.Option("--asset-type", case_sensitive=False)
        ] = AssetMode.AUTO,
        provider: Annotated[
            str | None, typer.Option("--provider", help="LLM provider; default: .env / DEFAULT_CONFIG.")
        ] = None,
        quick_model: Annotated[
            str | None, typer.Option("--quick-model", help="Quick-thinking model ID.")
        ] = None,
        deep_model: Annotated[
            str | None, typer.Option("--deep-model", help="Deep-thinking model ID.")
        ] = None,
        backend_url: Annotated[
            str | None, typer.Option("--backend-url", help="LLM API base URL.")
        ] = None,
        header: Annotated[
            list[str] | None,
            typer.Option(
                "--header", help="Extra HTTP header 'Name: value'; repeatable. Keep secrets in .env."
            ),
        ] = None,
        language: Annotated[
            str | None, typer.Option("--language", help="Report language; default: .env / English.")
        ] = None,
        openai_reasoning_effort: Annotated[
            str | None,
            typer.Option(
                "--openai-reasoning-effort",
                help="OpenAI reasoning effort (also supported by routed OpenAI models); overrides .env.",
            ),
        ] = None,
        google_thinking_level: Annotated[
            str | None,
            typer.Option(
                "--google-thinking-level",
                help="Gemini thinking level, e.g. high or minimal; overrides .env.",
            ),
        ] = None,
        anthropic_effort: Annotated[
            str | None,
            typer.Option(
                "--anthropic-effort",
                help="Claude effort, e.g. low, medium, high or max; overrides .env.",
            ),
        ] = None,
        opencode_go_api: Annotated[
            ProviderAPI | None,
            typer.Option(
                "--opencode-go-api",
                case_sensitive=False,
                help="OpenCode Go protocol; auto routes known models. Set explicitly for a custom model.",
            ),
        ] = None,
        commandcode_api: Annotated[
            ProviderAPI | None,
            typer.Option(
                "--commandcode-api",
                case_sensitive=False,
                help="Command Code protocol; auto routes known models. Set explicitly for a custom model.",
            ),
        ] = None,
        temperature: Annotated[float | None, typer.Option("--temperature", min=0)] = None,
        max_tokens: Annotated[int | None, typer.Option("--max-tokens", min=1)] = None,
        max_retries: Annotated[int | None, typer.Option("--max-retries", min=0)] = None,
        checkpoint: Annotated[
            bool | None,
            typer.Option("--checkpoint/--no-checkpoint", help="Override checkpoint/resume setting."),
        ] = None,
        clear_checkpoints: Annotated[
            bool,
            typer.Option(
                "--clear-checkpoints",
                help="Delete ALL saved checkpoints before running, like the interactive flag.",
            ),
        ] = False,
        portfolio: Annotated[
            Path | None,
            typer.Option(
                "--portfolio",
                exists=True,
                dir_okay=False,
                readable=True,
                help="JSON holdings and cash.",
            ),
        ] = None,
        output_dir: Annotated[
            Path | None,
            typer.Option(
                "--output-dir",
                file_okay=False,
                help="Complete-report export directory (native Save path). Logs remain under results_dir/SYMBOL/DATE.",
            ),
        ] = None,
        results_dir: Annotated[
            Path | None,
            typer.Option(
                "--results-dir",
                file_okay=False,
                help="Override the native results root for ticker/date logs, JSON state and default report exports.",
            ),
        ] = None,
        progress: Annotated[
            bool | None,
            typer.Option(
                "--progress/--no-progress",
                help="Show the native dashboard on stderr. Default: on in terminals, off for --json/pipes. Explicit --progress uses plain updates without a terminal.",
            ),
        ] = None,
        save_report: Annotated[
            bool,
            typer.Option(
                "--save-report/--no-save-report",
                help="Export the complete Markdown report (default: on). Native section files and logs are always written.",
            ),
        ] = True,
        show_report: Annotated[
            bool,
            typer.Option(
                "--show-report/--no-show-report",
                help="Display the complete report after saving, without prompting. Uses stderr; --json stdout stays clean.",
            ),
        ] = False,
        html: Annotated[bool, typer.Option("--html/--no-html", help="Also export a self-contained HTML report.")] = True,
        json_output: Annotated[
            bool,
            typer.Option(
                "--json",
                help="Print one machine-readable JSON summary to stdout; diagnostics go to stderr.",
            ),
        ] = False,
    ):
        """Analyze SYMBOL without prompts and save reports automatically.

        Defaults: today, all applicable analysts, medium research depth. Uses the
        existing provider/model/key environment settings. Put options after analyze.
        """
        try:
            # Inspect the original streams before diagnostic redirection. Live UI
            # and plain progress always use stderr, leaving --json stdout parseable.
            progress_mode = resolve_progress(progress, json_output)
            with redirect_stdout(sys.stderr):
                config = build_headless_config(
                    get_config(),
                    effort=effort.value if effort is not None else None,
                    debate_rounds=debate_rounds,
                    risk_rounds=risk_rounds,
                    headers=header,
                    llm_provider=provider,
                    quick_think_llm=quick_model,
                    deep_think_llm=deep_model,
                    backend_url=backend_url,
                    output_language=language,
                    temperature=temperature,
                    openai_reasoning_effort=openai_reasoning_effort,
                    google_thinking_level=google_thinking_level,
                    anthropic_effort=anthropic_effort,
                    opencode_go_api=opencode_go_api.value if opencode_go_api is not None else None,
                    commandcode_api=commandcode_api.value if commandcode_api is not None else None,
                    max_tokens=max_tokens,
                    llm_max_retries=max_retries,
                    checkpoint_enabled=checkpoint,
                    results_dir=str(results_dir) if results_dir is not None else None,
                )
                result = run_headless_analysis(
                    symbol,
                    config=config,
                    analysis_date=date,
                    analysts=analysts,
                    asset_type=asset_type.value,
                    portfolio_path=portfolio,
                    output_dir=output_dir,
                    progress_mode=progress_mode,
                    show_report=show_report,
                    save_report=save_report,
                    html=html,
                    clear_checkpoints=clear_checkpoints,
                )
        except Exception as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None
        if json_output:
            typer.echo(json.dumps(result, ensure_ascii=False))
        else:
            typer.echo(f"Analysis complete: {result['symbol']} on {result['date']}")
            typer.echo(f"Decision: {result['decision']}")
            if result["needs_review"]:
                typer.echo("No rating could be parsed; review the saved report.", err=True)
            if result["report"]:
                typer.echo(f"Report: {result['report']}")
            if result.get("storage_backend") == "sqlite":
                typer.echo(f"Archive: {result['storage_db']}")
                typer.echo(f"Run ID: {result['run_id']}")
            else:
                typer.echo(f"Run directory: {result['output_dir']}")
                typer.echo(f"Message/tool log: {result['log_file']}")
    return analyze_headless
