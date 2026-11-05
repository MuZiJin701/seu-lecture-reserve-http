import json
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

import seu_reserve.app as app
from seu_reserve.client import AuthenticationExpired, Client
from test_app import School, diagnostics_rows, lecture
from test_scheduler_and_preparation import has_event, until
from test_unified_workers import install_reader_sessions


@pytest.mark.parametrize('change', [
    'none', 'full', 'history', 'history_format', 'list_timeout', 'list_expired',
    'auth', 'rate_limit', 'stop'])
def test_fresh_captcha_keeps_owner_responsive_and_revalidates_before_submit(tmp_path, monkeypatch, change):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    closing, expired, rate_limited = threading.Event(), threading.Event(), threading.Event()
    expire_next, limit_next = threading.Event(), threading.Event()
    list_timeout, ocr_done = threading.Event(), threading.Event()
    clock_shift = [0.0]
    busy = [False]
    image_threads, submit_codes, auth_threads, writers, ocr_threads = [], [], [], [], []
    class DelayedSchool(School):
        def request(self, method, url, **kwargs):
            endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
            if endpoint == 'vcode.do':
                assert not busy[0]
                busy[0] = True
                image_threads.append(threading.get_ident())
                try:
                    if len(image_threads) == 1:
                        entered.set()
                        assert release.wait(4)
                    return super().request(method, url, **kwargs)
                finally:
                    busy[0] = False
            if endpoint == 'addReservation.do':
                assert not busy[0] and threading.current_thread().name == 'task-owner'
                submit_codes.append(kwargs['data']['vcode'])
            if endpoint == 'queryActivityList.do' and expire_next.is_set() and not expired.is_set():
                expired.set()
                raise AuthenticationExpired('business_authentication_expired')
            if endpoint == 'queryActivityList.do' and list_timeout.is_set():
                raise requests.ReadTimeout('constructed list timeout')
            response = super().request(method, url, **kwargs)
            if endpoint == 'queryMyActivityList.do' and limit_next.is_set() and not rate_limited.is_set():
                rate_limited.set()
                response.status_code = 429
                response.headers['Retry-After'] = '20'
            return response
    school, calls, results = DelayedSchool(), [], []
    school.activities = [lecture()]
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app.random, 'uniform', lambda *_: .02)
    monkeypatch.setattr(app, 'short_backoff', lambda _: .01)
    history_poller = app.new_history_poller
    def frequent_history(query, **kwargs):
        kwargs['interval'] = .02
        return history_poller(query, **kwargs)
    monkeypatch.setattr(app, 'new_history_poller', frequent_history)
    authenticate, save = Client.authenticate, app.Task.save
    def exclusive_authentication(client):
        assert not busy[0]
        auth_threads.append(threading.current_thread().name)
        return authenticate(client)
    def owner_save(task):
        writers.append(threading.current_thread().name)
        return save(task)
    monkeypatch.setattr(Client, 'authenticate', exclusive_authentication)
    monkeypatch.setattr(app.Task, 'save', owner_save)
    class ObservedWorker(app.CaptchaWorker):
        def close(self):
            closing.set()
            super().close()
    monkeypatch.setattr(app, 'CaptchaWorker', ObservedWorker)
    def recognize(_):
        ocr_threads.append(threading.get_ident())
        ocr_done.set()
        return '1111' if len(ocr_threads) == 1 else '2222'
    runner = threading.Thread(name='task-owner', target=lambda: results.append(app.run(
        app.Settings(state_dir=tmp_path, prewarm=False, captcha_prefetch=False), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
        recognize=recognize, clock=lambda: time.time() + clock_shift[0], monotonic=time.monotonic,
        stop=stopped.is_set)))
    runner.start()
    try:
        assert entered.wait(3)
        before = len(calls)
        until(lambda: len(calls) > before + 2)
        assert not school.submitted
        if change == 'full':
            school.activities = [lecture(YYZT='已满')]
            before = len(calls)
            until(lambda: len(calls) > before + 2)
        elif change == 'history':
            school.mine = [{**lecture('other'), 'HD_WID': 'other', 'YYZT': '已预约'}]
            before = len(calls)
            until(lambda: len(calls) > before + 2)
        elif change == 'history_format':
            valid = {**lecture('other'), 'HD_WID': 'other', 'YYZT': '已预约'}
            school.mine = [valid, {**valid, 'YYZT': '已取消'}]
            before = len(calls)
            until(lambda: len(calls) > before + 3, seconds=1)
        elif change in ('list_timeout', 'list_expired'):
            list_calls = sum(call[0] == 'queryActivityList.do' for call in calls)
            list_timeout.set()
            until(lambda: sum(call[0] == 'queryActivityList.do' for call in calls) >= list_calls + 2)
            if change == 'list_expired':
                clock_shift[0] = 11.0
        elif change == 'auth':
            expire_next.set()
            assert expired.wait(1)
            until(lambda: has_event(tmp_path, 'request_failed'))
            assert auth_threads == ['task-owner']
        elif change == 'rate_limit':
            limit_next.set()
            assert rate_limited.wait(1)
            def backing_off():
                try:
                    return json.loads((tmp_path/'task.json').read_text()).get('retry_server') is True
                except (FileNotFoundError, PermissionError):
                    return False
            until(backing_off)
        elif change == 'stop':
            stopped.set()
            assert closing.wait(1)
            assert runner.is_alive() and not school.submitted
        release.set()
        if change == 'list_expired':
            assert ocr_done.wait(1)
            until(lambda: has_event(tmp_path, 'reader_snapshot_missing'))
            assert not school.submitted
            list_timeout.clear()
        if change in ('none', 'auth', 'list_timeout', 'list_expired', 'history_format'):
            until(lambda: bool(school.submitted))
        elif change in ('full', 'history'):
            until(lambda: has_event(tmp_path, 'captcha_candidate_changed'))
    finally:
        stopped.set()
        release.set()
        runner.join(5)
    assert results == [130] and not runner.is_alive()
    assert set(writers) == {'task-owner'}
    assert len(set(image_threads)) == 1 and ocr_threads == image_threads
    assert not any(t.name == 'captcha-worker' for t in threading.enumerate())
    jobs = [row for row in diagnostics_rows(tmp_path) if row['event'] == 'captcha_job_started']
    assert [row['job'] for row in jobs] == list(range(1, len(jobs) + 1))
    if change == 'auth':
        assert submit_codes == ['2222'] and auth_threads == ['task-owner', 'task-owner']
        assert [row['generation'] for row in jobs] == [1, 2]
    elif change in ('none', 'list_timeout', 'list_expired', 'history_format'):
        assert submit_codes == ['1111'] and len(image_threads) == 1
    else:
        assert not submit_codes and len(image_threads) == 1


