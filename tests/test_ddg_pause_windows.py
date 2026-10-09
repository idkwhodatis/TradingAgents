"""Native Windows security and persistence checks; no ACL or locking mocks.

These tests require Windows and pywin32. Linux collection deliberately skips
this module rather than treating simulated Win32 results as compatibility proof.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.extensions import ddg_pause as pause

if os.name != "nt":
    pytest.skip("requires native Windows ACLs and file locks", allow_module_level=True)

# A missing declared Windows dependency must fail collection, not silently skip.
import ntsecuritycon  # noqa: E402
import win32api  # noqa: E402
import win32security  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "tradingagents" / "ddg-block.json"


def current_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def security(path):
    return win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )


def descriptor(path):
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        security(path), win32security.SDDL_REVISION_1,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )


def assert_private(path):
    sd = security(path)
    sid = current_sid()
    assert sd.GetSecurityDescriptorOwner() == sid
    assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    acl = sd.GetSecurityDescriptorDacl()
    assert acl is not None and acl.GetAceCount() > 0
    allowed = []
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        assert not ace[0][1] & win32security.INHERITED_ACE
        if ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE:
            assert ace[2] == sid
            allowed.append(ace[1])
        else:
            pytest.fail(f"Unexpected ACE type in private store: {ace[0][0]}")
    assert allowed


def allow_world(path, access=ntsecuritycon.FILE_GENERIC_READ):
    acl = security(path).GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION, access,
        win32security.CreateWellKnownSid(win32security.WinWorldSid, None),
    )
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )


def child(code):
    # Exercise the real backend in a fresh interpreter, with all network disabled.
    offline = """
