"""The archive maintenance CLI never silently applies deletion or reclamation."""

import json

import pytest

from tradingagents.storage import SQLiteStorage
from tradingagents.storage.__main__ import main


def test_cli_retention_is_dry_run_until_apply(tmp_path, capsys):
    db = tmp_path / "archive.sqlite3"
    archive = SQLiteStorage(db)
    run = archive.create_run("NVDA", "2000-01-01")
    run.write_artifact("report.txt", "saved")
    run.finish()
    argv = ["--db", str(db), "retention", "--max-bytes", "0"]
    main(argv)
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["candidate_run_ids"] == [run.run_id]
    assert archive.get_run(run.run_id)["status"] == "completed"
    main([*argv, "--apply"])
    applied = json.loads(capsys.readouterr().out)
    assert applied["deleted_run_ids"] == [run.run_id]
    assert archive.list_runs() == []


def test_cli_missing_path_does_not_create_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(SystemExit):
        main(["--db", str(path), "list"])
    assert not path.exists()


def test_vacuum_requires_explicit_apply(tmp_path, monkeypatch):
    path = tmp_path / "archive.sqlite3"
    SQLiteStorage(path)
    monkeypatch.setattr(SQLiteStorage, "vacuum", lambda self: pytest.fail("unexpected vacuum"))
    with pytest.raises(SystemExit):
        main(["--db", str(path), "vacuum"])


def test_cli_export_reports_truthful_destination(tmp_path, capsys):
    path = tmp_path / "archive.sqlite3"
    store = SQLiteStorage(path).create_run("NVDA", "2026-01-01")
    store.write_artifact("report_state.json", json.dumps({"trade_date": "2026-01-01", "market_report": "中文"}), "application/json")
    store.write_artifact("settings.json", "{}", "application/json")
    store.finish()
    destination = tmp_path / "export"
    main(["--db", str(path), "export", store.run_id, str(destination)])
    assert json.loads(capsys.readouterr().out)["output_dir"] == str(destination)
    assert (destination / "reports" / "complete_report.html").is_file()
    assert "中文" in (destination / "reports" / "complete_report.md").read_text()
