import json
import os
import stat
import subprocess
import sys

import pytest

from seu_reserve.state import (FINGERPRINT_FILE, FINGERPRINT_PATTERN, Task,
                               load_or_create_device_fingerprint, open_nofollow)


def test_save_retries_a_transient_file_sharing_violation(tmp_path, monkeypatch):
    from seu_reserve import state
    task = Task(tmp_path, 'test-user')
    calls = []
    original_replace = state.os.replace
    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(13, 'sharing violation')
        original_replace(src, dst)
    try:
        monkeypatch.setattr(state.os, 'replace', flaky_replace)
        task.data['status'] = 'scanning'
        task.save()
    finally:
        task.close()
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning'
    assert len(calls) == 3
    assert not list(tmp_path.glob('.task-*'))


def test_save_gives_up_after_repeated_file_sharing_violations(tmp_path, monkeypatch):
    from seu_reserve import state
    task = Task(tmp_path, 'test-user')
    calls = []
    def always_blocked(src, dst):
        calls.append(dst)
        raise PermissionError(13, 'sharing violation')
    try:
        monkeypatch.setattr(state.os, 'replace', always_blocked)
        task.data['status'] = 'scanning'
        with pytest.raises(PermissionError):
            task.save()
    finally:
        task.close()
    assert len(calls) == state.REPLACE_ATTEMPTS
    assert not list(tmp_path.glob('.task-*'))


def test_task_lock_excludes_another_process_and_releases_on_close(tmp_path):
    script = """
import sys
from pathlib import Path
from seu_reserve.state import Task
try:
    task = Task(Path(sys.argv[1]), 'test-user')
except OSError:
    sys.exit(3)
task.close()
"""
    task = Task(tmp_path, 'test-user')
    try:
        blocked = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                                 capture_output=True, text=True, timeout=10)
        assert blocked.returncode == 3, blocked.stderr
    finally:
        task.close()
    resumed = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                             capture_output=True, text=True, timeout=10)
    assert resumed.returncode == 0, resumed.stderr


@pytest.mark.parametrize('native_nofollow', [True, False])
@pytest.mark.parametrize('existing_target', [True, False])
def test_state_file_symlinks_are_rejected_without_changing_target(
        tmp_path, monkeypatch, native_nofollow, existing_target):
    if not native_nofollow:
        monkeypatch.delattr(os, 'O_NOFOLLOW', raising=False)
    target = tmp_path / 'private'
    if existing_target:
        target.write_text('do not change')
    link = tmp_path / 'events.log'
    try:
        link.symlink_to(target)
    except OSError:
        if sys.platform == 'win32':
            pytest.skip('Creating symlinks requires Windows Developer Mode or administrator rights')
        raise
    with pytest.raises(OSError):
        open_nofollow(link, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    assert target.exists() == existing_target
    if existing_target:
        assert target.read_text() == 'do not change'


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows open handles prevent this replacement')
def test_state_file_replaced_during_open_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delattr(os, 'O_NOFOLLOW', raising=False)
    path = tmp_path / 'events.log'
    original_open = os.open
    opened = []
    def replace_after_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        replacement = tmp_path / 'replacement'
        replacement.write_text('do not change')
        os.replace(replacement, path)
        return fd
    monkeypatch.setattr(os, 'open', replace_after_open)
    with pytest.raises(OSError, match='unsafe_state_file'):
        open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    assert path.read_text() == 'do not change'
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_device_fingerprint_is_generated_once_and_reused(tmp_path):
    value = load_or_create_device_fingerprint(tmp_path)
    assert value.startswith('stable_')
    assert FINGERPRINT_PATTERN.fullmatch(value)
    path = tmp_path / FINGERPRINT_FILE
    assert path.read_text() == value
    # A new login (new process, same state dir) must send the same device identity.
    assert load_or_create_device_fingerprint(tmp_path) == value


def test_device_fingerprint_creates_a_missing_state_directory_privately(tmp_path):
    nested = tmp_path / 'a' / 'b'
    value = load_or_create_device_fingerprint(nested)
    assert (nested / FINGERPRINT_FILE).read_text() == value
    if sys.platform != 'win32':
        assert stat.S_IMODE(nested.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX file mode only')
def test_device_fingerprint_file_is_private(tmp_path):
    load_or_create_device_fingerprint(tmp_path)
    mode = stat.S_IMODE((tmp_path / FINGERPRINT_FILE).stat().st_mode)
    assert mode == 0o600


def test_device_fingerprint_regenerates_a_malformed_file(tmp_path):
    path = tmp_path / FINGERPRINT_FILE
    path.write_text('PRIVATE not a valid fingerprint\n')
    value = load_or_create_device_fingerprint(tmp_path)
    assert value != 'PRIVATE not a valid fingerprint'
    assert FINGERPRINT_PATTERN.fullmatch(value)
    assert path.read_text() == value


def test_device_fingerprint_returns_a_generated_value_when_it_cannot_persist(tmp_path, monkeypatch):
    from seu_reserve import state

    def unsafe(path, flags):
        raise OSError('unsafe_state_file')

    monkeypatch.setattr(state, 'open_nofollow', unsafe)
    value = load_or_create_device_fingerprint(tmp_path)
    assert value.startswith('stable_') and FINGERPRINT_PATTERN.fullmatch(value)
    assert not (tmp_path / FINGERPRINT_FILE).exists()


def test_device_fingerprint_survives_an_unusable_state_directory(tmp_path):
    # A path that exists but is not a directory must not fail the login attempt.
    directory = tmp_path / 'not-a-directory'
    directory.write_text('file')
    value = load_or_create_device_fingerprint(directory)
    assert value.startswith('stable_') and FINGERPRINT_PATTERN.fullmatch(value)


def test_device_fingerprint_rejects_a_symlink_without_touching_its_target(tmp_path):
    target = tmp_path / 'private'
    target.write_text('do not change')
    link = tmp_path / FINGERPRINT_FILE
    try:
        link.symlink_to(target)
    except OSError:
        if sys.platform == 'win32':
            pytest.skip('Creating symlinks requires Windows Developer Mode or administrator rights')
        raise
    value = load_or_create_device_fingerprint(tmp_path)
    assert value.startswith('stable_') and FINGERPRINT_PATTERN.fullmatch(value)
    assert target.read_text() == 'do not change'
    assert link.is_symlink()
