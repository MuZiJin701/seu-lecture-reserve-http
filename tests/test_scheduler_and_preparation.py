import json
import math
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import seu_reserve.app as app
import pytest
from seu_reserve.model import SHANGHAI
from test_app import School, diagnostics_rows, execute, lecture, native_activity
from test_unified_workers import install_reader_sessions


def until(predicate, seconds=4):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        time.sleep(.005)
    assert predicate()


def has_event(directory, name):
    path = directory/'diagnostics.jsonl'
    return path.exists() and any(json.loads(line).get('event') == name
        for line in path.read_text(encoding='utf-8').splitlines())


def test_absolute_wait_subtracts_time_spent_flushing_the_checkpoint(tmp_path, monkeypatch):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    now = [1000.0]
    original = app.Task.save
    saves_in_wait = []
    def save(task):
        result = original(task)
        if task.data.get('health',{}).get('phase')=='waiting':
            now[0] += .1
            saves_in_wait.append(1)
        return result
    monkeypatch.setattr(app.Task,'save',save)
    def sleep(seconds):
        checkpoint = json.loads((tmp_path/'task.json').read_text())
        assert seconds <= checkpoint['health']['next_check_at'] - now[0] + .00001
        raise KeyboardInterrupt
    assert execute(school,tmp_path,clock=lambda:now[0],monotonic=lambda:now[0],sleep=sleep)==130
    assert saves_in_wait


def test_log_handles_close_before_releasing_the_account_checkpoint_lock(tmp_path, monkeypatch):
    closed = []
    log_close, task_close = app.LocalLogs.close, app.Task.close
    def close_logs(logs):
        result = log_close(logs)
        closed.append(True)
        return result
    def close_task(task):
        assert closed
        return task_close(task)
    monkeypatch.setattr(app.LocalLogs,'close',close_logs)
    monkeypatch.setattr(app.Task,'close',close_task)
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    assert execute(school,tmp_path)==130


