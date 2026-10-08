"""Local-only tests for compressed storage, retention and independent writers."""

import json
import multiprocessing
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import zstandard

from tradingagents.storage import (
    CorruptArtifactError,
    SchemaVersionError,
    SQLiteRunStore,
    SQLiteStorage,
    create_run,
    database_path,
    sanitize_metadata,
    sqlite_enabled,
)
from tradingagents.storage.codec import CODEC_VERSION, DEFAULT_MAX_ARTIFACT_BYTES, encode
from tradingagents.storage.sqlite import APPLICATION_ID, MAX_LOG_BYTES, SCHEMA_VERSION

pytestmark = pytest.mark.unit


@pytest.fixture
def storage(tmp_path):
    return SQLiteStorage(tmp_path / "runs.sqlite3")


@pytest.fixture
def run(storage):
    return storage.create_run("AAPL", "2000-01-02")


def _alter(store, sql, args=()):
    with sqlite3.connect(store.database_path) as conn:
        conn.execute(sql, args)


def test_filesystem_default_does_not_create_storage(tmp_path):
    config = {"results_dir": str(tmp_path)}
    assert not sqlite_enabled(config)
    assert create_run(config, "AAPL", "2026-01-01") is None
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="storage_backend"):
        sqlite_enabled({"storage_backend": "sqltie"})


def test_factory_paths_stay_in_separate_results_directories(tmp_path):
    configs = [
        {"results_dir": str(tmp_path / p), "storage_backend": "sqlite"}
        for p in ("main", "backtest")
    ]
    first, second = [create_run(config, "AAPL", "2026-01-01") for config in configs]
    assert first.database_path != second.database_path
    assert first.database_path == tmp_path / "main" / "runs.sqlite3"
    assert (
        database_path({**configs[0], "storage_db_path": tmp_path / "explicit.db"})
        == tmp_path / "explicit.db"
    )
    assert len(SQLiteStorage(first.database_path).list_runs()) == 1


def test_unique_run_identity_and_duplicate_rejection(storage):
    first = storage.create_run("AAPL", "2000-01-01")
    second = storage.create_run("AAPL", "2000-01-01")
    assert first.run_id != second.run_id
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_run("AAPL", "2000-01-01", run_id=first.run_id)
    assert len(storage.list_runs()) == 2


@pytest.mark.parametrize("value", ["", "hello", "日本語 🦊\n" * 20000, b"", b"\x00\xff" * 100000])
def test_full_fidelity_artifact_roundtrip(run, value):
    run.write_artifact("nested/result.json", value, "application/json")
    assert run.read_artifact("nested/result.json") == value
    (entry,) = run.list_artifacts()
    assert entry["raw_length"] == len(value.encode() if isinstance(value, str) else value)
    assert entry["codec"] == "zstd"
    assert entry["codec_version"] == CODEC_VERSION
    assert entry["media_type"] == "application/json"
    if entry["raw_length"] > 10000:
        assert entry["stored_bytes"] < entry["raw_length"] // 10


def test_long_json_state_preserved_without_clipping(run):
    state = {
        "history": "a report with unicode 東京\n" * 100000,
        "metadata": {"values": list(range(100))},
    }
    text = json.dumps(state, ensure_ascii=False, indent=2)
    run.write_artifact("full_states.json", text, "application/json")
    assert json.loads(run.read_artifact("full_states.json")) == state


def test_upsert_is_scoped_and_atomic(storage, run):
    other = storage.create_run("MSFT", "2000-01-02")
    run.write_artifact("same.txt", "one")
    other.write_artifact("same.txt", "other")
    created = run.list_artifacts()[0]["created_at"]
    run.write_artifact("same.txt", "replacement")
    assert run.read_artifact("same.txt") == "replacement"
    assert other.read_artifact("same.txt") == "other"
    assert run.list_artifacts()[0]["created_at"] == created
    assert len(run.list_artifacts()) == 1


def test_raw_short_logs_compressed_long_logs_are_ordered_and_lossless(run):
    values = ["", "a short event", "🙂" * MAX_LOG_BYTES, "output\n" * 100000, "last"]
    ids = [run.append_log(value) for value in values]
    assert [row["line"] for row in run.read_logs()] == values
    assert [row["event_id"] for row in run.read_logs(after=ids[1], limit=2)] == ids[2:4]
    with sqlite3.connect(run.database_path) as conn:
        rows = conn.execute(
            "SELECT codec,line,length(payload),raw_length FROM log_events ORDER BY event_id"
        ).fetchall()
    assert rows[0][:3] == ("raw", "", None)
    assert rows[2][0] == "zstd" and rows[2][1] is None
    assert rows[3][2] < rows[3][3] // 10


