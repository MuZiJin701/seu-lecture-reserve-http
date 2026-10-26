import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from seu_reserve.app import Settings, run
from seu_reserve.cli import show_status
from seu_reserve.state import Task, task_running
from test_app import School, execute, interrupted, lecture


def snapshot(directory):
    return json.loads((directory / 'task.json').read_text())['health']


def test_successful_scan_is_observable_while_running_and_after_stop(tmp_path, capsys):
    def wait(seconds):
        current = snapshot(tmp_path)
        assert current['phase'] == 'waiting'
        assert current['last_query_at'] == 1000
        assert current['next_check_at'] == 1010
        assert current['activity_count'] == current['candidate_count'] == 0
        assert task_running(tmp_path)
        capsys.readouterr()
        show_status(tmp_path)
        output = capsys.readouterr().out
        assert 'process_running=true' in output and 'phase=waiting' in output
        raise KeyboardInterrupt
    assert execute(School(), tmp_path, clock=lambda: 1000, sleep=wait) == 130
    assert snapshot(tmp_path)['phase'] == 'stopped'
    assert not task_running(tmp_path)
    show_status(tmp_path)
    assert 'process_running=false' in capsys.readouterr().out


def test_status_displays_health_and_selection_snapshot_fields(tmp_path, capsys):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    assert execute(school, tmp_path, clock=lambda: 1000, notify=None) == 130
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert all(key in output for key in (
        'status=scanning', 'process_running=false', 'phase=stopped', 'last_phase=stopped',
        'updated_at=', 'last_query_at=', 'last_error_at=', 'next_check_at=', 'last_mail_at=',
        'last_error=none', 'failure_count=0', 'mail_status=disabled',
        'activity_count=1', 'candidate_count=0', 'last_mail_status=never',
        'last_mail_error=none', 'last_mail_event=none', 'last_mail_duration_seconds=unknown',
        'selection_updated_at=1970-01-01T08:16:40+08:00'))


def test_query_failure_mail_failure_and_recovery_are_visible_without_repeats(tmp_path):
    school = School()
    school.query_statuses = [500] * 10
    states, messages = [], []
    now = 1000.0
    def notify(event):
        messages.append(event)
        raise OSError('SECRET SMTP failure')
    def wait(seconds):
        nonlocal now
        states.append(snapshot(tmp_path))
        now += seconds
        if len(states) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=notify, sleep=wait, clock=lambda: now) == 130
    assert messages == ['query_failure']
    assert [entry['failure_count'] for entry in states] == [1, 2, 3, 4, 5]
    assert [entry['phase'] for entry in states] == ['backoff'] * 5
    assert states[0]['last_query_at'] == 0
    assert states[4]['mail_status'] == 'failed'
    assert 'SECRET' not in (tmp_path / 'task.json').read_text()
    saved = json.loads((tmp_path / 'task.json').read_text())
    saved['retry_at'] = 0
    (tmp_path / 'task.json').write_text(json.dumps(saved))
    school.query_statuses = []
    def stop(seconds):
        raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=notify, sleep=stop, clock=lambda: now) == 130
    assert messages == ['query_failure']
    assert 'event=query_recovered' in (tmp_path / 'events.log').read_text()
    recovered = snapshot(tmp_path)
    assert recovered['last_query_at'] == now and recovered['phase'] == 'stopped'


@pytest.mark.parametrize('error, event', [(OSError, 'task_failed'), (RuntimeError, 'unexpected_failure')])
@pytest.mark.parametrize('mail_fails', [False, True])
def test_fatal_errors_notify_once_and_preserve_safe_failure_state(tmp_path, capsys, error, event, mail_fails):
    class FailedSchool(School):
        def request(self, *args, **kwargs):
            raise error('SECRET credential or response')
    messages = []
    def notify(message):
        messages.append(message)
        if mail_fails:
            raise OSError('SECRET SMTP')
    assert execute(FailedSchool(), tmp_path, notify=notify) == 1
    assert messages == [event]
    current = snapshot(tmp_path)
    assert current['phase'] == 'failed'
    assert current['mail_status'] == ('failed' if mail_fails else 'accepted')
    assert not task_running(tmp_path)
    assert 'SECRET' not in capsys.readouterr().out + (tmp_path / 'task.json').read_text()


def test_invalid_task_notifies_without_overwriting_the_original(tmp_path):
    path = tmp_path / 'task.json'
    path.write_text('invalid SECRET')
    messages = []
    school = School()
    assert execute(school, tmp_path, notify=messages.append) == 1
    assert messages == ['task_failed']
    assert path.read_text() == 'invalid SECRET'
    assert not school.requests


