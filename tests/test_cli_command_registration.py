"""The fork command can be hosted without importing or rewriting cli.main."""

import json

import typer
from typer.testing import CliRunner

from cli import analyze_command


def test_command_dependencies_are_explicit_and_resolved_per_invocation(monkeypatch):
    app = typer.Typer()

    @app.callback()
    def root():
        pass

    current = {"settings": {"generation": 1}}
    modes, runs = [], []

    def resolve(progress, json_output):
        modes.append((progress, json_output))
        return "off"

    def run(symbol, **kwargs):
        runs.append((symbol, kwargs))
        return {"symbol": symbol, "decision": "Hold"}

    monkeypatch.setattr(analyze_command, "build_headless_config", lambda base, **kwargs: base)
    monkeypatch.setattr(analyze_command, "run_headless_analysis", run)
    command = analyze_command.register_analyze_command(
        app, get_config=lambda: current["settings"], resolve_progress=resolve,
    )
    assert command.__module__ == "cli.analyze_command"
    runner = CliRunner()
    for generation in (1, 2):
        current["settings"] = {"generation": generation}
        result = runner.invoke(app, ["analyze", "NVDA", "--json", "--no-progress"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {"symbol": "NVDA", "decision": "Hold"}
        assert runs[-1][1]["config"] is current["settings"]
        assert runs[-1][1]["progress_mode"] == "off"
    assert modes == [(False, True), (False, True)]
