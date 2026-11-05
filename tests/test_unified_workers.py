import json
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import seu_reserve.app as app
from seu_reserve.client import Client
from seu_reserve.model import SHANGHAI
from test_app import (School, lecture, native_activity, FakePoller, FakeHistoryPoller,
                      execute, seed_opening_selection, diagnostics_rows, moment)
import pytest
import requests
from seu_reserve.state import Task


@pytest.mark.parametrize(('offset','expected'),[
    (-31,(10,10)),(-30,(2,2)),(-5,(2,2)),
    (8.99,(2,2)),(9,(2,2)),(300,(10,10)),
])
def test_shared_read_policy_at_phase_boundaries(offset,expected):
    policy=app.read_policy([(1000,1300)],1000+offset)
    assert (policy.timeout,policy.list_max_age)==expected


def install_reader_sessions(monkeypatch, school, calls):
    class ReadSession:
        def request(self, method, url, **kwargs):
            endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
            assert endpoint in ('queryActivityList.do', 'queryMyActivityList.do')
            calls.append((endpoint, threading.current_thread().name, threading.get_ident(), kwargs['timeout']))
            return school.request(method, url, **kwargs)
        def close(self):
            pass
    def clone(client):
        result = Client(ReadSession(), client.environ, client.prompt, client.notify,
                        diagnostic=client.diagnostic)
        return result
    monkeypatch.setattr(Client, 'clone_readonly', clone)


def test_normal_readers_book_and_mail_does_not_block_scanning(tmp_path, monkeypatch):
    school = School()
    school.activities = [lecture()]
    calls = []
    writers = []
    save = Task.save
    def save_on_owner(task):
        writers.append(threading.current_thread().name)
        return save(task)
    monkeypatch.setattr(Task, 'save', save_on_owner)
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app.random, 'uniform', lambda low, high: .02)
    mailed, release, stopped = threading.Event(), threading.Event(), threading.Event()
    result = []
    send_threads = []
    def notify(event):
        send_threads.append(threading.current_thread().name)
        mailed.set()
        assert release.wait(3)
    def runner():
        result.append(app.run(app.Settings(state_dir=tmp_path, mail_success=True),
            session=school, environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
            notify=notify, recognize=lambda _: '1234', stop=stopped.is_set))
    thread = threading.Thread(target=runner, name='task-owner')
    thread.start()
    try:
        assert mailed.wait(3)
        before = len(calls)
        deadline = time.monotonic()+2
        while len(calls) <= before and time.monotonic()<deadline:
            time.sleep(.01)
        assert len(calls)>before
    finally:
        release.set()
        stopped.set()
        thread.join(4)
    assert result == [130]
    assert school.submitted == ['one']
    assert send_threads == ['mail-sender']
    assert {name for _,name,_,_ in calls} == {'activities-reader','history-reader'}
    assert all(budget == (8 if name == 'history-reader' else 10) for _,name,_,budget in calls)
    state = json.loads((tmp_path/'task.json').read_text())
    assert state['status'] != 'pending'
    assert set(writers) == {'task-owner'}


def test_production_wake_path_submits_before_the_pause_interval(tmp_path, monkeypatch):
    """The real readers' on_update must wake the default work_updated wait.

    With a broken wiring the main thread would sleep the full 6 s scan interval after its first
    empty pass, so a submission within the short deadline proves the production wake path.
    """
    school = School()
    school.activities = [lecture()]
    calls = []
    install_reader_sessions(monkeypatch, school, calls)
    monkeypatch.setattr(app.random, 'uniform', lambda low, high: high)
    stopped = threading.Event()
    result = []
    started = time.monotonic()
    thread = threading.Thread(target=lambda: result.append(app.run(
        app.Settings(state_dir=tmp_path), session=school,
        environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
        recognize=lambda _: '1234', stop=stopped.is_set)))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not school.submitted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert school.submitted == ['one']
        assert time.monotonic() - started < 3
    finally:
        stopped.set()
        thread.join(4)
    assert result == [130]


