"""Private, persistent DuckDuckGo circuit breaker (standard library only).

An unreadable or unsafe state is a closed circuit, never permission to retry.
The request guard serializes the *whole* request and block recording across
processes. Callers must check ``status()`` again after entering the guard.
"""
from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # No unlocked fallback on platforms without flock.
    fcntl = None

_MAX_BYTES = 4096
_MAX_SECONDS = 168 * 3600
_REASONS = frozenset({"provider_blocked", "captcha_or_challenge", "http_202_blocked",
                      "http_401_blocked", "http_403_blocked", "http_429_blocked"})
_FIELDS = {"version", "provider", "blocked_at", "blocked_until", "reason"}
_LOCK = threading.RLock()
_LOCAL = threading.local()
_PID = os.getpid()
monotonic = time.monotonic
_HELD_FDS = set()


def _after_fork():
    global _PID, _LOCK, _LOCAL
    for fd in _HELD_FDS:
        os.close(fd)
    _HELD_FDS.clear()
    _PID, _LOCK, _LOCAL = os.getpid(), threading.RLock(), threading.local()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def safe_reason(reason):
    """Map arbitrary response text to a small, non-sensitive vocabulary."""
    return reason if isinstance(reason, str) and reason in _REASONS else "provider_blocked"


class PauseError(RuntimeError):
    """Safe public failure code, with no filesystem paths or server content."""
    def __init__(self, reason="pause_state_unavailable"):
        self.reason = reason
        super().__init__(reason)


def _root():
    value = os.environ.get("XDG_CACHE_HOME")
    base = Path(value) if value else Path.home() / ".cache"
    if not base.is_absolute():
        raise PauseError()
    return base / "tradingagents"


