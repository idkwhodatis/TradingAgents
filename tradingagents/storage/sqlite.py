"""SQLite metadata, compressed named artifacts, and small raw log events.

Connections are per operation, WAL enables concurrent readers, and all writes
use short atomic transactions. Retention deletes only rows in this database;
its budget measures payload bytes, not physical SQLite file sizes. Retention
is opt-in, dry-run by default, and never deletes running runs.
"""

import json
import math
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .codec import (
    CODEC,
    CODEC_VERSION,
    DEFAULT_MAX_ARTIFACT_BYTES,
    CorruptArtifactError,
    SchemaVersionError,
    StorageError,
    decode,
    encode,
    validate_header,
    validate_limit,
)

SCHEMA_VERSION = 1
APPLICATION_ID = 0x54415253  # TARS: TradingAgents Run Storage
MAX_LOG_BYTES = 8192
MAX_LOG_PAGE_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 32768
STATUSES = frozenset({"running", "completed", "failed", "cancelled", "interrupted"})
# Deliberately omit arbitrary config, URLs, headers, credentials, paths, error
# messages, and nested unknown fields. Reports/state belong in artifacts.
_METADATA_SCALARS = frozenset(
    {
        "version",
        "llm_provider",
        "quick_think_provider",
        "deep_think_provider",
        "quick_think_llm",
        "deep_think_llm",
        "output_language",
        "max_debate_rounds",
        "max_risk_discuss_rounds",
        "max_tool_rounds",
        "temperature",
        "max_tokens",
        "llm_max_retries",
        "google_thinking_level",
        "openai_reasoning_effort",
        "anthropic_effort",
        "checkpoint_enabled",
        "final_rating",
        "rating",
        "decision",
        "export_status",
        "duration_seconds",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "source",
        "report_status",
        "needs_review",
    }
)
_METADATA_LISTS = frozenset({"analysts", "selected_analysts"})
_VENDOR_CATEGORIES = frozenset(
    {
        "core_stock_apis",
        "technical_indicators",
        "fundamental_data",
        "news_data",
        "macro_data",
        "prediction_markets",
    }
)
_SAFE_LABEL = re.compile(r"^[\w .,+/@:^()\-]{0,256}$", re.UNICODE)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value) and "://" not in value:
        return value
    return _INVALID


_INVALID = object()


def sanitize_metadata(mapping: Mapping) -> dict:
    """Copy only bounded, JSON-safe, explicit non-secret metadata fields.

    This is an allowlist, not a promise to detect secrets pasted into model
    names. Never pass credentials in a field intended to name a model/provider.
    """
    if not isinstance(mapping, Mapping):
        raise TypeError("metadata must be a mapping")
    clean = {}
    for key in _METADATA_SCALARS:
        if key in mapping and (value := _scalar(mapping[key])) is not _INVALID:
            clean[key] = value
    for key in _METADATA_LISTS:
        values = mapping.get(key)
        if (
            isinstance(values, (list, tuple))
            and len(values) <= 32
            and all(isinstance(v, str) and _scalar(v) is not _INVALID for v in values)
        ):
            clean[key] = list(values)
    vendors = mapping.get("data_vendors")
    if isinstance(vendors, Mapping):
        clean["data_vendors"] = {
            key: value
            for key, value in vendors.items()
            if key in _VENDOR_CATEGORIES
            and isinstance(value, str)
            and _scalar(value) is not _INVALID
        }
    # Tool names are bounded identifiers, not arbitrary recursive configuration.
    vendors = mapping.get("tool_vendors")
    if isinstance(vendors, Mapping):
        clean["tool_vendors"] = {
            key: value
            for key, value in list(vendors.items())[:128]
            if isinstance(key, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key)
            and isinstance(value, str)
            and _scalar(value) is not _INVALID
            and not any(
                part in key for part in ("secret", "password", "token", "key", "header", "url")
            )
        }
    encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds the bounded metadata limit")
    return clean


def sqlite_enabled(config: Mapping) -> bool:
    backend = config.get("storage_backend", "filesystem")
    if backend not in {"filesystem", "sqlite"}:
        raise ValueError("storage_backend must be 'filesystem' or 'sqlite'")
    return backend == "sqlite"


