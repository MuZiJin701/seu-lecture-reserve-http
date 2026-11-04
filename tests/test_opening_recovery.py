"""Opening query recovery, isolated from real school credentials and network."""
from urllib.parse import urlsplit

import pytest
import requests

import seu_reserve.app as app
from seu_reserve.poller import Snapshot
from test_app import (FakeHistoryPoller, FakePoller, School, diagnostics_rows, execute,
                      moment, native_activity, seed_opening_selection)






@pytest.fixture
def opening(tmp_path, monkeypatch):
    start = moment('2026-10-18 12:00:00').timestamp()
    now = [start]
    seed_opening_selection(tmp_path, start='2026-10-18T12:00:04+08:00')
    row = native_activity(opens='2026-10-18 12:00:04')
    row.update(YYRS=1, HDZRS=1)
    for key, value in dict(next_rows=[row], rows_override=None, next_error=None,
                           at_offset=0, next_wake_once=False, on_timeout=None).items():
        monkeypatch.setattr(FakePoller, key, value)
    monkeypatch.setattr(FakePoller, 'instances', [])
    for key, value in dict(rows=[], error=None, reason=None, retry_after=0,
                           server_directed=False, starts=0, events=[]).items():
        monkeypatch.setattr(FakeHistoryPoller, key, value)
    monkeypatch.setattr(app, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app, 'new_history_poller', FakeHistoryPoller)
    monkeypatch.setattr(app.random, 'uniform', lambda low, high: (low + high) / 2)
    school = School()
    school.activities = [row]
    def wait(seconds):
        now[0] += max(0, seconds)
        if now[0] >= start + 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(FakePoller, 'on_timeout', wait)
    return start, now, school, wait


@pytest.mark.parametrize('reader_kind', ['list', 'history'])
def test_busy_reader_is_not_bypassed_by_a_main_thread_query(
        opening, reader_kind, tmp_path, monkeypatch):
    start, now, school, wait = opening
    class BusyList(FakePoller):
        def snapshot(self):
            return None if now[0] < start + .65 else super().snapshot()
    class BusyHistory(FakeHistoryPoller):
        def snapshot(self):
            return None if now[0] < start + .65 else super().snapshot()
    if reader_kind == 'list':
        monkeypatch.setattr(app, 'ActivityPoller', BusyList)
    else:
        monkeypatch.setattr(app, 'new_history_poller', BusyHistory)
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True, settings=app.Settings(
                       state_dir=tmp_path, prewarm=False, captcha_prefetch=False)) == 130
    endpoint = 'queryActivityList.do' if reader_kind == 'list' else 'queryMyActivityList.do'
    calls = [url for _, url, _ in school.requests if url.endswith(endpoint)]
    assert calls == []


def test_reader_error_keeps_both_owners_alive(
        opening, tmp_path, monkeypatch):
    start, now, school, wait = opening
    class FailedList(FakePoller):
        def snapshot(self):
            if now[0] >= start + .3:
                return Snapshot([], now[0], error='RemoteError', reason='network_timeout',
                                failed_at=now[0])
            return super().snapshot()
    original = school.request
    def request(method, url, **kwargs):
        if urlsplit(url).path.endswith('queryActivityList.do') and now[0] >= start + .3:
            now[0] += .1
            raise requests.Timeout()
        return original(method, url, **kwargs)
    monkeypatch.setattr(school, 'request', request)
    monkeypatch.setattr(app, 'ActivityPoller', FailedList)
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True, settings=app.Settings(
                       state_dir=tmp_path, prewarm=False, captcha_prefetch=False)) == 130
    assert 'event=network_timeout' in (tmp_path / 'events.log').read_text()
    assert FakeHistoryPoller.starts == 1
    assert len(FakePoller.instances) == 1
    assert FakeHistoryPoller.events.count('history_stop') == 1  # final cleanup only
    assert not school.submitted


def test_fast_snapshot_age_does_not_expand_with_reader_timeout(opening, tmp_path, monkeypatch):
    _, now, school, wait = opening
    monkeypatch.setattr(FakePoller, 'at_offset', -2.001)
    monkeypatch.setattr(app, 'OPENING_BACKGROUND_TIMEOUT', 20.0)
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True, settings=app.Settings(
                       state_dir=tmp_path, prewarm=False, captcha_prefetch=False)) == 130
    assert any(r['event'] == 'reader_snapshot_missing' for r in diagnostics_rows(tmp_path))
    assert all(r.get('rows_age', 0) < .75 for r in diagnostics_rows(tmp_path)
               if r['event'] == 'scan_timing' and r.get('rows_source') == 'poller')


def test_in_flight_reader_is_not_replaced_until_its_request_finishes(
        tmp_path, monkeypatch):
    """A stopped reader whose request has not returned keeps the endpoint until it finishes."""
    class DrainedPoller(FakePoller):
        created = 0
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            DrainedPoller.created += 1
            self.drained = DrainedPoller.created == 1
            if self.drained:
                self.running = False
                self.in_flight_since = self.clock()
        def start(self):
            if not self.drained:
                self.running = True
        def snapshot(self):
            if not self.drained:
                return super().snapshot()
            now = self.clock()
            return Snapshot([], now, error='RemoteError', reason='network_timeout',
                            failed_at=now, server_directed=False)
    monkeypatch.setattr(app, 'ActivityPoller', DrainedPoller)
    monkeypatch.setattr(app, 'new_history_poller', FakeHistoryPoller)
    FakePoller.instances.clear()
    FakePoller.on_timeout = None
    FakeHistoryPoller.rows = []
    FakeHistoryPoller.error = None
    FakeHistoryPoller.retry_after = 0.0
    FakeHistoryPoller.server_directed = False
    now = [moment('2026-10-18 12:00:00').timestamp()]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += max(0, seconds)
        if len(waits) == 1:
            # While the first reader's request is still in flight it must not be replaced.
            assert len(FakePoller.instances) == 1
        if len(waits) == 2:
            FakePoller.instances[0].in_flight_since = None
        if len(waits) >= 6:
            raise KeyboardInterrupt
    monkeypatch.setattr(FakePoller, 'on_timeout', wait)
    assert execute(School(), tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True, settings=app.Settings(
                       state_dir=tmp_path, prewarm=False, captcha_prefetch=False)) == 130
    assert len(FakePoller.instances) >= 2
    assert all(instance.drained for instance in FakePoller.instances[:1])