def test_metadata_explicit_allowlist_and_canonical_status(run):
    dangerous = {
        "llm_provider": "openai",
        "quick_think_llm": "model",
        "analysts": ["market", "news"],
        "api_key": "secret",
        "password": "secret",
        "llm_headers": {"Authorization": "secret"},
        "backend_url": "https://secret.example/?token=secret",
        "results_dir": "/secret/path",
        "data_vendors": {"news_data": "yfinance", "api_key": "secret"},
        "tool_vendors": {"get_news": "yfinance", "api_key": "secret"},
        "deep_think_llm": "https://user:password@example.test",
        "duration_seconds": float("nan"),
        "status": "completed",
        "needs_review": True,
    }
    run.update_metadata(dangerous)
    saved = run.get_metadata()
    assert saved == {
        "llm_provider": "openai",
        "quick_think_llm": "model",
        "analysts": ["market", "news"],
        "data_vendors": {"news_data": "yfinance"},
        "tool_vendors": {"get_news": "yfinance"},
        "needs_review": True,
    }
    assert "secret" not in json.dumps(saved)
    assert run.status == "running"
    run.finish("completed")
    run.update_metadata({"export_status": "failed"})
    assert run.status == "completed"
    assert run.metadata["export_status"] == "failed"


def test_metadata_does_not_mutate_caller_values():
    source = {"analysts": ["market"], "data_vendors": {"news_data": "yfinance"}}
    result = sanitize_metadata(source)
    result["analysts"].append("news")
    result["data_vendors"]["news_data"] = "other"
    assert source["analysts"] == ["market"]
    assert source["data_vendors"]["news_data"] == "yfinance"


@pytest.mark.parametrize(
    "name", ["", "../a", "/absolute", "a/../b", "a//b", "./a", "C:/a", "a\\b", "bad\x00name"]
)
def test_unsafe_artifact_names_rejected(run, name):
    with pytest.raises(ValueError):
        run.write_artifact(name, "data")


def test_configured_bounds_reject_write_and_read(tmp_path):
    storage = SQLiteStorage(tmp_path / "bounded.db", max_artifact_bytes=128)
    run = storage.create_run("AAPL", "2000-01-01")
    run.write_artifact("ok.txt", "a" * 128)
    with pytest.raises(ValueError, match="limit"):
        run.write_artifact("large.txt", "a" * 129)
    with pytest.raises(ValueError, match="limit"):
        run.append_log("b" * 129)
    reader = SQLiteRunStore(run.database_path, run.run_id, max_artifact_bytes=100)
    with pytest.raises(CorruptArtifactError):
        reader.read_artifact("ok.txt")
    with pytest.raises(TypeError):
        run.write_artifact("wrong.json", {"not": "serialized"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("codec", "pickle"),
        ("codec_version", 999),
        ("raw_length", 999999999),
        ("raw_length", 0),
        ("encoding", "utf-16"),
        ("payload", b"bad"),
    ],
)
def test_corrupt_artifact_metadata_rejected(run, field, value):
    run.write_artifact("a", "valid data")
    _alter(run, f"UPDATE artifacts SET {field}=?", (value,))
    with pytest.raises(CorruptArtifactError):
        run.read_artifact("a")


@pytest.mark.parametrize(
    "transform",
    [
        lambda b: b[:-1],
        lambda b: b + b"garbage",
        lambda b: b + b,
        lambda b: b[:-1] + bytes([b[-1] ^ 1]),
    ],
)
@pytest.mark.parametrize("text", ["", "hello" * 100])
def test_truncation_checksum_trailing_and_concatenated_frames_rejected(run, transform, text):
    payload, length, _encoding = encode(text, DEFAULT_MAX_ARTIFACT_BYTES)
    run.write_artifact("a", text)
    _alter(run, "UPDATE artifacts SET payload=?,raw_length=?", (transform(payload), length))
    with pytest.raises(CorruptArtifactError):
        run.read_artifact("a")