def database_path(config: Mapping) -> Path:
    """Resolve the database under this run's results directory by default."""
    explicit = config.get("storage_db_path")
    if explicit:
        return Path(explicit).expanduser()
    results = config.get("results_dir")
    if not results:
        from tradingagents.default_config import DEFAULT_CONFIG

        results = DEFAULT_CONFIG["results_dir"]
    return Path(results).expanduser() / "runs.sqlite3"


def create_run(config: Mapping, ticker: str, trade_date: str, run_id: str | None = None):
    """Return a new scoped SQLite run, or None for legacy filesystem output."""
    if not sqlite_enabled(config):
        return None
    storage = SQLiteStorage(
        database_path(config),
        max_artifact_bytes=config.get("storage_max_artifact_bytes", DEFAULT_MAX_ARTIFACT_BYTES),
    )
    return storage.create_run(ticker, trade_date, run_id=run_id, metadata=config)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 512:
        raise ValueError("artifact name must be a nonempty relative path of at most 512 bytes")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(p in {".", "..", ""} for p in name.split("/"))
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValueError("artifact name must be a safe relative path")
    return name


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be a safe identifier of at most 128 characters")
    return run_id


def _bounded_text(value: str, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} must be a nonempty string of at most {limit} bytes")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must not contain control characters")
    return value


