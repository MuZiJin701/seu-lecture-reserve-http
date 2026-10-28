import threading

import pytest

import seu_reserve.app as app_module
import seu_reserve.client as client_module
from seu_reserve.poller import Snapshot
from test_activities import ActivitySchool, OPEN, activity
from test_app import School, execute


def test_background_poller_reads_list_during_opening(tmp_path, monkeypatch):
    gate = threading.Event()
    calls = []

    class Clone:
        session = None

        def activities(self):
            calls.append('poller')
            gate.set()
            return [activity()]

    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: Clone())
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = '2029-12-31 18:59:50'

    def wait(seconds):
        assert gate.wait(2.0)
        raise KeyboardInterrupt

    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=wait,
                   background_poller=True) == 130
    assert calls


def test_background_poller_is_not_started_outside_openings(tmp_path, monkeypatch):
    def unexpected_clone(self):
        pytest.fail('poller must only run during an opening window')

    monkeypatch.setattr(client_module.Client, 'clone_readonly', unexpected_clone)
    assert execute(School(), tmp_path, background_poller=True) == 130


def instant_poller(error, retry_after, *, server_directed):
    """Deterministic stand-in for a poller whose first query already failed."""

    class Instant:
        def __init__(self, query, *, interval, clock):
            self._snapshot = Snapshot(rows=[], at=clock(), error=error,
                                      retry_after=retry_after, failed_at=clock(),
                                      server_directed=server_directed)
        running = False

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return self._snapshot

    return Instant


class _Clone:
    session = None

    def activities(self):
        return []


def opening_school(now, list_calls):
    """Opening-window school that records when the foreground queries the list."""
    class Tracked(ActivitySchool):
        def request(self, method, url, **kwargs):
            if url.rsplit('/', 1)[-1] == 'queryActivityList.do':
                list_calls.append(now[0])
            return super().request(method, url, **kwargs)

    school = Tracked()
    # capacity_full: an opening window exists but there is no submission to attempt.
    school.activities = [activity(HDZRS=100)]
    school.hdrs = [{'HD_WID': 'native', 'YYRS': 100}]
    return school


def test_background_poller_retry_after_pauses_the_scan_loop(tmp_path, monkeypatch):
    """A server-directed wait seen by the poller must also stop the synchronous fallback."""
    now = [OPEN]
    waits, list_calls = [], []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        if len(list_calls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module, 'ActivityPoller',
                        instant_poller('RemoteError', 120, server_directed=True))
    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: _Clone())
    school = opening_school(now, list_calls)

    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True) == 130

    assert list_calls[0] == OPEN
    assert list_calls[1] >= OPEN + 120
    assert [when for when in list_calls if OPEN < when < OPEN + 120] == []
    assert any(seconds > 60 for seconds in waits)


def test_background_poller_plain_failure_still_falls_back_to_a_query(tmp_path, monkeypatch):
    """Only server-directed waits are shared; other poller errors keep the sync fallback."""
    now = [OPEN]
    waits, list_calls = [], []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        if len(list_calls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module, 'ActivityPoller',
                        instant_poller('RemoteError', 0, server_directed=False))
    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: _Clone())
    school = opening_school(now, list_calls)

    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True) == 130

    assert list_calls[1] < OPEN + 60
    assert all(seconds <= 5 for seconds in waits)


def test_poller_wait_is_rechecked_before_captcha_and_submit(tmp_path, monkeypatch):
    """A wait learned after the scan start must also stop captcha and submission."""
    now = [OPEN]
    waits, requests = [], []
    poller_instances = []

    class FirstFailsThenSucceeds:
        running = False

        def __init__(self, query, *, interval, clock):
            self._failed = not poller_instances
            poller_instances.append(self)
            self._failed_at = now[0]

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            if self._failed:
                return Snapshot(rows=[], at=self._failed_at, error='RemoteError',
                                retry_after=120, failed_at=self._failed_at,
                                server_directed=True)
            return Snapshot(rows=[activity()], at=now[0])

    class Tracked(ActivitySchool):
        def request(self, method, url, **kwargs):
            tail = url.rsplit('/', 1)[-1]
            if tail in ('vcode.do', 'addReservation.do'):
                requests.append((tail, now[0]))
            return super().request(method, url, **kwargs)

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        if any(tail == 'addReservation.do' for tail, _ in requests):
            raise KeyboardInterrupt
        if len(waits) >= 8:
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module, 'ActivityPoller', FirstFailsThenSucceeds)
    monkeypatch.setattr(client_module.Client, 'clone_readonly', lambda self: _Clone())
    school = Tracked()
    school.activities = [activity()]

    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait,
                   background_poller=True) == 130

    early = [request for request in requests if request[1] < OPEN + 120]
    submits = [when for tail, when in requests if tail == 'addReservation.do']
    assert early == []                       # no captcha or submit inside the server wait
    assert submits and submits[0] >= OPEN + 120