def _directory(create=False):
    """Walk using directory FDs so symlinks cannot redirect the store."""
    if fcntl is None or not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "getuid", "pread", "pwrite", "ftruncate", "fsync")):
        raise PauseError("pause_state_unsupported_platform")
    path = _root()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise PauseError()
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            root_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (info.st_uid not in {0, os.getuid()}
                    or (info.st_mode & 0o022 and not root_sticky)):
                raise PauseError()
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PauseError()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _check_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise PauseError()
    return info


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _read(fd):
    try:
        state_fd = os.open("ddg-block.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        if _check_file(state_fd).st_size > _MAX_BYTES:
            raise PauseError()
        raw = os.read(state_fd, _MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise PauseError()
    finally:
        os.close(state_fd)
    data = json.loads(raw, object_pairs_hook=_no_duplicates)
    if (not isinstance(data, dict) or set(data) != _FIELDS
            or type(data["version"]) is not int or data["version"] != 1
            or data["provider"] != "duckduckgo"
            or not isinstance(data["reason"], str) or data["reason"] not in _REASONS):
        raise PauseError()
    for field in ("blocked_at", "blocked_until"):
        value = data[field]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
            raise PauseError()
    if not 0 < data["blocked_until"] - data["blocked_at"] <= _MAX_SECONDS:
        raise PauseError()
    return data


def _active(data, now):
    if data is None:
        return None
    if now < data["blocked_at"]:
        raise PauseError()
    if now >= data["blocked_until"]:
        return None
    return {"reason": data["reason"], "retry_after_seconds": math.ceil(data["blocked_until"] - now),
            "blocked_at": data["blocked_at"], "blocked_until": data["blocked_until"], "source": "persistent"}


def status():
    """Return active pause, None if clear, or an indefinite fail-closed pause."""
    fd = None
    try:
        fd = _directory()
        if fd is None:
            return None
        _check_marker(fd)
        return _active(_read(fd), time.time())
    except PauseError as exc:
        reason = "pause_state_unsupported_platform" if exc.reason == "pause_state_unsupported_platform" else "pause_state_unavailable"
        return {"reason": reason, "retry_after_seconds": None, "source": "persistent"}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {"reason": "pause_state_unavailable", "retry_after_seconds": None, "source": "persistent"}
    finally:
        if fd is not None:
            os.close(fd)


def _remaining(deadline):
    if deadline is None:
        return None
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise PauseError("budget_exhausted")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PauseError("budget_exhausted")
    return remaining


@contextmanager
def request_guard(deadline=None):
    """Hold a reentrant cross-process lock, respecting an absolute monotonic deadline."""
    global _PID, _LOCK, _LOCAL
    if os.getpid() != _PID:  # Forked children must not inherit logical lock ownership.
        _PID, _LOCK, _LOCAL = os.getpid(), threading.RLock(), threading.local()
    remaining = _remaining(deadline)
    acquired = _LOCK.acquire() if remaining is None else _LOCK.acquire(timeout=remaining)
    if not acquired:
        raise PauseError("budget_exhausted")
    fd = lock_fd = None
    nested = getattr(_LOCAL, "depth", 0) > 0
    try:
        if not nested:
            if fcntl is None:
                raise PauseError("pause_state_unsupported_platform")
            try:
                fd = _directory(create=True)
                lock_fd = os.open("ddg-block.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  0o600, dir_fd=fd)
                _check_file(lock_fd)
                _HELD_FDS.add(lock_fd)
                while True:
                    _remaining(deadline)
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = _remaining(deadline)
                        time.sleep(min(0.025, remaining) if remaining is not None else 0.025)
            except OSError as exc:
                raise PauseError() from exc
        if not nested:
            _LOCAL.lock_fd = lock_fd
            _LOCAL.directory_fd = fd
        _LOCAL.depth = getattr(_LOCAL, "depth", 0) + 1
        try:
            yield
        finally:
            _LOCAL.depth -= 1
    finally:
        if not nested:
            _LOCAL.lock_fd = None
            _LOCAL.directory_fd = None
        if lock_fd is not None:
            _HELD_FDS.discard(lock_fd)
            os.close(lock_fd)
        if fd is not None:
            os.close(fd)
        _LOCK.release()


def _record_impl(reason, hours=6):
    """Atomically save a pause; extending it never shortens an existing pause."""
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 1 <= hours <= 168:
        raise PauseError()
    sanitized_reason = safe_reason(reason)
    fd = None
    temporary = None
    try:
        with request_guard():
            fd = _directory(create=True)
            now = time.time()
            previous = _read(fd)
            active = _active(previous, now)
            until = now + hours * 3600
            if active is not None and active["blocked_until"] >= until:
                return active
            data = {"version": 1, "provider": "duckduckgo", "blocked_at": now,
                    "blocked_until": until, "reason": sanitized_reason}
            temporary = ".ddg-block-" + uuid.uuid4().hex
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(data, stream, separators=(",", ":"), allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, "ddg-block.json", src_dir_fd=fd, dst_dir_fd=fd)
                temporary = None
                os.fsync(fd)
            finally:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=fd)
                    temporary = None
            return _active(data, now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise PauseError() from exc
    finally:
        if fd is not None:
            os.close(fd)


def _marker_value(fd):
    _check_file(fd)
    value = os.pread(fd, 2, 0)
    if value not in {b"", b"0", b"1"}:
        raise PauseError()
    return value


def _check_marker(directory_fd):
    try:
        fd = os.open("ddg-block.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    try:
        value = _marker_value(fd)
        if value == b"1" and not getattr(_LOCAL, "attempt", False):
            if getattr(_LOCAL, "depth", 0):
                raise PauseError()
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                # An active request owns the exclusive gate. Let callers queue
                # within their budget and recheck after acquiring that gate.
                return
            if _marker_value(fd) == b"1":
                raise PauseError()
    finally:
        os.close(fd)


def _write_marker(value):
    fd = getattr(_LOCAL, "lock_fd", None)
    if fd is None:
        raise PauseError()
    if os.pwrite(fd, value, 0) != 1:
        raise PauseError()
    os.ftruncate(fd, 1)
    os.fsync(fd)
    # Persist a newly-created lock entry before allowing any external request.
    os.fsync(_LOCAL.directory_fd)


@contextmanager
def request_attempt():
    """Durably mark an in-flight request; crashes and failed records stay closed.

    Must be entered inside request_guard, after checking status. The marker is
    deliberately not cleared by another process, even if its owner has died.
    """
    if not getattr(_LOCAL, "depth", 0) or getattr(_LOCAL, "attempt", False):
        raise PauseError()
    try:
        if _marker_value(_LOCAL.lock_fd) == b"1":
            raise PauseError()
        _write_marker(b"1")
    except OSError as exc:
        raise PauseError() from exc
    _LOCAL.attempt = True
    _LOCAL.record_failed = False
    try:
        yield
    except BaseException as exc:
        if not isinstance(exc, Exception):
            _LOCAL.record_failed = True
        raise
    finally:
        try:
            if not _LOCAL.record_failed:
                _write_marker(b"0")
        except (OSError, PauseError) as exc:
            # Keep an in-memory/durable dirty indication after a failed clear.
            with suppress(OSError, PauseError):
                _write_marker(b"1")
            raise PauseError() from exc
        finally:
            _LOCAL.attempt = False


def record(reason, hours=6):
    """Save a pause without ever erasing evidence of a failed persistence step."""
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 1 <= hours <= 168:
        raise PauseError()
    with request_guard():
        if getattr(_LOCAL, "attempt", False):
            try:
                return _record_impl(reason, hours)
            except BaseException:
                _LOCAL.record_failed = True
                raise
        with request_attempt():
            try:
                return _record_impl(reason, hours)
            except BaseException:
                _LOCAL.record_failed = True
                raise
