"""Offline process-boundary checks for the provider-wide DuckDuckGo pause.

Every child refuses real network calls. Only the first challenge-producing
request is mocked; subsequent processes exercise the actual persisted gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import requests

from tradingagents.extensions import ddg_pause, duckduckgo_news as ddg

ROOT = Path(__file__).resolve().parents[1]
START, END = "2026-10-01", "2026-10-08"
IDENTITY = {"code": "601868", "canonical_symbol": "601868.SS", "chinese_short_name": "中国能建"}

# Socket guards are needed in children because pytest's autouse fixture is not
# inherited by a new interpreter. The requests guard detects attempted HTTP,
# even if production catches the transport exception.
OFFLINE = """
import json, socket, requests
http_calls = []
def no_http(self, *args, **kwargs):
    http_calls.append([str(arg) for arg in args])
    raise AssertionError('unexpected HTTP request')
def no_socket(*args, **kwargs):
    raise AssertionError('unexpected network access')
requests.sessions.Session.request = no_http
socket.socket.connect = no_socket
socket.socket.connect_ex = no_socket
socket.getaddrinfo = no_socket
"""


def child_env(cache, **overrides):
    env = dict(os.environ)
    # Explicitly mask .env overlays as conftest does for the parent process.
    from dotenv import dotenv_values
    for filename in (".env", ".env.enterprise"):
        for key in dotenv_values(ROOT / filename):
            if key.startswith("TRADINGAGENTS_"):
                env[key] = ""
    env.update(XDG_CACHE_HOME=str(cache), PYTHONPATH=str(ROOT))
    env.update(overrides)
    return env


def run_child(code, cache, **overrides):
    result = subprocess.run(
        [sys.executable, "-c", OFFLINE + textwrap.dedent(code)],
        cwd=ROOT, env=child_env(cache, **overrides), text=True,
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def clean_memory(monkeypatch):
    from tradingagents.extensions import ashare_announcements
    monkeypatch.setattr(ddg, "_BLOCK_UNTIL", 0.0)
    monkeypatch.setattr(ddg, "_BLOCK_REASON", "")
    monkeypatch.setattr(ddg, "_BLOCK_ERROR", None)
    monkeypatch.setattr(ddg, "_LAST_REQUEST_STARTED", None)
    ddg._CACHE.clear()
    ashare_announcements._CACHE.clear()
    yield
    ddg._CACHE.clear()
    ashare_announcements._CACHE.clear()


@pytest.mark.parametrize("consumer", ["news", "official", "diagnostic"])
def test_challenge_in_one_process_stops_next_process(tmp_path, consumer):
    cache = tmp_path / "isolated-cache"
    first = run_child('''
        from tradingagents.extensions.duckduckgo_news import fetch_news
        calls = []
        class Challenge:
            status_code = 200
            headers = {"content-type": "text/html"}
            def iter_content(self, chunk_size):
                yield b"<html>challenge-form</html>"
            def close(self):
                pass
        def challenge(self, url, **kwargs):
            calls.append(url)
            return Challenge()
        requests.Session.get = challenge
        result = fetch_news(["China Energy"], "2026-10-01", "2026-10-08", {
            "duckduckgo_news_cache_ttl": 0,
        })
        print(json.dumps({"result": result, "calls": calls}))
    ''', cache)
    assert len(first["calls"]) == 1
    assert first["result"]["diagnostics"]["stop_reason"] == "captcha_or_challenge"
    second = run_child(f'''
        import contextlib, io
        from tradingagents.extensions.duckduckgo_news import fetch_news
        from tradingagents.extensions.ashare_announcements import fetch_announcements
        from tradingagents.extensions.news_diagnostics import main
        consumer = {consumer!r}
        if consumer == "news":
            result = fetch_news(["different query"], "2026-10-01", "2026-10-08", {{}})
        elif consumer == "official":
            result = fetch_announcements({IDENTITY!r}, "2026-10-01", "2026-10-08", {{}})
        else:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--query", "different diagnostic query"])
            assert code == 1
            result = json.loads(output.getvalue())
        print(json.dumps({{"result": result, "http_calls": http_calls}}))
    ''', cache)
    assert second["http_calls"] == []
    assert second["result"]["status"] == "unavailable"
    assert "captcha_or_challenge" in json.dumps(second["result"])
    assert second["result"]["diagnostics"]["stop_search"] is True


def snapshot(folder):
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): (p.stat().st_mode, p.stat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None)
            for p in folder.rglob("*")}


@pytest.mark.parametrize("state", ["missing", "active", "expired", "corrupt"])
def test_status_only_has_no_query_network_or_writes(tmp_path, monkeypatch, state):
    cache = tmp_path / "status-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    if state != "missing":
        ddg_pause.record("http_429_blocked")
        path = cache / "tradingagents" / "ddg-block.json"
        if state == "expired":
            data = json.loads(path.read_text())
            data["blocked_at"] = time.time() - 7200
            data["blocked_until"] = time.time() - 3600
            path.write_text(json.dumps(data))
        elif state == "corrupt":
            path.write_text("{broken")
    before = snapshot(cache)
    output = run_child('''
        import io, contextlib
        from tradingagents.extensions.news_diagnostics import main
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = main(["--status"])
        print(json.dumps({"result": json.loads(captured.getvalue()),
                          "code": code, "http_calls": http_calls}))
    ''', cache)
    assert snapshot(cache) == before
    assert output["http_calls"] == []
    assert output["result"]["network_attempted"] is False
    assert output["code"] == (1 if state == "corrupt" else 0)
    pause = output["result"]["pause"]
    if state in {"missing", "expired"}:
        assert pause is None
    elif state == "corrupt":
        assert pause["reason"] == "pause_state_unavailable"
        assert pause["retry_after_seconds"] is None
    else:
        assert pause["reason"] == "http_429_blocked"


def test_diagnostic_uses_configured_pause_duration(tmp_path):
    output = run_child('''
        import contextlib, io
        from tradingagents.extensions.news_diagnostics import main
        class Blocked:
            status_code = 429
            headers = {}
            def close(self): pass
        requests.Session.get = lambda *a, **k: Blocked()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = main(["--query", "China Energy"])
        from tradingagents.extensions import ddg_pause
        print(json.dumps({"code": code, "pause": ddg_pause.status()}))
    ''', tmp_path / "duration", TRADINGAGENTS_DUCKDUCKGO_NEWS_PAUSE_HOURS="12")
    assert output["code"] == 1
    pause = output["pause"]
    assert pause["blocked_until"] - pause["blocked_at"] == pytest.approx(12 * 3600)


@pytest.mark.parametrize("hours", [0, 0.5, 169, float("nan"), float("inf"), True, "invalid"])
def test_invalid_pause_duration_rejected_before_http(monkeypatch, hours):
    calls = []
    def no_http(*args, **kwargs):
        calls.append(True)
        pytest.fail("Invalid pause duration must not reach HTTP")
    monkeypatch.setattr(requests.Session, "request", no_http)
    result = ddg.fetch_news(["China Energy"], START, END, {"duckduckgo_news_pause_hours": hours})
    assert result["status"] == "invalid_request"
    assert calls == []
    assert ddg_pause.status() is None


def test_fail_closed_pause_is_visible_in_retrieve_news(monkeypatch, tmp_path):
    from tradingagents.dataflows.config import set_config
    from tradingagents.extensions.news_evidence import retrieve_news
    cache = tmp_path / "unreadable-state"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    ddg_pause.record("http_403_blocked")
    (cache / "tradingagents" / "ddg-block.json").write_text("malformed")
    set_config({"duckduckgo_news_enabled": True, "ashare_announcements_enabled": False,
                "duckduckgo_news_cache_ttl": 0})
    def no_http(*args, **kwargs):
        pytest.fail("Fail-closed state must prevent HTTP")
    monkeypatch.setattr(requests.Session, "request", no_http)
    report = retrieve_news(lambda: "DATA_UNAVAILABLE: primary empty", None, START, END)
    assert "pause_state_unavailable" in report
    assert '"retry_after_seconds": null' in report
    assert '"stop_search": true' in report
    assert '"evidence_retrieved": false' in report
    assert "unavailable coverage, not absence of news" in report


def test_cross_process_gate_wait_respects_request_budget(tmp_path, monkeypatch):
    cache = tmp_path / "locked-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    locked = tmp_path / "budget-gate-locked"
    code = f'''
from pathlib import Path
from tradingagents.extensions.ddg_pause import request_guard
with request_guard():
    Path({str(locked)!r}).write_text("locked")
    input()
'''
    holder = subprocess.Popen([sys.executable, "-u", "-c", OFFLINE + code],
        cwd=ROOT, env=child_env(cache), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # File handshakes work on Windows, where selectors cannot watch pipes.
        wait_for_file(locked, holder)
        def no_http(*args, **kwargs):
            pytest.fail("A waiting gate must not start HTTP")
        monkeypatch.setattr(requests.Session, "request", no_http)
        started = time.monotonic()
        result = ddg.fetch_news(["China Energy"], START, END, {
            "duckduckgo_news_cache_ttl": 0,
            "_duckduckgo_news_deadline": started + 0.2,
        })
        elapsed = time.monotonic() - started
        assert 0.1 <= elapsed < 2
        assert result["status"] == "unavailable"
        assert result["diagnostics"]["stop_reason"] == "budget_exhausted"
        assert result["diagnostics"]["transport"] == []
        assert ddg_pause.status() is None
    finally:
        try:
            holder.communicate("\n", timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=5)


BLOCKED_RESPONSE = '''
class Blocked:
    status_code = 202
    headers = {}
    def close(self):
        pass
'''


def start_child(code, cache):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", OFFLINE + textwrap.dedent(code)],
        cwd=ROOT, env=child_env(cache), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def finish_child(process):
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stdout + stderr
    return json.loads(stdout)


def stop_children(*processes):
    for process in processes:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def wait_for_file(path, process):
    deadline = time.monotonic() + 20
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"Child exited before handshake: {stdout}\n{stderr}")
        assert time.monotonic() < deadline, "Timed out waiting for child handshake"
        time.sleep(0.01)


def assert_fresh_process_is_blocked(cache):
    output = run_child('''
        from tradingagents.extensions.duckduckgo_news import fetch_news, block_status
        result = fetch_news(["fresh process"], "2026-10-01", "2026-10-08", {
            "duckduckgo_news_cache_ttl": 0,
        })
        print(json.dumps({"result": result, "pause": block_status(), "http_calls": http_calls}))
    ''', cache)
    assert output["http_calls"] == []
    assert output["pause"]["reason"] == "pause_state_unavailable"
    assert output["pause"]["retry_after_seconds"] is None
    assert output["result"]["status"] == "unavailable"
    assert output["result"]["diagnostics"]["stop_search"] is True
    return output


def test_simultaneous_processes_do_not_slip_http_past_a_block(tmp_path):
    cache = tmp_path / "concurrent-cache"
    first_started = tmp_path / "first-http-started"
    second_entered = tmp_path / "second-check-entered"
    release = tmp_path / "release-first-http"
    first = second = None
    try:
        first = start_child(BLOCKED_RESPONSE + f'''
from pathlib import Path
import time
from tradingagents.extensions.duckduckgo_news import fetch_news
calls = []
def blocked_http(self, url, **kwargs):
    calls.append(url)
    Path({str(first_started)!r}).write_text("started")
    deadline = time.monotonic() + 20
    while not Path({str(release)!r}).exists():
        assert time.monotonic() < deadline, "HTTP mock release timed out"
        time.sleep(0.01)
    return Blocked()
requests.Session.get = blocked_http
result = fetch_news(["first process"], "2026-10-01", "2026-10-08", {{
    "duckduckgo_news_cache_ttl": 0,
}})
print(json.dumps({{"result": result, "http_calls": calls}}))
''', cache)
        wait_for_file(first_started, first)
        second = start_child(f'''
from pathlib import Path
from tradingagents.extensions import duckduckgo_news as ddg
original_status = ddg.block_status
def checked_status():
    Path({str(second_entered)!r}).write_text("entered")
    return original_status()
ddg.block_status = checked_status
result = ddg.fetch_news(["second process"], "2026-10-01", "2026-10-08", {{
    "duckduckgo_news_cache_ttl": 0,
}})
print(json.dumps({{"result": result, "http_calls": http_calls}}))
''', cache)
        wait_for_file(second_entered, second)
        # The second process has reached the shared pause check while the first
        # still owns the request lock and has not received or saved its 202.
        release.write_text("release")
        first_result = finish_child(first)
        second_result = finish_child(second)
        assert len(first_result["http_calls"]) == 1
        assert first_result["result"]["diagnostics"]["stop_reason"] == "http_202_blocked"
        assert second_result["http_calls"] == []
        assert second_result["result"]["status"] == "unavailable"
        assert second_result["result"]["diagnostics"]["stop_search"] is True
        assert second_result["result"]["diagnostics"]["stop_reason"] == "http_202_blocked"
    finally:
        stop_children(first, second)


def test_failed_atomic_pause_write_blocks_a_fresh_process(tmp_path):
    cache = tmp_path / "failed-write-cache"
    first = run_child(BLOCKED_RESPONSE + '''
from tradingagents.extensions import ddg_pause
from tradingagents.extensions.duckduckgo_news import fetch_news, block_status
calls = []
def blocked_http(self, url, **kwargs):
    calls.append(url)
    return Blocked()
def replace_failure(*args, **kwargs):
    raise OSError("simulated atomic replace failure")
requests.Session.get = blocked_http
if ddg_pause._WINDOWS:
    ddg_pause._windows.write_state = replace_failure
else:
    ddg_pause.os.replace = replace_failure
result = fetch_news(["failed write"], "2026-10-01", "2026-10-08", {
    "duckduckgo_news_cache_ttl": 0,
})
print(json.dumps({"result": result, "pause": block_status(), "http_calls": calls}))
''', cache)
    assert len(first["http_calls"]) == 1
    assert first["result"]["diagnostics"]["stop_reason"] == "http_202_blocked"
    assert first["pause"]["reason"] == "pause_state_unavailable"
    assert_fresh_process_is_blocked(cache)


def test_killed_http_process_leaves_fail_closed_state(tmp_path):
    cache = tmp_path / "killed-request-cache"
    started = tmp_path / "http-started-before-kill"
    process = start_child(f'''
from pathlib import Path
import time
from tradingagents.extensions.duckduckgo_news import fetch_news
def stalled_http(self, url, **kwargs):
    Path({str(started)!r}).write_text("started")
    time.sleep(60)
    raise AssertionError("Parent must kill the in-flight request")
requests.Session.get = stalled_http
fetch_news(["killed request"], "2026-10-01", "2026-10-08", {{
    "duckduckgo_news_cache_ttl": 0,
}})
''', cache)
    try:
        wait_for_file(started, process)
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0
        assert_fresh_process_is_blocked(cache)
    finally:
        stop_children(process)


def test_healthy_inflight_request_queues_next_process_without_false_pause(tmp_path):
    cache = tmp_path / "healthy-concurrent-cache"
    first_started = tmp_path / "healthy-http-started"
    second_waiting = tmp_path / "healthy-second-at-gate"
    second_http = tmp_path / "healthy-second-http"
    release = tmp_path / "release-healthy-http"
    response_code = '''
from pathlib import Path
import time
from tradingagents.extensions import duckduckgo_news as ddg
class Healthy:
    status_code = 200
    headers = {"content-type": "text/html"}
    def iter_content(self, chunk_size):
        yield b"healthy response"
    def close(self):
        pass
calls = []
'''
    execute = '''
with requests.Session() as session:
    session._duckduckgo_news_deadline = time.monotonic() + 15
    session._duckduckgo_news_min_interval = 0
    result = ddg._read_response(session, ddg.SEARCH_URL, {"q": "healthy"}, 5)
print(json.dumps({"result": result, "http_calls": calls, "pause": ddg.block_status()}))
'''
    first = second = None
    try:
        first = start_child(response_code + f'''
def healthy_http(self, url, **kwargs):
    calls.append(url)
    Path({str(first_started)!r}).write_text("started")
    deadline = time.monotonic() + 20
    while not Path({str(release)!r}).exists():
        assert time.monotonic() < deadline, "healthy mock release timed out"
        time.sleep(0.01)
    return Healthy()
requests.Session.get = healthy_http
''' + execute, cache)
        wait_for_file(first_started, first)
        second = start_child(response_code + f'''
from contextlib import contextmanager
from tradingagents.extensions import ddg_pause
original_guard = ddg_pause.request_guard
@contextmanager
def observed_guard(deadline=None):
    Path({str(second_waiting)!r}).write_text("waiting")
    with original_guard(deadline):
        yield
ddg_pause.request_guard = observed_guard
def healthy_http(self, url, **kwargs):
    assert Path({str(release)!r}).exists(), "HTTP escaped the first request's gate"
    Path({str(second_http)!r}).write_text("started")
    calls.append(url)
    return Healthy()
requests.Session.get = healthy_http
''' + execute, cache)
        wait_for_file(second_waiting, second)
        # It passed readiness while another process had a live dirty marker.
        # It must queue at that process's lock rather than fail closed or send.
        time.sleep(0.1)
        assert second.poll() is None
        assert not second_http.exists()
        release.write_text("release")
        for result in (finish_child(first), finish_child(second)):
            assert result["result"] == "healthy response"
            assert len(result["http_calls"]) == 1
            assert result["pause"] is None
        assert second_http.exists()
    finally:
        stop_children(first, second)