_SCHEMA = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled','interrupted')),
        metadata TEXT NOT NULL
    )""",
    "CREATE INDEX runs_created_at ON runs(created_at, run_id)",
    """CREATE TABLE artifacts (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        media_type TEXT NOT NULL,
        encoding TEXT NOT NULL,
        codec TEXT NOT NULL,
        codec_version INTEGER NOT NULL,
        raw_length INTEGER NOT NULL CHECK(raw_length >= 0),
        payload BLOB NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(run_id, name)
    )""",
    """CREATE TABLE log_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        line TEXT,
        payload BLOB,
        codec TEXT NOT NULL,
        codec_version INTEGER NOT NULL,
        raw_length INTEGER NOT NULL CHECK(raw_length >= 0),
        CHECK((codec='raw' AND line IS NOT NULL AND payload IS NULL
               AND length(CAST(line AS BLOB)) <= 8192)
           OR (codec='zstd' AND line IS NULL AND payload IS NOT NULL))
    )""",
    "CREATE INDEX log_events_run ON log_events(run_id, event_id)",
)

# The budget includes every stored payload: compressed artifacts, bounded raw
# logs, and bounded metadata. SQLite indexes/pages/free space are separate.
_RUN_BYTES = """length(CAST(r.metadata AS BLOB))
    + COALESCE((SELECT sum(length(a.payload)) FROM artifacts a WHERE a.run_id=r.run_id),0)
    + COALESCE((SELECT sum(COALESCE(length(l.payload),length(CAST(l.line AS BLOB)))) FROM log_events l WHERE l.run_id=r.run_id),0)"""


class SQLiteStorage:
    """A connectionless database facade, safe to use from threads/processes."""

    def __init__(self, database_path, *, max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES):
        self.database_path = Path(database_path).expanduser().resolve()
        self.max_artifact_bytes = validate_limit(max_artifact_bytes)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        return conn

    @staticmethod
    def _check_schema(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        application = conn.execute("PRAGMA application_id").fetchone()[0]
        if application != APPLICATION_ID or version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"unsupported run database schema (application={application}, version={version}); "
                "use a compatible TradingAgents version or a different database path"
            )

    def _initialize(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            application = conn.execute("PRAGMA application_id").fetchone()[0]
            objects = conn.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version == 0 and application == 0 and not objects:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                self._check_schema(conn)
            conn.commit()
            # This pragma must run outside a transaction. Concurrent processes
            # can initialize the same new database; briefly retry WAL lock races.
            for attempt in range(6):
                try:
                    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if mode.lower() != "wal":
                        raise StorageError("SQLite database could not enable WAL mode")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @contextmanager
    def _connection(self, *, write=False):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check_schema(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _require_run(conn, run_id):
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return row

    def create_run(self, ticker, trade_date, *, run_id=None, metadata=None):
        run_id = _validate_run_id(run_id if run_id is not None else uuid.uuid4().hex)
        ticker = _bounded_text(ticker, "ticker")
        trade_date = _bounded_text(trade_date, "trade_date", 64)
        clean = sanitize_metadata(metadata or {})
        now = _now()
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    ticker,
                    trade_date,
                    now,
                    now,
                    "running",
                    json.dumps(clean, ensure_ascii=False),
                ),
            )
        return SQLiteRunStore(self.database_path, run_id, _storage=self)

    @staticmethod
    def _run_dict(row):
        result = dict(row)
        try:
            result["metadata"] = json.loads(result["metadata"])
        except (TypeError, ValueError) as exc:
            raise StorageError("invalid run metadata") from exc
        if not isinstance(result["metadata"], dict):
            raise StorageError("invalid run metadata")
        return result

    def get_run(self, run_id):
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT r.*, ({_RUN_BYTES}) AS stored_bytes FROM runs r WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            return self._run_dict(row)

    def list_runs(self, *, limit=100, ticker=None, status=None):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        clauses, args = [], []
        if ticker is not None:
            clauses.append("r.ticker=?")
            args.append(ticker)
        if status is not None:
            if status not in STATUSES:
                raise ValueError("invalid run status")
            clauses.append("r.status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT r.*, ({_RUN_BYTES}) AS stored_bytes FROM runs r{where} "
                "ORDER BY r.created_at DESC,r.run_id DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
            return [self._run_dict(row) for row in rows]

    def write_artifact(self, run_id, name, content, media_type="text/plain"):
        name = _validate_name(name)
        media_type = _bounded_text(media_type, "media_type", 200)
        payload, raw_length, encoding = encode(content, self.max_artifact_bytes)
        now = _now()
        with self._connection(write=True) as conn:
            self._require_run(conn, run_id)
            conn.execute(
                """INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,name) DO UPDATE SET media_type=excluded.media_type,
                encoding=excluded.encoding,codec=excluded.codec,codec_version=excluded.codec_version,
                raw_length=excluded.raw_length,payload=excluded.payload,updated_at=excluded.updated_at""",
                (
                    run_id,
                    name,
                    media_type,
                    encoding,
                    CODEC,
                    CODEC_VERSION,
                    raw_length,
                    payload,
                    now,
                    now,
                ),
            )
            conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))

    @staticmethod
    def _artifact_dict(row):
        return dict(row)

    def list_artifacts(self, run_id):
        with self._connection() as conn:
            self._require_run(conn, run_id)
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT name,media_type,encoding,codec,codec_version,raw_length,"
                    "length(payload) AS stored_bytes,created_at,updated_at FROM artifacts "
                    "WHERE run_id=? ORDER BY name",
                    (run_id,),
                ).fetchall()
            ]

    def _read_artifact(self, conn, run_id, name):
        row = conn.execute(
            "SELECT codec,codec_version,raw_length,encoding,length(payload) AS stored_bytes "
            "FROM artifacts WHERE run_id=? AND name=?",
            (run_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact {name!r} for run {run_id}")
        validate_header(
            row["codec"],
            row["codec_version"],
            row["raw_length"],
            row["stored_bytes"],
            row["encoding"],
            self.max_artifact_bytes,
        )
        payload = conn.execute(
            "SELECT payload FROM artifacts WHERE run_id=? AND name=?", (run_id, name)
        ).fetchone()[0]
        if not isinstance(payload, bytes):
            raise CorruptArtifactError("artifact payload is not a blob")
        return decode(
            payload,
            row["codec"],
            row["codec_version"],
            row["raw_length"],
            row["encoding"],
            self.max_artifact_bytes,
        )

    def read_artifact(self, run_id, name):
        _validate_name(name)
        with self._connection() as conn:
            return self._read_artifact(conn, run_id, name)

    def append_log(self, run_id, line):
        if not isinstance(line, str):
            raise TypeError("log line must be a string")
        raw_length = len(line.encode("utf-8"))
        if raw_length <= min(MAX_LOG_BYTES, self.max_artifact_bytes):
            stored_line, payload, codec = line, None, "raw"
        else:
            payload, raw_length, _encoding = encode(line, self.max_artifact_bytes)
            stored_line, codec = None, CODEC
        now = _now()
        with self._connection(write=True) as conn:
            self._require_run(conn, run_id)
            cursor = conn.execute(
                "INSERT INTO log_events(run_id,created_at,line,payload,codec,codec_version,raw_length) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, now, stored_line, payload, codec, CODEC_VERSION, raw_length),
            )
            conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))
            return cursor.lastrowid

    def read_logs(self, run_id, *, after=0, limit=1000):
        """Read ordered events with a 4MiB aggregate decoded-byte page budget.

        One event may exceed the page budget, up to max_artifact_bytes; that
        event is returned alone so callers can always advance their cursor.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid log cursor or limit")
        with self._connection() as conn:
            self._require_run(conn, run_id)
            return self._read_logs(conn, run_id, after=after, limit=limit)

    def _read_logs(self, conn, run_id, *, after=0, limit=1000):
        headers = conn.execute(
            "SELECT event_id,created_at,codec,codec_version,raw_length,"
            "COALESCE(length(payload),length(CAST(line AS BLOB))) AS stored_bytes "
            "FROM log_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
            (run_id, after, limit),
        ).fetchall()
        result = []
        page_raw_bytes = 0
        for row in headers:
            # Apply aggregate bounds before fetching or decoding each payload.
            # Allow one bounded oversized event so pagination always advances.
            if type(row["raw_length"]) is not int or row["raw_length"] < 0:
                raise CorruptArtifactError("invalid log raw length")
            if result and page_raw_bytes + row["raw_length"] > MAX_LOG_PAGE_BYTES:
                break
            if row["codec"] == "raw":
                if (
                    row["codec_version"] != CODEC_VERSION
                    or type(row["raw_length"]) is not int
                    or row["stored_bytes"] != row["raw_length"]
                    or not 0 <= row["raw_length"] <= min(MAX_LOG_BYTES, self.max_artifact_bytes)
                ):
                    raise CorruptArtifactError("invalid raw log event")
                line = conn.execute(
                    "SELECT line FROM log_events WHERE event_id=?", (row["event_id"],)
                ).fetchone()[0]
                if not isinstance(line, str):
                    raise CorruptArtifactError("invalid raw log text")
            else:
                validate_header(
                    row["codec"],
                    row["codec_version"],
                    row["raw_length"],
                    row["stored_bytes"],
                    "utf-8",
                    self.max_artifact_bytes,
                )
                payload = conn.execute(
                    "SELECT payload FROM log_events WHERE event_id=?", (row["event_id"],)
                ).fetchone()[0]
                if not isinstance(payload, bytes):
                    raise CorruptArtifactError("log payload is not a blob")
                line = decode(
                    payload,
                    row["codec"],
                    row["codec_version"],
                    row["raw_length"],
                    "utf-8",
                    self.max_artifact_bytes,
                )
            page_raw_bytes += row["raw_length"]
            result.append(
                {
                    "event_id": row["event_id"],
                    "created_at": row["created_at"],
                    "line": line,
                    "truncated": False,
                }
            )
        return result

    def update_metadata(self, run_id, mapping):
        clean = sanitize_metadata(mapping)
        with self._connection(write=True) as conn:
            row = self._require_run(conn, run_id)
            metadata = self._run_dict(row)["metadata"]
            metadata.update(clean)
            # Enforce the total bound as well as the update's bound.
            metadata = sanitize_metadata(metadata)
            conn.execute(
                "UPDATE runs SET metadata=?,updated_at=? WHERE run_id=?",
                (json.dumps(metadata, ensure_ascii=False), _now(), run_id),
            )

    def finish(self, run_id, status="completed"):
        if status not in STATUSES - {"running"}:
            raise ValueError("finish status must be completed, failed, cancelled, or interrupted")
        with self._connection(write=True) as conn:
            self._require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, _now(), run_id)
            )

    def export_run(self, run_id, destination, *, render_reports=True, html=True):
        """Stream a consistent run snapshot into a new, exclusively published folder.

        Artifacts go under artifacts/, renderer output under reports/. A stable
        read transaction also covers logs, so concurrent appends cannot extend
        the export indefinitely and retention cannot delete its input midway.
        Only one artifact and a bounded log page are decoded at a time. A failed
        export removes its own staging folder and never modifies user files.
        """
        from .export import publish_directory

        destination = Path(destination).expanduser().absolute()
        # lexists also catches a broken symlink, which must not be replaced.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"export destination already exists: {destination}")
        stage = None
        try:
            with self._connection() as conn:
                metadata = self._run_dict(self._require_run(conn, run_id))
                names = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM artifacts WHERE run_id=? ORDER BY name", (run_id,)
                    )
                ]
                paths = set(names)
                for name in names:
                    _validate_name(name)
                    if any(str(parent) in paths for parent in PurePosixPath(name).parents):
                        raise StorageError("artifact paths collide during export")
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(
                    tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent)
                )
                (stage / "run.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                state, settings = None, {}
                for name in names:
                    content = self._read_artifact(conn, run_id, name)
                    path = stage / "artifacts" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(
                            content.encode("utf-8") if isinstance(content, str) else content
                        )
                    if render_reports and name in {"report_state.json", "settings.json"}:
                        try:
                            value = json.loads(content)
                        except (TypeError, ValueError, UnicodeError) as exc:
                            raise StorageError("report renderer inputs are not valid JSON") from exc
                        if not isinstance(value, dict):
                            raise StorageError("report renderer inputs must be JSON objects")
                        if name == "report_state.json":
                            state = value
                        else:
                            settings = value
                    del content
                with (stage / "logs.jsonl").open("x", encoding="utf-8") as stream:
                    after = 0
                    while rows := self._read_logs(conn, run_id, after=after):
                        for row in rows:
                            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        after = rows[-1]["event_id"]
            if state is not None:
                from tradingagents.reporting import write_report_tree

                write_report_tree(state, metadata["ticker"], stage / "reports", settings, html=html)
            publish_directory(stage, destination)
            stage = None
            return destination
        finally:
            if stage is not None:
                shutil.rmtree(stage)

    def _file_sizes(self):
        paths = {
            "database_bytes": self.database_path,
            "wal_bytes": Path(str(self.database_path) + "-wal"),
            "shm_bytes": Path(str(self.database_path) + "-shm"),
        }
        sizes = {}
        for key, path in paths.items():
            try:
                sizes[key] = path.stat().st_size
            except FileNotFoundError:
                sizes[key] = 0
        sizes["total_physical_bytes"] = sum(sizes.values())
        return sizes

    def statistics(self):
        with self._connection() as conn:
            compressed = conn.execute(
                "SELECT COALESCE(sum(length(payload)),0) FROM artifacts"
            ).fetchone()[0]
            logs = conn.execute(
                "SELECT COALESCE(sum(COALESCE(length(payload),length(CAST(line AS BLOB)))),0) FROM log_events"
            ).fetchone()[0]
            metadata = conn.execute(
                "SELECT COALESCE(sum(length(CAST(metadata AS BLOB))),0) FROM runs"
            ).fetchone()[0]
            count = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            running = conn.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]
            # Observe WAL sizes while the read connection is open.
            sizes = self._file_sizes()
        return {
            "run_count": count,
            "running_runs": running,
            "artifact_compressed_bytes": compressed,
            "log_stored_bytes": logs,
            "metadata_bytes": metadata,
            "logical_bytes": compressed + logs + metadata,
            **sizes,
        }

    def retention(self, *, max_age_days=None, max_bytes=None, dry_run=True, now=None):
        """Plan/apply oldest-first cleanup of completed, failed, cancelled runs.

        Running runs are protected even when they prevent meeting the quota.
        Age uses actual UTC creation time, never an analysis/trade date. No
        automatic expiry, physical file deletion, checkpoint, or VACUUM occurs.
        The same transaction plans and applies deletion to avoid writer races.
        """
        if type(dry_run) is not bool:
            raise TypeError("dry_run must be a boolean")
        if max_age_days is None and max_bytes is None:
            raise ValueError("supply max_age_days and/or max_bytes")
        if max_age_days is not None and (
            isinstance(max_age_days, bool)
            or not isinstance(max_age_days, (int, float))
            or not math.isfinite(max_age_days)
            or max_age_days < 0
        ):
            raise ValueError("max_age_days must be a finite nonnegative number")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
            raise ValueError("max_bytes must be a nonnegative integer")
        now = datetime.now(UTC) if now is None else now
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be a timezone-aware datetime")
        cutoff = (
            (now.astimezone(UTC) - timedelta(days=max_age_days))
            if max_age_days is not None
            else None
        )
        with self._connection(write=not dry_run) as conn:
            rows = conn.execute(
                f"SELECT r.run_id,r.created_at,r.status,({_RUN_BYTES}) AS stored_bytes "
                "FROM runs r ORDER BY r.created_at,r.run_id"
            ).fetchall()
            before = sum(row["stored_bytes"] for row in rows)
            remaining = before
            candidates = []
            for row in rows:
                if row["status"] == "running":
                    continue
                try:
                    created = datetime.fromisoformat(row["created_at"])
                    if created.tzinfo is None:
                        raise ValueError("missing UTC offset")
                except (ValueError, TypeError) as exc:
                    raise StorageError("invalid run creation time; refusing retention") from exc
                if cutoff is not None and created < cutoff:
                    candidates.append(row["run_id"])
                    remaining -= row["stored_bytes"]
            selected = set(candidates)
            if max_bytes is not None:
                for row in rows:
                    if remaining <= max_bytes:
                        break
                    if row["status"] != "running" and row["run_id"] not in selected:
                        candidates.append(row["run_id"])
                        selected.add(row["run_id"])
                        remaining -= row["stored_bytes"]
            if not dry_run:
                conn.executemany(
                    "DELETE FROM runs WHERE run_id=? AND status!='running'",
                    [(run_id,) for run_id in candidates],
                )
            return {
                "dry_run": dry_run,
                "candidate_run_ids": candidates,
                "deleted_run_ids": [] if dry_run else candidates,
                "reclaimed_bytes": before - remaining,
                "logical_bytes_before": before,
                "logical_bytes_after": remaining,
                "over_budget_bytes": max(0, remaining - max_bytes) if max_bytes is not None else 0,
                "protected_running_runs": sum(row["status"] == "running" for row in rows),
            }

    def vacuum(self):
        """Explicitly reclaim free database pages; never invoked by retention.

        SQLite may wait for other connections up to the busy timeout. This can
        take time and temporarily need additional free disk space. WAL remains
        enabled, and busy readers may prevent the final WAL truncate.
        """
        conn = self._connect()
        try:
            self._check_schema(conn)
            before = self._file_sizes()
            conn.execute("VACUUM")
            checkpoint = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            after = self._file_sizes()
            return {"before": before, "after": after, "checkpoint": checkpoint}
        finally:
            conn.close()