@pytest.mark.parametrize(
    "compressor",
    [
        zstandard.ZstdCompressor(write_content_size=False, write_checksum=True),
        zstandard.ZstdCompressor(write_checksum=False),
    ],
)
def test_unknown_size_and_unchecksummed_frames_rejected(run, compressor):
    run.write_artifact("a", "data")
    _alter(run, "UPDATE artifacts SET payload=?", (compressor.compress(b"TA\x01data"),))
    with pytest.raises(CorruptArtifactError):
        run.read_artifact("a")


def test_invalid_utf8_payload_rejected(run):
    payload, length, _encoding = encode(b"\xff", DEFAULT_MAX_ARTIFACT_BYTES)
    run.write_artifact("a", "x")
    _alter(run, "UPDATE artifacts SET payload=?,raw_length=?", (payload, length))
    with pytest.raises(CorruptArtifactError):
        run.read_artifact("a")


def test_compressed_log_corruption_rejected(run):
    run.append_log("long" * 10000)
    _alter(run, "UPDATE log_events SET payload=?", (b"invalid",))
    with pytest.raises(CorruptArtifactError):
        run.read_logs()


def test_schema_owned_version_and_wal(storage):
    with sqlite3.connect(storage.database_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    _alter(storage, "PRAGMA user_version=999")
    with pytest.raises(SchemaVersionError):
        SQLiteStorage(storage.database_path)
    with pytest.raises(SchemaVersionError):
        storage.list_runs()
    with pytest.raises(SchemaVersionError):
        storage.retention(max_bytes=0, dry_run=False)
    with pytest.raises(SchemaVersionError):
        storage.vacuum()


def test_foreign_database_rejected_without_modification(tmp_path):
    path = tmp_path / "checkpoints.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE checkpoints(id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO checkpoints(data) VALUES('user data')")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        SQLiteStorage(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT data FROM checkpoints").fetchone()[0] == "user data"


def test_retention_dry_run_age_uses_created_time_not_trade_date(storage, tmp_path):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    old = storage.create_run("OLD", "2099-01-01")
    fresh = storage.create_run("FRESH", "1900-01-01")
    running = storage.create_run("RUNNING", "1900-01-01")
    for run in (old, fresh, running):
        run.write_artifact("a", "data" * 1000)
    old.finish("failed")
    fresh.finish("completed")
    _alter(storage, "UPDATE runs SET created_at=?", ((now - timedelta(days=1)).isoformat(),))
    for run in (old, running):
        _alter(
            storage,
            "UPDATE runs SET created_at=? WHERE run_id=?",
            ((now - timedelta(days=90)).isoformat(), run.run_id),
        )
    keep = tmp_path / "user-report.txt"
    keep.write_text("never delete user files")
    preview = storage.retention(max_age_days=30, now=now)
    assert preview["candidate_run_ids"] == [old.run_id]
    assert preview["deleted_run_ids"] == []
    assert len(storage.list_runs()) == 3
    applied = storage.retention(max_age_days=30, now=now, dry_run=False)
    assert applied["deleted_run_ids"] == [old.run_id]
    assert {row["run_id"] for row in storage.list_runs()} == {fresh.run_id, running.run_id}
    assert keep.read_text() == "never delete user files"
    with sqlite3.connect(storage.database_path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM artifacts WHERE run_id=?", (old.run_id,)).fetchone()[
                0
            ]
            == 0
        )


def test_retention_quota_counts_compressed_payload_protects_running(storage):
    finished = []
    now = datetime.now(UTC)
    for index in range(3):
        run = storage.create_run("AAPL", "2000-01-01")
        run.write_artifact("a", "very compressible" * 100000)
        run.append_log("log" * 100000)
        run.finish()
        _alter(
            storage,
            "UPDATE runs SET created_at=? WHERE run_id=?",
            ((now + timedelta(seconds=index)).isoformat(), run.run_id),
        )
        finished.append(run)
    active = storage.create_run("RUNNING", "1900-01-01")
    active.append_log("active")
    stats = storage.statistics()
    assert stats["logical_bytes"] < 10000
    assert stats["database_bytes"] > stats["logical_bytes"]
    first_bytes = finished[0].get_run()["stored_bytes"]
    preview = storage.retention(max_bytes=stats["logical_bytes"] - first_bytes)
    assert preview["candidate_run_ids"] == [finished[0].run_id]
    result = storage.retention(max_bytes=0, dry_run=False)
    assert set(result["deleted_run_ids"]) == {run.run_id for run in finished}
    assert result["over_budget_bytes"] == active.get_run()["stored_bytes"]
    assert result["protected_running_runs"] == 1
    assert storage.statistics()["logical_bytes"] == result["logical_bytes_after"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"max_bytes": -1},
        {"max_age_days": -1},
        {"max_age_days": float("nan")},
        {"max_bytes": 0, "dry_run": "false"},
        {"max_age_days": 1, "now": datetime.now()},
    ],
)
def test_retention_invalid_input_cannot_delete(storage, run, kwargs):
    run.finish()
    with pytest.raises((ValueError, TypeError)):
        storage.retention(**kwargs)
    assert storage.get_run(run.run_id)


