import json
import os
import subprocess
import sys

import pytest

from seu_reserve.state import Task, open_nofollow


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