def test_phase_switch_keeps_both_reader_threads_and_changes_budgets(tmp_path, monkeypatch):
    school = School()
    base=time.time()
    school.server_now = datetime.fromtimestamp(base - 30, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    opening=datetime.fromtimestamp(base+120,SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    school.activities=[native_activity(opens=opening,closes='2030-01-01 09:00:00')]
    calls=[]
    install_reader_sessions(monkeypatch,school,calls)
    monkeypatch.setattr(app.random,'uniform',lambda low,high:.02)
    offset=[0.0]
    stopped=threading.Event()
    thread=threading.Thread(target=lambda:app.run(app.Settings(state_dir=tmp_path,prewarm=False,
        captcha_prefetch=False),session=school,environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',clock=lambda:time.time()+offset[0],stop=stopped.is_set))
    thread.start()
    try:
        for shift in (0,100,117):
            offset[0]=shift
            count=len(calls)
            deadline=time.monotonic()+2
            while len(calls)<count+2 and time.monotonic()<deadline:
                time.sleep(.01)
            assert len(calls)>=count+2
    finally:
        stopped.set()
        thread.join(4)
    assert not thread.is_alive()
    for name in ('activities-reader','history-reader'):
        assert len({ident for _,role,ident,_ in calls if role==name})==1
    assert {budget for _,name,_,budget in calls if name == 'activities-reader'} == {2, 10}
    assert {budget for _,name,_,budget in calls if name == 'history-reader'} == {8}
    assert not school.submitted


def test_transient_reader_failure_recovers_without_replacing_healthy_threads(tmp_path, monkeypatch):
    class FlakySchool(School):
        failed = False
        def request(self,method,url,**kwargs):
            if url.endswith('queryActivityList.do') and not self.failed:
                self.failed=True
                raise requests.Timeout('constructed failure')
            return super().request(method,url,**kwargs)
    school=FlakySchool()
    calls=[]
    install_reader_sessions(monkeypatch,school,calls)
    monkeypatch.setattr(app.random,'uniform',lambda low,high:.02)
    stopped=threading.Event()
    result=[]
    runner=threading.Thread(target=lambda:result.append(app.run(app.Settings(state_dir=tmp_path),
        session=school,environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',stop=stopped.is_set)))
    runner.start()
    try:
        deadline=time.monotonic()+3
        while sum(endpoint=='queryActivityList.do' for endpoint,*_ in calls)<3 and time.monotonic()<deadline:
            time.sleep(.01)
    finally:
        stopped.set();runner.join(4)
    assert result==[130]
    assert sum(endpoint=='queryActivityList.do' for endpoint,*_ in calls)>=3
    for name in ('activities-reader','history-reader'):
        assert len({ident for _,role,ident,_ in calls if role==name})==1


def test_server_wait_parks_both_readers_without_replacing_them(tmp_path,monkeypatch):
    class LimitedSchool(School):
        limited_at=None
        after=[]
        def request(self,method,url,**kwargs):
            if url.endswith(('queryActivityList.do','queryMyActivityList.do')):
                self.after.append(time.monotonic())
            if url.endswith('queryActivityList.do') and self.limited_at is None:
                self.limited_at=time.monotonic()
                response=requests.Response();response.status_code=429
                response.headers['Retry-After']='1';response._content=b''
                return response
            return super().request(method,url,**kwargs)
    school=LimitedSchool();calls=[]
    install_reader_sessions(monkeypatch,school,calls)
    monkeypatch.setattr(app.random,'uniform',lambda low,high:.02)
    stopped=threading.Event()
    runner=threading.Thread(target=lambda:app.run(app.Settings(state_dir=tmp_path),session=school,
        environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',stop=stopped.is_set))
    runner.start()
    try:
        deadline=time.monotonic()+3
        while (school.limited_at is None or time.monotonic()<school.limited_at+1.2) and time.monotonic()<deadline:
            time.sleep(.01)
    finally:
        stopped.set();runner.join(4)
    assert school.limited_at is not None
    assert not any(school.limited_at+.05<t<school.limited_at+.95 for t in school.after)
    assert any(t>=school.limited_at+1 for t in school.after)
    for name in ('activities-reader','history-reader'):
        assert len({ident for _,role,ident,_ in calls if role==name})==1


def test_opening_alert_queues_immediately_after_its_failure_threshold(tmp_path,monkeypatch):
    now=time.time()
    task=Task(tmp_path,'test-user')
    task.data['query_failure_since']=now-301
    task.data['selection']=dict(updated_at=now,activities=[dict(title='constructed',reason='reservation_not_started',
        reservation_start=datetime.fromtimestamp(now,SHANGHAI).isoformat(),
        reservation_end=datetime.fromtimestamp(now+300,SHANGHAI).isoformat())])
    task.save();task.close()
    class FailingSchool(School):
        def request(self,method,url,**kwargs):
            if url.endswith('queryActivityList.do'):
                raise requests.Timeout('constructed outage')
            return super().request(method,url,**kwargs)
    school=FailingSchool();calls=[]
    install_reader_sessions(monkeypatch,school,calls)
    sent=threading.Event();messages=[]
    def notify(event):
        messages.append((event,threading.current_thread().name));sent.set()
    stopped=threading.Event()
    runner=threading.Thread(target=lambda:app.run(app.Settings(state_dir=tmp_path),session=school,
        environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},notify=notify,
        recognize=lambda _:'1234',stop=stopped.is_set))
    runner.start()
    try:
        assert sent.wait(2)
    finally:
        stopped.set();runner.join(4)
    assert messages==[('query_failure','mail-sender')]
    state=json.loads((tmp_path/'task.json').read_text())
    assert 'query_failure' in state['alerts']
    assert 'notification_deferred' not in (tmp_path/'events.log').read_text()


@pytest.mark.parametrize('enabled', [True, False])
def test_fast_opening_notifies_history_once(tmp_path,monkeypatch,enabled):
    start=moment('2026-10-18 12:00:00').timestamp()
    now=[start+4]
    seed_opening_selection(tmp_path,start='2026-10-18T12:00:10+08:00')
    row=native_activity(opens='2026-10-18 12:00:10');row.update(YYRS=1, HDZRS=1, _SERVER_NOW='2026-10-18 12:00:00')
    monkeypatch.setattr(app,'ActivityPoller',FakePoller)
    notifications = []
    class History(FakeHistoryPoller):
        def request_refresh(self):
            notifications.append(now[0])
            return super().request_refresh()
    monkeypatch.setattr(app,'new_history_poller',History)
    for key,value in dict(instances=[],next_rows=[row],rows_override=None,at_offset=0,
                           next_error=None,next_wake_once=False).items():
        monkeypatch.setattr(FakePoller,key,value)
    for key,value in dict(rows=[],error=None,reason=None,retry_after=0,server_directed=False).items():
        monkeypatch.setattr(FakeHistoryPoller,key,value)
    monkeypatch.setattr(app.random,'uniform',lambda lower,upper:.075)
    def wait(seconds):
        now[0]+=seconds
        if now[0]>=start+11:
            raise KeyboardInterrupt
    monkeypatch.setattr(FakePoller,'on_timeout',wait)
    school=School();school.activities=[row]
    assert execute(school,tmp_path,clock=lambda:now[0],sleep=wait,background_poller=True,
        settings=app.Settings(state_dir=tmp_path,prewarm=enabled,captcha_prefetch=False))==130
    assert notifications == (pytest.approx([start+5], abs=.1) if enabled else [])
    assert not any(url.endswith('queryMyActivityList.do') for _,url,_ in school.requests)


def test_each_negative_confirmation_uses_distinct_post_check_queries(tmp_path,monkeypatch):
    school=School();school.activities=[lecture()];school.submit_timeout=True
    calls=[]
    install_reader_sessions(monkeypatch,school,calls)
    monkeypatch.setattr(app.random,'uniform',lambda lower,upper:.02)
    monkeypatch.setattr(app,'PENDING_MISS_PAUSE',.02)
    stopped=threading.Event()
    result=[]
    runner=threading.Thread(target=lambda:result.append(app.run(app.Settings(state_dir=tmp_path),
        session=school,environ={'SEU_USERNAME':'test-user','SEU_PASSWORD':'test-password'},
        recognize=lambda _:'1234',stop=stopped.is_set)))
    runner.start()
    try:
        deadline=time.monotonic()+3
        eventfile=tmp_path/'events.log'
        while time.monotonic()<deadline:
            if eventfile.exists() and 'reservation_unconfirmed_released' in eventfile.read_text():break
            time.sleep(.005)
    finally:
        stopped.set();runner.join(4)
    assert result==[130]
    checks=[r for r in diagnostics_rows(tmp_path) if r['event']=='reservation_checked']
    assert [r['checks'] for r in checks[:3]]==[1,2,3]
    assert len({r['check_started_at'] for r in checks[:3]})==3
    for row in checks:
        assert row['activities_started_at']>=row['check_started_at']
        assert row['history_started_at']>=row['check_started_at']


def test_wait_learned_while_recycling_history_keeps_the_list_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'ActivityPoller', FakePoller)
    for key, value in dict(instances=[], next_rows=[], rows_override=None, at_offset=0,
                           next_error=None, next_wake_once=False, on_timeout=None).items():
        monkeypatch.setattr(FakePoller, key, value)
    gates = []
    class StoppedHistory(FakeHistoryPoller):
        def __init__(self, query, *, gate, **kwargs):
            super().__init__(query, **kwargs)
            self.gate = gate
            gates.append(gate)
        def start(self):
            self.running = False
        def snapshot(self):
            return None
        def stop(self, *, join=True):
            self.gate.record(120)
    monkeypatch.setattr(app, 'new_history_poller', StoppedHistory)
    now = [1000.0]
    def wait(seconds):
        if gates and gates[0].remaining() > 0:
            assert FakePoller.instances[0].running
            raise KeyboardInterrupt
        now[0] += seconds
    assert execute(School(), tmp_path, background_poller=True,
                   clock=lambda: now[0], sleep=wait) == 130
