import json
import math
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import seu_reserve.app as app
import pytest
from seu_reserve.client import AuthenticationExpired, Client
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


def test_preparation_that_starts_after_its_deadline_does_not_fetch_a_stale_image(tmp_path, monkeypatch):
    school, calls, results, image_threads = School(), [], [], []
    opening = math.ceil(time.time())+2
    stamp = datetime.fromtimestamp(opening,SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    school.activities = [native_activity(opens=stamp,closes='2030-01-01 09:00:00')]
    install_reader_sessions(monkeypatch,school,calls)
    request = school.request
    class DelayedWorker(app.CaptchaWorker):
        def submit(self, prepare, *, generation):
            number = self.number
            def delayed():
                if number == 0:
                    time.sleep(.65)
                return prepare()
            return super().submit(delayed, generation=generation)
    def observed_request(method,url,**kwargs):
        if urlsplit(url).path.endswith('vcode.do'):
            image_threads.append(threading.current_thread().name)
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
    assert results==[130] and image_threads==['captcha-worker']
    assert any(row['event']=='captcha_prefetch_failed' and row.get('reason')=='deadline_expired'
        for row in diagnostics_rows(tmp_path))


@pytest.mark.parametrize('expire', [False, True])
def test_preparation_leaves_readers_running_and_hands_session_back_before_submit(tmp_path, monkeypatch, expire):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    in_preparation = [False]
    request_threads = []
    class DelayedCaptcha(School):
        def request(self, method, url, **kwargs):
            endpoint = urlsplit(url).path.rsplit('/',1)[-1]
            if endpoint == 'vcode.do':
                request_threads.append(threading.current_thread().name)
                in_preparation[0] = True
                entered.set()
                try:
                    assert release.wait(2)
                    return super().request(method,url,**kwargs)
                finally:
                    in_preparation[0] = False
            if endpoint == 'addReservation.do':
                assert not in_preparation[0]
                request_threads.append(threading.current_thread().name)
            return super().request(method,url,**kwargs)
    school = DelayedCaptcha()
    opening = math.ceil(time.time()) + 2
    stamp = datetime.fromtimestamp(opening, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    school.activities = [native_activity(opens=stamp,closes='2030-01-01 09:00:00')]
    calls = []
    install_reader_sessions(monkeypatch, school, calls)
    expired = threading.Event()
    original_activities, authenticate = Client.activities, Client.authenticate
    def activities(client):
        if expire and client.request_role == 'activities' and entered.is_set() and not expired.is_set():
            expired.set()
            raise AuthenticationExpired('business_authentication_expired')
        return original_activities(client)
    def exclusive_authentication(client):
        assert not in_preparation[0]
        return authenticate(client)
    monkeypatch.setattr(Client, 'activities', activities)
    monkeypatch.setattr(Client, 'authenticate', exclusive_authentication)
    monkeypatch.setattr(app, 'short_backoff', lambda _: .01)
    results, writers = [], []
    save = app.Task.save
    def recorded_save(task):
        writers.append(threading.current_thread().name)
        return save(task)
    monkeypatch.setattr(app.Task, 'save', recorded_save)
    runner = threading.Thread(name='task-owner',target=lambda:results.append(app.run(
        app.Settings(state_dir=tmp_path,prewarm=False),session=school,
        environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _: '1234',stop=stopped.is_set)))
    runner.start()
    try:
        assert entered.wait(4)
        count = len(calls)
        if expire:
            assert expired.wait(.3)
        else:
            until(lambda: len(calls)>count, seconds=.3)
        assert not school.submitted
        release.set()
        until(lambda: bool(school.submitted), seconds=3)
    finally:
        release.set()
        stopped.set()
        runner.join(4)
    assert results == [130]
    assert request_threads == (['captcha-worker','captcha-worker','task-owner'] if expire
                              else ['captcha-worker','task-owner'])
    assert set(writers) == {'task-owner'}
    started = [row for row in diagnostics_rows(tmp_path) if row['event']=='submission_started']
    assert started[0]['captcha_source'] == ('fresh' if expire else 'prefetch')
    assert not any(t.name=='captcha-worker' for t in threading.enumerate())