def test_vacuum_only_explicit_and_preserves_live_content(storage, run):
    run.write_artifact("large.bin", os.urandom(1000000))
    run.finish()
    storage.retention(max_bytes=0, dry_run=False)
    before = storage.database_path.stat().st_size
    assert before > 1000000
    active = storage.create_run("LIVE", "2026-01-01")
    active.write_artifact("a", "still here")
    result = storage.vacuum()
    assert result["after"]["database_bytes"] < before
    assert active.read_artifact("a") == "still here"
    assert result["checkpoint"][0] == 0


def test_export_reconstructs_reports_and_preserves_all_artifacts(run, tmp_path):
    state = {
        "market_report": "日本語 full report " * 10000,
        "trade_date": "2000-01-02",
        "final_trade_decision": "BUY",
        "investment_plan": "plan",
    }
    run.write_artifact(
        "report_state.json", json.dumps(state, ensure_ascii=False), "application/json"
    )
    run.write_artifact("settings.json", json.dumps({"llm_provider": "test"}), "application/json")
    run.write_artifact("nested/raw.bin", b"\x00\xff")
    run.append_log("short")
    run.append_log("long" * 10000)
    run.finish()
    destination = run.export_run(tmp_path / "export", html=False)
    assert (destination / "artifacts" / "nested" / "raw.bin").read_bytes() == b"\x00\xff"
    assert (destination / "reports" / "1_analysts" / "market.md").read_text() == state[
        "market_report"
    ]
    assert json.loads((destination / "run.json").read_text())["run_id"] == run.run_id
    logs = [json.loads(line) for line in (destination / "logs.jsonl").read_text().splitlines()]
    assert [row["line"] for row in logs] == ["short", "long" * 10000]
    assert "2000-01-02" in (destination / "reports" / "complete_report.md").read_text()
    with pytest.raises(FileExistsError):
        run.export_run(destination)


