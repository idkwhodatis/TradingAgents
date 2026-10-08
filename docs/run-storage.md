# Compressed SQLite run archives

Filesystem output remains the default. Opt in for **both the interactive TUI and
`tradingagents analyze`** with:

```sh
export TRADINGAGENTS_STORAGE_BACKEND=sqlite
# Optional: otherwise the archive is <results_dir>/runs.sqlite3
export TRADINGAGENTS_STORAGE_DB_PATH=/absolute/path/to/runs.sqlite3
export TRADINGAGENTS_SAVE_REPORT=false
tradingagents analyze NVDA --json
```

Programmatic configuration uses `storage_backend="sqlite"`, `storage_db_path`
(default `None`), and `storage_max_artifact_bytes` (default 64 MiB per decoded
artifact). The latter also has `TRADINGAGENTS_STORAGE_MAX_ARTIFACT_BYTES`.
`TradingAgentsGraph.propagate()` creates an independent archive run per call;
`graph.last_run_id` identifies it after success or failure without changing the
existing `(final_state, signal)` return value.
Changing these storage-only settings does not invalidate analytical checkpoints.
Do not point this database at a LangGraph checkpoint or any other application DB.

## What is saved

- Unique run ID, actual UTC creation time, ticker, analysis date, execution status
  and allowlisted metadata are queryable without decompression.
- Named artifacts contain changed partial Markdown sections, the existing final
  state JSON, and renderer inputs (`report_state.json`, `settings.json`). The
  renderer snapshot preserves the memory-note header without persisting runtime
  message objects, raw config, credentials, or portfolio objects.
- CLI message/tool logs and partial reports are committed incrementally, including
  before a failed/interrupted run. Completed analyses are marked separately from
  a later complete-report export failure. An abrupt process kill leaves a running
  record and whatever had already committed; there is no unreliable automatic
  inference that an old running record is dead.
- Long payloads use application-layer Zstandard with version, codec, decoded
  length and checksum validation. Small events may be stored raw. Compression is
  not encryption: treat the archive and its exports as private analysis data.
  Use the read/export API rather than directly decompressing BLOBs: the versioned
  payload includes an internal framing marker.
  Agent text/tool arguments can themselves contain sensitive information, just as
  the existing filesystem logs can.

SQLite is canonical for archived artifacts in this mode: no parallel native
`message_tool.log`, partial-report tree, `run.json`, or final-state JSON tree is
written. Headless summaries instead provide `storage_backend`, `storage_db` and
`run_id`; `output_dir` and `log_file` are null. `report` is a real exported path or
null. The TUI prints the archive path and run ID after completion.

`--no-save-report` still skips the **complete export only**, not journaling.
The built-in `save_report=True` remains backward compatible. Set
`TRADINGAGENTS_SAVE_REPORT=false` to skip complete exports by default, then use
`--save-report` to export a particular analysis. Explicit `--save-report` /
`--no-save-report` flags override the environment/default config. The interactive
Save report prompt uses that config as its default and still accepts a choice.
`--output-dir` requires exports enabled (add `--save-report` if your default is
false). Exports use the existing Markdown/HTML renderer and ordinary real files.

Setting `SAVE_REPORT=false` alone does **not** select SQLite: the filesystem
backend still journals native logs and partial reports. Use both settings above
to archive those artifacts in SQLite without an export directory. Neither setting
disables checkpoints, and changing the export preference does not invalidate them.
Exports are user-owned copies;
archive retention never deletes them. Re-export renders with the current renderer
and a fresh generated timestamp, rather than promising byte-identical HTML.

Memory Markdown, market-data caches, and checkpoint databases are untouched.
Defaulting the DB under `results_dir` preserves separate backtest/sweep roots.
An explicit shared `storage_db_path` intentionally overrides that isolation.
Programmatic `invoke` runs archive final results and lifecycle status; they do not
silently switch to streaming or capture partial graph output that `invoke` does
not expose. Existing resumable checkpoints remain the recovery mechanism there.

## Inspect, export, and retain

Use `python -m tradingagents.storage --help` for archive-management commands.

```sh
python -m tradingagents.storage --db /path/runs.sqlite3 list
python -m tradingagents.storage --db /path/runs.sqlite3 show RUN_ID --artifact run.json
python -m tradingagents.storage --db /path/runs.sqlite3 export RUN_ID ./new-export
python -m tradingagents.storage --db /path/runs.sqlite3 stats
# Preview only, no deletion:
python -m tradingagents.storage --db /path/runs.sqlite3 retention --max-age-days 90 --max-bytes 1073741824
# Add --apply only after reviewing the selection and backing up.
# Physical reclamation is a separate explicit operation:
python -m tradingagents.storage --db /path/runs.sqlite3 vacuum --apply
```
All cleanup defaults to a dry run. Retention uses **actual creation time**, not
historical analysis dates, and skips running runs. There is no automatic cleanup,
no automatic migration of legacy files, and no deletion of user files.

`max_bytes` is a logical quota for compressed artifact payloads, encoded logs and
metadata retained by this archive. It is **not a hard bound on the database,
WAL, filesystem overhead, temporary files, or peak disk usage**. Protected running
runs may leave the quota unsatisfied. Removing rows frees reusable SQLite pages;
it need not shrink the physical database. Explicit vacuum/reclamation is separate,
may need additional free disk space and can contend with writers. Run maintenance
while analyses are idle and keep a backup before applying retention. Never use
`PRAGMA max_page_count` as an archive-retention policy.

Archive re-export uses a stable read snapshot and a private staging directory,
then exclusively publishes the completed directory. It streams artifacts and caps
decoded log pages at 4 MiB, permitting one larger bounded event to make progress.
Exclusive publication supports Linux, macOS and Windows; unsupported operating
systems/filesystems fail safely rather than overwrite an existing destination.

The backend uses WAL, a busy timeout, per-operation connections and short
transactions. It holds no DB transaction across model or network calls. This
supports multiple processes on a local filesystem; SQLite WAL is not intended
for a shared network filesystem. Back up using SQLite's backup facilities or with
all writers stopped; copying the main file alone while WAL is active can omit
committed data.

## Fork maintenance

The implementation is mostly additive under `tradingagents/storage/`. The small
upstream-sensitive seams are:

1. `cli/run_output.py`: existing buffer journaling routes to the archive sink.
2. `cli/run.py` and `cli/headless.py`: pass one scoped run handle and record honest
   status/output identifiers; both UI paths still share the same execution loop.
3. `TradingAgentsGraph._log_state` / `propagate`: archive final state and lifecycle,
   keeping memory and checkpoint semantics separate.
4. Default config and checkpoint signature exclusions; one Zstandard dependency.

The DB uses generic artifact names rather than a column per analyst/report.
Renderer input keys are derived from the existing renderer's section definition;
review new renderer header fields when rebasing. New schema versions are rejected
rather than guessed at. Legacy filesystem data is left in place; migration is
intentionally not part of this feature.