def test_rejected_fresh_codes_reuse_one_worker_and_keep_four_attempt_limit(tmp_path, monkeypatch):
    school, calls, results, images, ocr = School(), [], [], [], []
    school.activities = [lecture(), lecture('two')]
    school.submit_replies = [{'code': 500, 'msg': '验证码错误'}] * 4 + [{'code': 500}]
    request = school.request
    def observed_request(method, url, **kwargs):
        if urlsplit(url).path.endswith('vcode.do'):
            images.append(threading.get_ident())
            assert threading.current_thread().name == 'captcha-worker'
        if urlsplit(url).path.endswith('addReservation.do'):
            assert threading.current_thread().name == 'task-owner'
        return request(method, url, **kwargs)
    monkeypatch.setattr(school, 'request', observed_request)
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app.random, 'uniform', lambda *_: .02)
    stopped = threading.Event()
    def recognize(_):
        ocr.append(threading.get_ident())
        return '1234'
    runner = threading.Thread(name='task-owner', target=lambda: results.append(app.run(
        app.Settings(state_dir=tmp_path, captcha_prefetch=False), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
        recognize=recognize, stop=stopped.is_set)))
    runner.start()
    try:
        until(lambda: len(school.submitted) == 5)
    finally:
        stopped.set()
        runner.join(4)
    assert results == [130]
    assert school.submitted == ['one'] * 4 + ['two']
    assert len(images) == 5 and len(set(images)) == 1 and ocr == images
    rows = diagnostics_rows(tmp_path)
    assert not any(row['event'] == 'captcha_cooldown' for row in rows)
    assert not json.loads((tmp_path / 'task.json').read_text())['cooldowns']
    assert [row['job'] for row in rows if row['event'] == 'captcha_job_started'] == [1, 2, 3, 4, 5]


def test_dispatched_job_obeys_server_wait_learned_before_fetch(tmp_path, monkeypatch):
    entered, release, limited, stopped, finished = (threading.Event() for _ in range(5))
    image_requests = []
    class HeldWorker(app.CaptchaWorker):
        def submit(self, prepare, *, generation):
            def delayed():
                entered.set()
                assert release.wait(3)
                try:
                    return prepare()
                finally:
                    finished.set()
            super().submit(delayed, generation=generation)
    class LimitedSchool(School):
        def request(self, method, url, **kwargs):
            if urlsplit(url).path.endswith('vcode.do'):
                image_requests.append(1)
            response = super().request(method, url, **kwargs)
            if urlsplit(url).path.endswith('queryMyActivityList.do') and entered.is_set():
                response.status_code = 429
                response.headers['Retry-After'] = '20'
                limited.set()
            return response
    school, calls, results = LimitedSchool(), [], []
    school.activities = [lecture()]
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app, 'CaptchaWorker', HeldWorker)
    original = app.new_history_poller
    def history(query, **kwargs):
        kwargs['interval'] = .02
        return original(query, **kwargs)
    monkeypatch.setattr(app, 'new_history_poller', history)
    runner = threading.Thread(target=lambda: results.append(app.run(
        app.Settings(state_dir=tmp_path, captcha_prefetch=False), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
        recognize=lambda _: '1234', stop=stopped.is_set)))
    runner.start()
    try:
        assert entered.wait(3) and limited.wait(1)
        def backing_off():
            try:
                return json.loads((tmp_path/'task.json').read_text()).get('retry_server') is True
            except (FileNotFoundError, PermissionError):
                return False
        until(backing_off)
        release.set()
        assert finished.wait(1) and not image_requests
    finally:
        release.set()
        stopped.set()
        runner.join(4)
    assert results == [130] and not school.submitted