def test_default_mailer_is_available_for_task_initialization_failure(tmp_path, monkeypatch):
    (tmp_path / 'task.json').write_text('invalid')
    messages = []
    class SMTP:
        def __init__(self, host, port, **kwargs):
            assert kwargs['context'].check_hostname
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def login(self, username, password):
            pass
        def send_message(self, message):
            messages.append(message)
    monkeypatch.setattr('smtplib.SMTP_SSL', SMTP)
    assert run(Settings(state_dir=tmp_path), session=School(), environ={
        'SEU_USERNAME': 'test-user', 'SMTP_USERNAME': 'sender@gmail.com', 'SMTP_PASSWORD': 'SECRET'}) == 1
    assert len(messages) == 1
    assert '任务异常退出' in messages[0]['Subject']
    assert 'SECRET' not in messages[0].get_content()


def test_unconfirmed_submissions_are_recorded_but_not_mailed(tmp_path):
    school = School()
    school.activities = [lecture('one'), lecture('two',
        KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    school.submit_replies = [{'code': -1}, {'code': -1}]
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == []
    assert school.submitted == ['one']
    school.mine = [{**lecture('one'), 'HD_WID': 'one', 'YYZT': '已预约'}]
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == []
    assert school.submitted == ['one', 'two']
    log = (tmp_path / 'events.log').read_text()
    assert log.count('event=reservation_unconfirmed') == 2
    assert 'event=reservation_succeeded' in log
    assert snapshot(tmp_path)['phase'] == 'stopped'


def test_successful_pending_query_breaks_consecutive_failure_count(tmp_path):
    import requests
    class PendingSchool(School):
        pending_reads = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do') and self.submitted:
                self.pending_reads += 1
                if self.pending_reads in (1, 2, 4):
                    raise requests.Timeout('SECRET timeout')
            return super().request(method, url, **kwargs)
    school = PendingSchool()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1}]
    events, waits = [], []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=wait) == 130
    assert waits == [20, 40, 2]
    assert events == []
    assert 'event=reservation_unconfirmed' in (tmp_path / 'events.log').read_text()
    assert snapshot(tmp_path)['failure_count'] == 0


@pytest.mark.parametrize('changes', [
    {'phase': ['SECRET']}, {'last_error': 'SECRET raw error'}, {'updated_at': float('inf')},
    {'last_query_at': -1}, {'failure_count': True}, {'mail_status': 'SECRET'},
])
def test_invalid_health_is_unknown_without_leaking_or_overwriting_task(tmp_path, capsys, changes):
    assert execute(School(), tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['health'].update(changes)
    path.write_text(json.dumps(saved))
    original = path.read_text()
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'phase=unknown' in output and 'SECRET' not in output
    assert path.read_text() == original


def test_lock_probe_does_not_create_files_or_hide_permission_errors(tmp_path, monkeypatch):
    missing = tmp_path / 'unused'
    assert not task_running(missing)
    assert not missing.exists()
    def denied(*args):
        raise PermissionError('SECRET denied')
    monkeypatch.setattr('seu_reserve.state.open_nofollow', denied)
    with pytest.raises(PermissionError):
        task_running(tmp_path)


def test_failed_atomic_health_write_keeps_the_previous_task(tmp_path, monkeypatch):
    task = Task(tmp_path, 'test-user')
    original = task.path.read_text()
    def failed_replace(*args):
        raise OSError('simulated disk error')
    monkeypatch.setattr(os, 'replace', failed_replace)
    try:
        task.data['health'] = {'phase': 'waiting'}
        with pytest.raises(OSError):
            task.save()
        assert task.path.read_text() == original
        assert not list(tmp_path.glob('.task-*'))
    finally:
        task.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='Unix signal delivery test')
@pytest.mark.parametrize('graceful', [True, False])
def test_cli_exit_releases_lock_and_cannot_report_old_snapshot_as_running(tmp_path, graceful):
    (tmp_path / '.env').write_text('SEU_CARD_NUMBER=test-user\nSEU_PASSWORD=test-password\n')
    tests_directory = Path(__file__).resolve().parent
    code = (f'import sys; sys.path.insert(0, {str(tests_directory)!r}); '
            'from test_app import School; import requests; School.close = lambda self: None; requests.Session = School; '
            'from seu_reserve.cli import main; raise SystemExit(main([]))')
    process = subprocess.Popen([sys.executable, '-c', code], cwd=tmp_path, env={},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (tmp_path / 'state' / 'task.json').exists():
                saved = json.loads((tmp_path / 'state' / 'task.json').read_text())
                if saved.get('health', {}).get('phase') == 'waiting':
                    break
            if process.poll() is not None:
                pytest.fail('offline CLI exited before waiting')
            time.sleep(0.02)
        else:
            pytest.fail('offline CLI did not reach waiting')
        process.send_signal(signal.SIGTERM if graceful else signal.SIGKILL)
        output, errors = process.communicate(timeout=8)
        assert process.returncode == (130 if graceful else -signal.SIGKILL), output + errors
        assert snapshot(tmp_path / 'state')['phase'] == ('stopped' if graceful else 'waiting')
        assert not task_running(tmp_path / 'state')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