def test_missing_list_uses_deadlines_instead_of_fast_scan_polling(tmp_path, monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    waits = []
    class ObservedSignal(app.WorkSignal):
        def wait(self, timeout=None):
            waits.append(timeout)
            return super().wait(timeout)
    class DelayedList(School):
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                entered.set()
                assert release.wait(2)
            return super().request(method,url,**kwargs)
    school, calls, results = DelayedList(), [], []
    task = app.Task(tmp_path,'test-user')
    now = time.time()
    task.data['selection'] = dict(activities=[dict(wid='one',title='constructed',
        reason='reservation_not_started',
        reservation_start=datetime.fromtimestamp(now-1,SHANGHAI).isoformat(),
        reservation_end=datetime.fromtimestamp(now+60,SHANGHAI).isoformat())])
    task.save()
    task.close()
    install_reader_sessions(monkeypatch,school,calls)
    monkeypatch.setattr(app,'WorkSignal',ObservedSignal)
    runner = threading.Thread(target=lambda:results.append(app.run(
        app.Settings(state_dir=tmp_path,prewarm=False,captcha_prefetch=False),session=school,
        environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',stop=stopped.is_set)))
    runner.start()
    try:
        assert entered.wait(1)
        until(lambda:bool(waits))
        time.sleep(.25)
        assert waits and all(value>=.1 for value in waits)
        assert sum(endpoint=='queryActivityList.do' for endpoint,*_ in calls)==1
    finally:
        stopped.set()
        release.set()
        runner.join(3)
    assert results==[130]


def test_stop_interrupts_a_server_wait_without_sending_more_requests(tmp_path, monkeypatch):
    class WaitingSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method,url,**kwargs)
            if '/modules/hdyy/' in url:
                response.status_code = 429
                response.headers['Retry-After'] = '20'
            return response
    school, calls, results = WaitingSchool(), [], []
    install_reader_sessions(monkeypatch,school,calls)
    stopped = threading.Event()
    runner = threading.Thread(target=lambda: results.append(app.run(app.Settings(state_dir=tmp_path),
        session=school,environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _: '1234',stop=stopped.is_set)))
    runner.start()
    try:
        path = tmp_path/'task.json'
        def backing_off():
            try:
                return json.loads(path.read_text()).get('health',{}).get('phase')=='backoff'
            except (FileNotFoundError, PermissionError):
                return False
        until(backing_off)
        before, started = len(calls), time.monotonic()
        stopped.set()
        runner.join(1)
        assert not runner.is_alive() and time.monotonic()-started < 1
        assert len(calls) == before
    finally:
        stopped.set()
        runner.join(3)
    assert results == [130]


def test_positive_history_wakes_the_two_second_pending_pause(tmp_path, monkeypatch):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    calls = []
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app.random, 'uniform', lambda *_: .02)
    monkeypatch.setattr(app, 'short_backoff', lambda _: .01)
    original = app.new_history_poller
    def history(query, **kwargs):
        kwargs['interval'] = .02
        return original(query, **kwargs)
    monkeypatch.setattr(app, 'new_history_poller', history)
    stopped = threading.Event()
    results = []
    runner = threading.Thread(target=lambda: results.append(app.run(app.Settings(state_dir=tmp_path),
        session=school, environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _: '1234', stop=stopped.is_set)))
    runner.start()
    try:
        until(lambda: has_event(tmp_path, 'reservation_checked'))
        started = time.monotonic()
        school.mine.append({**lecture(), 'HD_WID':'one', 'YYZT':'已预约'})
        until(lambda: has_event(tmp_path, 'reservation_confirmed'), seconds=1.5)
        assert time.monotonic()-started < 1.5
    finally:
        stopped.set()
        runner.join(4)
    assert results == [130]
    checks = [row for row in diagnostics_rows(tmp_path) if row['event']=='reservation_checked']
    assert [row['checks'] for row in checks] == [1]
    assert school.submitted == ['one']


def test_pending_history_confirms_while_first_list_request_is_still_waiting(tmp_path, monkeypatch):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'

    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    calls, results = [], []
    request = school.request

    def blocked_list(method, url, **kwargs):
        if url.endswith('queryActivityList.do'):
            entered.set()
            assert release.wait(4)
        result = request(method, url, **kwargs)
        if url.endswith('queryMyActivityList.do'):
            school.mine = [{**lecture(), 'HD_WID': 'one', 'YYZT': '已预约'}]
        return result

    school.mine = []
    monkeypatch.setattr(school, 'request', blocked_list)
    install_reader_sessions(monkeypatch, school, calls)
    runner = threading.Thread(target=lambda: results.append(app.run(
        app.Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'}, stop=stopped.is_set)))
    runner.start()
    try:
        assert entered.wait(1)
        until(lambda: has_event(tmp_path, 'reservation_confirmed'), seconds=2)
        assert not release.is_set() and school.submitted == ['one']
    finally:
        stopped.set()
        release.set()
        runner.join(4)
    assert results == [130]


def test_preparation_that_starts_after_its_deadline_does_not_fetch_a_stale_image(tmp_path, monkeypatch):
    school, calls, results, image_jobs = School(), [], [], []
    active_job = None
    opening = math.ceil(time.time())+2
    stamp = datetime.fromtimestamp(opening,SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    school.activities = [native_activity(opens=stamp,closes='2030-01-01 09:00:00')]
    install_reader_sessions(monkeypatch,school,calls)
    request = school.request
    monkeypatch.setattr(app, 'CAPTCHA_PREFETCH_TIMEOUT', .1)
    class DelayedWorker(app.CaptchaWorker):
        def submit(self, prepare, *, generation):
            number = self.number
            def delayed():
                nonlocal active_job
                active_job = number
                if number == 0:
                    # The worker starts too late to use the configured budget.
                    time.sleep(.2)
                return prepare()
            return super().submit(delayed, generation=generation)
    def observed_request(method,url,**kwargs):
        if urlsplit(url).path.endswith('vcode.do'):
            image_jobs.append(active_job)
        return request(method,url,**kwargs)
    monkeypatch.setattr(app,'CaptchaWorker',DelayedWorker)
    monkeypatch.setattr(school,'request',observed_request)
    stopped = threading.Event()
    runner = threading.Thread(name='task-owner',target=lambda:results.append(app.run(
        app.Settings(state_dir=tmp_path,prewarm=False),session=school,
        environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',stop=stopped.is_set)))
    runner.start()
    try:
        until(lambda:bool(school.submitted),seconds=5)
    finally:
        stopped.set()
        runner.join(3)
    assert results == [130] and image_jobs and all(job is not None and job > 0 for job in image_jobs)
    assert any(row['event']=='captcha_prefetch_failed' and row.get('reason')=='deadline_expired'
        for row in diagnostics_rows(tmp_path))