import socket
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
socket.socket.connect_ex = socket.socket.connect
socket.getaddrinfo = socket.socket.connect
from tradingagents.extensions import ddg_pause as p
"""
    return subprocess.Popen(
        [sys.executable, "-c", offline + code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def finish(process):
    try:
        out, err = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        pytest.fail("Windows pause child timed out")
    assert process.returncode == 0, out + err
    return out


def test_private_owner_and_protected_dacl_survive_atomic_replacement(store):
    pause.record("http_429_blocked", hours=1)
    for path in (store.parent, store, store.parent / "ddg-block.lock"):
        assert_private(path)
    pause.record("captcha_or_challenge", hours=8)
    assert_private(store)
    assert json.loads(finish(child("import json; print(json.dumps(p.status()))")))["reason"] == "captcha_or_challenge"
    assert sorted(path.name for path in store.parent.iterdir()) == ["ddg-block.json", "ddg-block.lock"]


@pytest.mark.parametrize("target", ["directory", "state", "lock"])
def test_world_allow_acl_is_rejected_without_repair(store, target):
    pause.record("provider_blocked")
    path = {"directory": store.parent, "state": store, "lock": store.parent / "ddg-block.lock"}[target]
    allow_world(path)
    before_acl, before_bytes = descriptor(path), store.read_bytes()
    if target != "lock":
        assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("http_429_blocked")
    assert descriptor(path) == before_acl
    assert store.read_bytes() == before_bytes


@pytest.mark.parametrize("target", ["state", "lock"])
def test_hardlinked_files_are_rejected(store, tmp_path, target):
    pause.record("provider_blocked")
    path = store if target == "state" else store.parent / "ddg-block.lock"
    other = tmp_path / "alias"
    os.link(path, other)
    before = other.read_bytes()
    if target == "state":
        assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("http_429_blocked")
    assert other.read_bytes() == before


def test_junction_directory_cannot_redirect_store(store, tmp_path):
    pause.record("provider_blocked")
    actual = tmp_path / "actual-store"
    store.parent.rename(actual)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(store.parent), str(actual)],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    before = (actual / store.name).read_bytes()
    try:
        assert pause.status()["reason"] == "pause_state_unavailable"
        with pytest.raises(pause.PauseError):
            pause.record("http_429_blocked")
        assert (actual / store.name).read_bytes() == before
    finally:
        # Remove the junction itself, never recursively follow its target.
        os.rmdir(store.parent)


def test_status_does_not_create_or_mutate_store(store):
    assert pause.status() is None
    assert not store.parent.exists()
    pause.record("provider_blocked")
    paths = [store.parent, store, store.parent / "ddg-block.lock"]
    before = [(path.stat().st_mtime_ns, descriptor(path), path.read_bytes() if path.is_file() else None) for path in paths]
    assert pause.status()["reason"] == "provider_blocked"
    assert [(path.stat().st_mtime_ns, descriptor(path), path.read_bytes() if path.is_file() else None) for path in paths] == before


@pytest.mark.parametrize("payload", ["", "{broken", "[]", "x" * 4097, '{"version":1,"version":1}'])
def test_corrupt_state_is_preserved_and_fails_closed(store, payload):
    pause.record("provider_blocked")
    store.write_text(payload)
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("provider_blocked")
    assert store.read_text() == payload


def test_cross_process_lock_deadline_and_maximum_duration(store):
    processes = []
    try:
        with pause.request_guard():
            contender = child("import time\ntry:\n with p.request_guard(time.monotonic()+0.15): pass\nexcept p.PauseError as exc: print(exc.reason)")
            assert finish(contender).strip() == "budget_exhausted"
            processes = [child(f"p.record('provider_blocked', hours={hours})") for hours in (1, 8, 2, 3)]
            time.sleep(0.2)
            assert not store.exists()
            assert all(process.poll() is None for process in processes)
        for process in processes:
            finish(process)
        assert pause.status()["retry_after_seconds"] > 7.9 * 3600
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_failed_persistence_keeps_marker_closed_in_fresh_process(store, monkeypatch):
    from tradingagents.extensions import ddg_pause_windows

    def fail_write(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(ddg_pause_windows, "write_state", fail_write)
    with pause.request_guard(), pause.request_attempt(), pytest.raises(pause.PauseError):
        pause.record("captcha_or_challenge")
    assert not store.exists()
    assert pause.status()["reason"] == "pause_state_unavailable"
    assert finish(child("print(p.status()['reason'])")).strip() == "pause_state_unavailable"
    assert_private(store.parent / "ddg-block.lock")


def test_orphaned_marker_fails_closed(store):
    process = child("import os\nwith p.request_guard(), p.request_attempt():\n os._exit(0)")
    finish(process)
    assert pause.status()["reason"] == "pause_state_unavailable"
    assert finish(child("print(p.status()['reason'])")).strip() == "pause_state_unavailable"


@pytest.mark.parametrize("payload", [b"?", b"01", b"x" * 4097])
def test_corrupt_marker_is_preserved_and_fails_closed(store, payload):
    pause.record("provider_blocked")
    lock = store.parent / "ddg-block.lock"
    lock.write_bytes(payload)
    assert pause.status()["reason"] == "pause_state_unavailable"
    assert lock.read_bytes() == payload


@pytest.mark.parametrize("target", ["state", "lock"])
def test_file_symlink_is_rejected_when_supported(store, tmp_path, target):
    pause.record("provider_blocked")
    path = store if target == "state" else store.parent / "ddg-block.lock"
    actual = tmp_path / "symlink-target"
    path.rename(actual)
    try:
        os.symlink(actual, path)
    except OSError as exc:
        actual.rename(path)
        if exc.winerror == 1314:
            pytest.skip("Windows account lacks symlink privilege; junction coverage still runs")
        raise
    before = actual.read_bytes()
    if target == "state":
        assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("http_429_blocked")
    assert actual.read_bytes() == before


def test_managed_reader_holds_metadata_lock_until_state_handle_closes(store, monkeypatch):
    from tradingagents.extensions import ddg_pause_windows as windows

    pause.record("http_403_blocked", hours=1)
    old = store.read_bytes()
    data = json.loads(old)
    data["reason"] = "http_429_blocked"
    new = json.dumps(data).encode()
    entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
    original_read = windows.read_at

    def held_read(*args, **kwargs):
        value = original_read(*args, **kwargs)
        if threading.current_thread().name.startswith("state-reader"):
            entered.set()
            assert release.wait(5), "Reader was not released"
        return value

    def read():
        directory = windows.directory(store.parent)
        try:
            return windows.read_state(directory, 4096)
        finally:
            windows.close_directory(directory)

    def write():
        directory = windows.directory(store.parent, create=True)
        try:
            writer_started.set()
            windows.write_state(directory, new)
        finally:
            windows.close_directory(directory)

    monkeypatch.setattr(windows, "read_at", held_read)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-reader") as readers, ThreadPoolExecutor(max_workers=1) as writers:
        reader = readers.submit(read)
        try:
            assert entered.wait(5), "Reader did not enter metadata-protected read"
            writer = writers.submit(write)
            assert writer_started.wait(5)
            time.sleep(0.1)
            assert not writer.done(), "Writer bypassed the reader's metadata lock"
        finally:
            release.set()
        assert reader.result(timeout=5) == old
        writer.result(timeout=5)
    assert store.read_bytes() == new
    assert_private(store)


def test_reader_waits_until_renamed_writer_handle_is_closed(store, monkeypatch):
    import win32file

    from tradingagents.extensions import ddg_pause_windows as windows

    pause.record("http_403_blocked", hours=1)
    data = json.loads(store.read_bytes())
    data["reason"] = "http_429_blocked"
    new = json.dumps(data).encode()
    renamed, release, reader_started = threading.Event(), threading.Event(), threading.Event()
    original_set = win32file.SetFileInformationByHandle

    def held_rename(handle, information_class, information):
        if information_class == win32file.FileRenameInfo:
            assert information["RootDirectory"] is None
            assert os.path.normcase(information["FileName"]) == os.path.normcase(str(store))
        result = original_set(handle, information_class, information)
        if information_class == win32file.FileRenameInfo:
            renamed.set()
            assert release.wait(5), "Writer was not released after rename"
        return result

    def write():
        directory = windows.directory(store.parent, create=True)
        try:
            windows.write_state(directory, new)
        finally:
            windows.close_directory(directory)

    def read():
        directory = windows.directory(store.parent)
        try:
            reader_started.set()
            return windows.read_state(directory, 4096)
        finally:
            windows.close_directory(directory)

    monkeypatch.setattr(win32file, "SetFileInformationByHandle", held_rename)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(write)
        try:
            assert renamed.wait(5), "Writer did not reach native rename"
            reader = executor.submit(read)
            assert reader_started.wait(5)
            time.sleep(0.1)
            assert not reader.done(), "Reader bypassed the writer's metadata lock"
        finally:
            release.set()
        writer.result(timeout=5)
        assert reader.result(timeout=5) == new
    assert pause.status()["reason"] == "http_429_blocked"


def test_world_writable_ancestor_is_rejected_without_repair(store):
    pause.record("provider_blocked")
    ancestor = store.parent.parent
    allow_world(ancestor, ntsecuritycon.FILE_GENERIC_WRITE)
    before_acl, before_bytes = descriptor(ancestor), store.read_bytes()
    assert pause.status()["reason"] == "pause_state_unavailable"
    with pytest.raises(pause.PauseError):
        pause.record("http_429_blocked")
    assert descriptor(ancestor) == before_acl
    assert store.read_bytes() == before_bytes


def test_ancestor_junction_cannot_redirect_store(store, tmp_path):
    pause.record("provider_blocked")
    ancestor = store.parent.parent
    actual = tmp_path / "actual-cache"
    ancestor.rename(actual)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(ancestor), str(actual)],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual_state = actual / "tradingagents" / store.name
    before = actual_state.read_bytes()
    try:
        assert pause.status()["reason"] == "pause_state_unavailable"
        with pytest.raises(pause.PauseError):
            pause.record("http_429_blocked")
        assert actual_state.read_bytes() == before
    finally:
        os.rmdir(ancestor)


def test_live_directory_handle_pins_ancestor_against_rename(store, tmp_path):
    from tradingagents.extensions import ddg_pause_windows as windows

    pause.record("provider_blocked")
    ancestor = store.parent.parent
    destination = tmp_path / "renamed-cache"
    directory = windows.directory(store.parent)
    try:
        with pytest.raises(OSError) as error:
            ancestor.rename(destination)
        assert error.value.winerror in {5, 32}
        assert ancestor.is_dir()
        assert not destination.exists()
        assert json.loads(windows.read_state(directory, 4096))["reason"] == "provider_blocked"
    finally:
        windows.close_directory(directory)
    # Closing the pins restores ordinary rename behavior.
    ancestor.rename(destination)
    assert (destination / "tradingagents" / store.name).is_file()


def test_owner_rights_ace_means_verified_ancestor_owner(store):
    pause.record("provider_blocked", hours=1)
    ancestor = store.parent.parent
    acl = security(ancestor).GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
        ntsecuritycon.FILE_ALL_ACCESS,
        win32security.ConvertStringSidToSid("S-1-3-4"),
    )
    win32security.SetNamedSecurityInfo(
        str(ancestor), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )
    before = descriptor(ancestor)
    pause.record("http_429_blocked", hours=8)
    assert pause.status()["reason"] == "http_429_blocked"
    assert descriptor(ancestor) == before
    assert_private(store)