def test_corrupt_or_unsafe_export_does_not_create_destination(run, tmp_path):
    run.write_artifact("valid", "text")
    _alter(run, "UPDATE artifacts SET name='../outside'")
    with pytest.raises(ValueError):
        run.export_run(tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()
    assert not (tmp_path / "outside").exists()


def test_export_rejects_file_directory_conflicts(run, tmp_path):
    run.write_artifact("a", "file")
    run.write_artifact("a/b", "nested")
    with pytest.raises(RuntimeError, match="collide"):
        run.export_run(tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def _process_writer(path, number):
    db = SQLiteStorage(path)
    run = db.create_run("SAME", "2000-01-01")
    for index in range(5):
        run.write_artifact(f"part-{index}.txt", f"worker-{number}-{index}" * 10000)
        run.append_log(f"worker-{number}-{index}")
    run.finish()
    return run.run_id


def test_concurrent_process_writers_initialize_and_finish_same_database(tmp_path):
    path = str(tmp_path / "concurrent.db")
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        ids = list(pool.map(_process_writer, [path] * 8, range(8)))
    storage = SQLiteStorage(path)
    assert len(set(ids)) == len(ids) == 8
    assert len(storage.list_runs(status="completed")) == 8
    for run_id in ids:
        assert len(storage.list_artifacts(run_id)) == 5
        assert len(storage.read_logs(run_id)) == 5
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_threads_append_same_run_without_global_state(run):
    def writer(index):
        run.append_log(f"event-{index}")
        run.write_artifact(f"part-{index}", str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(40)))
    rows = run.read_logs()
    assert {row["line"] for row in rows} == {f"event-{index}" for index in range(40)}
    assert len({row["event_id"] for row in rows}) == 40
    assert len(run.list_artifacts()) == 40


def test_log_pages_have_aggregate_decoded_bound_and_always_progress(run):
    from tradingagents.storage.sqlite import MAX_LOG_PAGE_BYTES

    values = ["a" * (MAX_LOG_PAGE_BYTES // 2)] * 3
    values += ["b" * (MAX_LOG_PAGE_BYTES + 1), "tail"]
    ids = [run.append_log(value) for value in values]
    first = run.read_logs()
    second = run.read_logs(after=first[-1]["event_id"])
    third = run.read_logs(after=second[-1]["event_id"])
    fourth = run.read_logs(after=third[-1]["event_id"])
    assert [row["event_id"] for row in first] == ids[:2]
    assert [row["event_id"] for row in second] == ids[2:3]
    assert [row["event_id"] for row in third] == ids[3:4]
    assert [row["event_id"] for row in fourth] == ids[4:]
    assert [row["line"] for row in first + second + third + fourth] == values


def test_export_uses_stable_snapshot_during_concurrent_updates(storage, run, tmp_path, monkeypatch):
    run.write_artifact("a", "first")
    run.write_artifact("b", "original second")
    run.append_log("original log")
    original = storage._read_artifact

    def read_and_update(conn, run_id, name):
        value = original(conn, run_id, name)
        if name == "a":
            run.write_artifact("b", "modified second")
            run.append_log("new log outside snapshot")
        return value

    monkeypatch.setattr(storage, "_read_artifact", read_and_update)
    destination = run.export_run(tmp_path / "snapshot")
    assert (destination / "artifacts" / "b").read_text() == "original second"
    logs = [json.loads(line) for line in (destination / "logs.jsonl").read_text().splitlines()]
    assert [row["line"] for row in logs] == ["original log"]
    assert run.read_artifact("b") == "modified second"


def test_export_snapshot_survives_concurrent_retention(storage, run, tmp_path, monkeypatch):
    run.write_artifact("a", "first")
    run.write_artifact("b", "second")
    run.append_log("still in snapshot")
    run.finish()
    original = storage._read_artifact

    def read_and_delete(conn, run_id, name):
        value = original(conn, run_id, name)
        if name == "a":
            assert storage.retention(max_bytes=0, dry_run=False)["deleted_run_ids"] == [run.run_id]
        return value

    monkeypatch.setattr(storage, "_read_artifact", read_and_delete)
    destination = run.export_run(tmp_path / "snapshot")
    assert (destination / "artifacts" / "b").read_text() == "second"
    assert "still in snapshot" in (destination / "logs.jsonl").read_text()
    assert storage.list_runs() == []


def test_failed_export_removes_only_owned_staging(run, tmp_path):
    run.write_artifact("a", "good")
    run.write_artifact("b", "bad")
    _alter(run, "UPDATE artifacts SET payload=? WHERE name='b'", (b"malformed",))
    sentinel = tmp_path / ".some-other-export"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("user data")
    with pytest.raises(CorruptArtifactError):
        run.export_run(tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    assert not list(tmp_path.glob(".failed.export-*"))
    assert (sentinel / "keep.txt").read_text() == "user data"


def test_export_publish_race_never_replaces_user_destination(run, tmp_path, monkeypatch):
    from tradingagents.storage import export

    run.write_artifact("a", "good")
    original = export.publish_directory

    def race(source, destination):
        destination.mkdir()
        (destination / "user.txt").write_text("do not replace")
        original(source, destination)

    monkeypatch.setattr(export, "publish_directory", race)
    with pytest.raises(FileExistsError):
        run.export_run(tmp_path / "raced")
    assert (tmp_path / "raced" / "user.txt").read_text() == "do not replace"
    assert not (tmp_path / "raced" / "artifacts").exists()
    assert not list(tmp_path.glob(".raced.export-*"))


def test_interrupted_is_terminal_and_retention_eligible(storage, run):
    run.finish("interrupted")
    assert run.status == "interrupted"
    assert storage.list_runs(status="interrupted")[0]["run_id"] == run.run_id
    assert storage.retention(max_bytes=0)["candidate_run_ids"] == [run.run_id]