class SQLiteRunStore:
    """Explicit scoped run handle; no global run, no open connection to close."""

    def __init__(
        self, database_path, run_id, *, max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES, _storage=None
    ):
        self._storage = _storage or SQLiteStorage(
            database_path, max_artifact_bytes=max_artifact_bytes
        )
        self.database_path = self._storage.database_path
        self.run_id = _validate_run_id(run_id)
        self._storage.get_run(run_id)

    @property
    def status(self):
        return self._storage.get_run(self.run_id)["status"]

    @property
    def metadata(self):
        return self.get_metadata()

    def get_metadata(self):
        return self._storage.get_run(self.run_id)["metadata"]

    def get_run(self):
        return self._storage.get_run(self.run_id)

    def write_artifact(self, name, content, media_type="text/plain"):
        return self._storage.write_artifact(self.run_id, name, content, media_type)

    def read_artifact(self, name):
        return self._storage.read_artifact(self.run_id, name)

    def list_artifacts(self):
        return self._storage.list_artifacts(self.run_id)

    def append_log(self, line):
        return self._storage.append_log(self.run_id, line)

    def read_logs(self, *, after=0, limit=1000):
        return self._storage.read_logs(self.run_id, after=after, limit=limit)

    def update_metadata(self, mapping):
        return self._storage.update_metadata(self.run_id, mapping)

    def finish(self, status="completed"):
        return self._storage.finish(self.run_id, status)

    def export_run(self, destination, *, render_reports=True, html=True):
        return self._storage.export_run(
            self.run_id, destination, render_reports=render_reports, html=html
        )
