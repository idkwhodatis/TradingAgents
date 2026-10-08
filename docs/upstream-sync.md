# CLI extension boundary and upstream sync checklist

The fork's prompt-free `analyze` command is registered in `cli/main.py` by
`cli/analyze_command.py`. The callback and `backtest` remain in the upstream
entry point. `get_config` and `resolve_progress` are explicit, late-bound host
policies, so command registration does not copy defaults or inspect another
module's globals. Flags, help, option ordering, JSON stdout and diagnostics are
owned by the command module. Root options must still be rejected before the
`analyze` subcommand when they belong after it.

## One execution path

Both the interactive callback and headless input adapter call
`cli.run.run_analysis`. It remains the compatibility entry point for resolved
selections and configuration, input collection, storage setup and report choices.
`cli.run._execute_analysis` owns presentation, callbacks, buffer journaling and
locking. It calls `cli.execution.execute_graph` once with an `AnalysisRequest`,
the existing graph and synchronous `AnalysisObserver` callbacks. There is no
second graph pipeline, monkey-patched module, or copied graph implementation.

`AnalysisGraph` documents the upstream-facing structural interface:

1. `create_run_state`, then `begin_checkpoint` for initial-state/checkpoint setup.
   Memory settlement and context preparation remain in the graph's Memory Log
   step during streaming.
2. Obtain graph arguments and add the checkpoint thread ID without discarding
   callbacks, recursion limits or existing configuration.
3. `checkpoint_input`, then `stream_run`, which owns parallel analyst execution.
   Observe messages even when there is no state chunk. Consume report deltas
   before merging them into the single final state. Observers run before the
   interrupt check, preserving partial journals.
4. `record_decision` once, then `clear_checkpoint_on_success` once. The graph
   owns final-state logging and memory recording; the CLI must not repeat either.
5. `end_checkpoint` in `finally`, including setup, observer, stream and recording
   failures. No-state streams and interrupts do not record or clear checkpoints.
   An empty dictionary chunk is a state, unlike a messages-only chunk.

The interface exposes behavior, not an implementation fork. New upstream graph
semantics must be reconciled here rather than hidden by keeping an old runner.
`AnalysisResult`, `_run_directory`, `_consume_state`, `_execute_analysis` and
`_offer_reports` remain available in `cli.run` for existing callers/tests.

## Merge-sensitive seams

- `cli/main.py`: the registration call and root/subcommand option guard.
- `cli/run.py`: graph construction, analyst plan/status tracking, the execution
  call, shared `run_config` context/lock, journaling and report policy.
- `cli/execution.py`: upstream graph lifecycle ordering and stream shape.
- `cli/headless.py`: input normalization and summary metadata around the same run.
- `cli/run_output.py` and storage: filesystem is still the default. SQLite runs
  share one store across journals, final state and headless summary. DB mode must
  not claim filesystem log/output paths. Interrupts retain partial data.
- Graph memory/checkpoint/report implementations: do not move them into a CLI
  extension. `save_reports(..., html=...)` remains the report-export boundary.
  A later export failure must not relabel an already completed analysis.

Provider selection and provider registries are outside this refactor.

## Checks for every upstream update

Read the upstream changes to CLI routing, graph streaming, memory, checkpoints
and report export even if Git reports no text conflict. Compare against these
contracts; resolve behavior changes intentionally rather than choosing an entire
side of a merge. In particular, exercise concurrent analysts (including their
messages-only chunks), resume after failure, memory preparation/recording and
HTML export, with both frontend modes and storage backends.

Run the offline suite and strict repository lint:

```sh
pytest -q
ruff check .
```

Focused boundary coverage is in `tests/test_cli_execution_contract.py`.
Existing behavior suites remain part of the contract:
`test_cli_commands.py`, `test_cli_no_console.py`, `test_headless_cli.py`,
`test_cli_tui_parity.py`, `test_headless_progress.py`, `test_cli_memory_log.py`,
`test_checkpoint_lifecycle.py`, `test_checkpoint_resume.py`,
`test_analyst_execution.py` and `test_cli_sqlite_storage.py`.
Also compare `python -m cli.main --help`, `analyze --help` and `backtest --help`
against the pre-merge version, including option names, defaults and help text.
The offline doubles cannot establish real provider/API reliability.
