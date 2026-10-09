"""The DDG circuit breaker is durable, private and conservative on failures."""
import json
import os
import subprocess
import sys
import time

import pytest

from tradingagents.extensions import ddg_pause as pause


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "tradingagents" / "ddg-block.json"


def child(code):
    return subprocess.Popen([sys.executable, "-c", "from tradingagents.extensions import ddg_pause as p; " + code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_empty_status_has_no_side_effects(store):
    assert pause.status() is None
    assert not store.parent.exists()


def test_record_private_and_persistent(store):
    result = pause.record("captcha_or_challenge")
    assert result["reason"] == "captcha_or_challenge"
    assert result["source"] == "persistent"
    assert 21598 <= result["retry_after_seconds"] <= 21600
    assert store.stat().st_mode & 0o777 == 0o600
    assert store.parent.stat().st_mode & 0o777 == 0o700
    before = store.stat().st_mtime_ns
    proc = child("import json; print(json.dumps(p.status()))")
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert json.loads(out)["blocked_until"] == result["blocked_until"]
    assert store.stat().st_mtime_ns == before


def test_sanitization_and_never_shorten(store):
    long = pause.record("SECRET https://example.test/?q=user", hours=8)
    assert long["reason"] == "provider_blocked"
    assert "SECRET" not in store.read_text()
    assert pause.record("http_429_blocked", hours=1) == long


@pytest.mark.parametrize("hours", [0, -1, 169, True, "6", float("nan"), float("inf")])
def test_invalid_hours_fail_closed(store, hours):
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked", hours)


def test_expiry_and_rollback(store, monkeypatch):
    item = pause.record("http_403_blocked", hours=1)
    monkeypatch.setattr(pause.time, "time", lambda: item["blocked_until"])
    assert pause.status() is None
    monkeypatch.setattr(pause.time, "time", lambda: item["blocked_at"] - 1)
    assert pause.status()["reason"] == "pause_state_unavailable"
    original = store.read_bytes()
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert store.read_bytes() == original


@pytest.mark.parametrize("payload", ["", "not json", "[]", "{}", "x" * 4097,
    '{"version":1,"version":1}',
    '{"version":1,"provider":"duckduckgo","blocked_at":NaN,"blocked_until":5,"reason":"provider_blocked"}'])
def test_corrupt_state_never_erased(store, payload):
    pause.record("provider_blocked")
    store.write_text(payload)
    assert pause.status()["retry_after_seconds"] is None
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert store.read_text() == payload


@pytest.mark.parametrize("target", ["state", "directory", "lock"])
def test_unsafe_permissions(store, target):
    pause.record("provider_blocked")
    path = {"state": store, "directory": store.parent, "lock": store.parent / "ddg-block.lock"}[target]
    path.chmod(0o755 if target == "directory" else 0o644)
    if target != "lock":
        assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")


def test_symlink_and_hardlink_state(store, tmp_path):
    pause.record("provider_blocked")
    other = tmp_path / "other"
    store.rename(other)
    store.symlink_to(other)
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    store.unlink()
    os.link(other, store)
    assert pause.status()["reason"] == "pause_state_unavailable"


def test_guard_reentrant_record(store):
    with pause.request_guard(deadline=time.monotonic() + 2), pause.request_guard():
        pause.record("http_429_blocked")
    assert pause.status()["reason"] == "http_429_blocked"


def test_lock_deadline(store):
    with pause.request_guard():
        proc = child("import time;\ntry:\n with p.request_guard(time.monotonic()+0.1): pass\nexcept p.PauseError as e: print(e.reason)")
        out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert out.strip() == "budget_exhausted"


def test_process_serialization_and_maximum_expiry(store):
    with pause.request_guard():
        processes = [child(f"p.record('provider_blocked', hours={hours})") for hours in [1, 8, 2, 3]]
        time.sleep(0.15)
        assert not store.exists()
        assert all(proc.poll() is None for proc in processes)
    for proc in processes:
        _, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, err
    assert pause.status()["retry_after_seconds"] > 7.9 * 3600


def test_symlink_directory(store, tmp_path):
    store.parent.parent.mkdir()
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    store.parent.symlink_to(actual, target_is_directory=True)
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert not list(actual.iterdir())


def test_io_failure_is_safe(store, monkeypatch):
    pause.record("provider_blocked")
    def denied(*args, **kwargs):
        raise PermissionError("private path")
    monkeypatch.setattr(pause.os, "open", denied)
    assert pause.status() == {"reason": "pause_state_unavailable", "retry_after_seconds": None, "source": "persistent"}
    with pytest.raises(pause.PauseError, match="^pause_state_unavailable$"):
        pause.record("provider_blocked")


def test_unsafe_ancestor(store):
    pause.record("provider_blocked")
    store.parent.parent.chmod(0o777)
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")


def test_deep_json_is_fail_closed(store):
    pause.record("provider_blocked")
    payload = "[" * 1500 + "0" + "]" * 1500
    store.write_text(payload)
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert store.read_text() == payload


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("provider", "google"),
    ("reason", "https://private.example"), ("blocked_at", True),
    ("blocked_until", float("inf")), ("blocked_until", -1),
    ("blocked_until", 1e20), ("unexpected", "field"),
])
def test_schema_is_strict(store, field, value):
    pause.record("provider_blocked")
    data = json.loads(store.read_text())
    data[field] = value
    store.write_text(json.dumps(data))
    assert pause.status()["reason"] == "pause_state_unavailable"


def test_symlink_lock_fails_closed(store, tmp_path):
    pause.record("provider_blocked")
    lock = store.parent / "ddg-block.lock"
    lock.unlink()
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    lock.symlink_to(victim)
    with pytest.raises(pause.PauseError), pause.request_guard():
        pytest.fail("must not enter guard")
    assert victim.read_text() == "unchanged"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_fifo_state_does_not_hang(store):
    pause.record("provider_blocked")
    store.unlink()
    os.mkfifo(store, 0o600)
    assert pause.status()["reason"] == "pause_state_unavailable"


def test_unsupported_lock_backend_fails_closed(store, monkeypatch):
    monkeypatch.setattr(pause, "fcntl", None)
    assert pause.status()["reason"] == "pause_state_unsupported_platform"
    with pytest.raises(pause.PauseError, match="pause_state_unsupported_platform"), pause.request_guard():
        pytest.fail("must not enter guard")


def test_successful_attempt_clears_marker(store):
    with pause.request_guard(), pause.request_attempt():
        assert pause.status() is None  # The owning request may inspect its state.
        proc = child("print(p.status())")
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, err
        assert out.strip() == "None"  # Live owner, not an orphaned marker.
    assert pause.status() is None
    assert (store.parent / "ddg-block.lock").read_bytes() == b"0"


def test_network_error_clears_marker(store):
    with pytest.raises(ConnectionError), pause.request_guard(), pause.request_attempt():
        raise ConnectionError("no response")
    assert pause.status() is None


def test_atomic_write_failure_stays_closed_across_processes(store, monkeypatch):
    def fail_replace(*args, **kwargs):
        raise OSError("disk failure")
    monkeypatch.setattr(pause.os, "replace", fail_replace)
    with pause.request_guard(), pause.request_attempt(), pytest.raises(pause.PauseError):
        pause.record("captcha_or_challenge")
        # Swallowing the persistence error cannot let the outer attempt clear it.
    assert not store.exists()
    assert pause.status()["reason"] == "pause_state_unavailable"
    proc = child("print(p.status()['reason'])")
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert out.strip() == "pause_state_unavailable"
    with pytest.raises(pause.PauseError), pause.request_guard(), pause.request_attempt():
        pytest.fail("must not allow a subsequent HTTP request")


def test_marker_fsync_failure_prevents_request(store, monkeypatch):
    def fail_fsync(*args):
        raise OSError("cannot persist")
    with pause.request_guard():
        monkeypatch.setattr(pause.os, "fsync", fail_fsync)
        with pytest.raises(pause.PauseError), pause.request_attempt():
            pytest.fail("must not reach HTTP before marker is durable")
    assert pause.status()["reason"] == "pause_state_unavailable"


def test_interrupted_attempt_stays_closed(store):
    proc = child("import os;\nwith p.request_guard(), p.request_attempt(): os._exit(0)")
    _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError), pause.request_guard(), pause.request_attempt():
        pytest.fail("an interrupted request must not be retried")


def test_record_without_attempt_write_failure_stays_closed(store, monkeypatch):
    def fail_replace(*args, **kwargs):
        raise OSError("disk failure")
    monkeypatch.setattr(pause.os, "replace", fail_replace)
    with pytest.raises(pause.PauseError):
        pause.record("http_429_blocked")
    assert pause.status()["reason"] == "pause_state_unavailable"


def test_default_home_cache_path(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pause.status() is None
    pause.record("provider_blocked")
    assert (tmp_path / ".cache" / "tradingagents" / "ddg-block.json").is_file()


def test_relative_xdg_path_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "relative-cache")
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert not (tmp_path / "relative-cache").exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_retains_marker(store, interruption):
    with pytest.raises(interruption), pause.request_guard(), pause.request_attempt():
        raise interruption()
    assert pause.status()["reason"] == "pause_state_unavailable"
    proc = child("print(p.status()['reason'])")
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert out.strip() == "pause_state_unavailable"


def test_missing_positional_io_is_unsupported(store, monkeypatch):
    monkeypatch.delattr(pause.os, "pwrite")
    assert pause.status()["reason"] == "pause_state_unsupported_platform"
    with pytest.raises(pause.PauseError, match="pause_state_unsupported_platform"), pause.request_guard():
        pytest.fail("must not enter an unsupported guard")


def test_orphan_marker_cannot_be_ignored_by_own_guard(store):
    with pause.request_guard(), pause.request_attempt():
        pass
    (store.parent / "ddg-block.lock").write_bytes(b"1")
    with pause.request_guard():
        assert pause.status()["reason"] == "pause_state_unavailable"
        with pytest.raises(pause.PauseError), pause.request_attempt():
            pytest.fail("An orphaned marker is never permission to send")
