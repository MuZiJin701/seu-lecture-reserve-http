import threading
import time

import pytest

from seu_reserve.client import AuthenticationExpired, RemoteError
from seu_reserve.poller import ActivityPoller, WaitGate


def test_dynamic_poll_interval_is_resampled_and_includes_request_time():
    now = [0.0]
    starts, waits = [], []
    intervals = iter([.1, .75, .05])

    def query():
        starts.append(now[0])
        now[0] += .15 if len(starts) == 1 else .2
        if len(starts) == 3:
            poller._stop.set()
        return []

    class Wake:
        def wait(self, seconds):
            waits.append(seconds)
            now[0] += seconds
            return False

    poller = ActivityPoller(query, interval=lambda: next(intervals), clock=lambda: now[0])
    poller._wake = Wake()
    poller._run()
    assert starts == pytest.approx([0, .15, .9])
    assert waits == pytest.approx([.55])


def test_poll_once_records_error_and_retry_after_without_raising():
    class Throttled(Exception):
        retry_after = 7.5

    def query():
        raise Throttled('server directed')

    poller = ActivityPoller(query, interval=0.25, clock=lambda: 55.0)
    assert poller.poll_once() is False
    snapshot = poller.snapshot()
    assert snapshot is not None
    assert snapshot.error == 'Throttled'
    assert snapshot.retry_after == 7.5
    assert snapshot.failed_at == 55.0
    assert snapshot.server_directed is True
    assert snapshot.rows == []


def test_scan_keeps_success_version_until_authentication_expires():
    attempts = iter([None, RemoteError('network_timeout'),
                     AuthenticationExpired('business_authentication_expired'),
                     RemoteError('network_timeout')])

    def query():
        error = next(attempts)
        if error is not None:
            raise error
        return []

    poller = ActivityPoller(query, interval=.25)
    assert poller.poll_once()
    success = poller.snapshot_with_version()
    assert not poller.poll_once()
    assert poller.snapshot_for_scan() == success
    latest, version = poller.snapshot_with_version()
    assert latest is not None and latest.error and version == 2
    assert not poller.poll_once()
    assert not poller.poll_once()
    assert poller.snapshot_for_scan() == poller.snapshot_with_version()


def test_rate_limited_failure_without_retry_after_is_server_directed():
    class Limited(Exception):
        http_status = 429

    def query():
        raise Limited('slow down')

    poller = ActivityPoller(query, interval=0.25, clock=lambda: 55.0)
    assert poller.poll_once() is False
    snapshot = poller.snapshot()
    assert snapshot is not None
    assert snapshot.retry_after == 0.0
    assert snapshot.server_directed is True


@pytest.mark.parametrize('error, reason', [
    (RemoteError('network_timeout'), 'network_timeout'),
    (RemoteError('business_authentication_expired'), 'business_authentication_expired'),
    # Only a fixed lowercase code may be copied; anything else stays a class name so response
    # or exception text can never leak into the local diagnostics.
    (RemoteError('PRIVATE server text'), 'RemoteError'),
    (ValueError('boom'), 'ValueError'),
])
def test_error_snapshot_records_a_fixed_log_safe_reason(error, reason):
    def query():
        raise error

    poller = ActivityPoller(query, interval=0.25, clock=lambda: 55.0)
    assert poller.poll_once() is False
    snapshot = poller.snapshot()
    assert snapshot is not None
    assert snapshot.error == type(error).__name__
    assert snapshot.reason == reason


def test_thread_polls_until_stopped():
    called = threading.Event()
    calls = []

    def query():
        calls.append('one')
        called.set()
        return []

    poller = ActivityPoller(query, interval=0.01, clock=time.monotonic)
    poller.start()
    assert called.wait(2.0)
    poller.stop()
    assert not poller.running
    assert calls
    assert poller.snapshot() is not None and poller.snapshot().error is None


def test_snapshot_with_version_returns_a_consistent_pair():
    counter = [0]

    def query():
        counter[0] += 1
        return [{'v': counter[0]}]

    poller = ActivityPoller(query, interval=0.01, clock=time.monotonic)
    poller.start()
    pairs = []
    try:
        deadline = time.monotonic() + 1.0
        while len(pairs) < 50 and time.monotonic() < deadline:
            snapshot, version = poller.snapshot_with_version()
            if snapshot is not None:
                pairs.append((snapshot.rows[0]['v'], version))
            time.sleep(0.001)
    finally:
        poller.stop()
    assert len(pairs) >= 10
    # Rows from poll N and version N are published under the same lock, so a reader can never
    # mark a version consumed while holding older rows.
    assert all(row == version for row, version in pairs)


def test_wait_gate_stops_the_sibling_thread_before_its_next_request():
    first_started = threading.Event()
    sibling_calls = []

    class Limited(Exception):
        retry_after = 120.0
        http_status = 429

    def limited():
        first_started.set()
        raise Limited('slow down')

    gate = WaitGate(time.monotonic)
    limited_poller = ActivityPoller(limited, interval=0.01, clock=time.monotonic, gate=gate)
    limited_poller.start()
    assert first_started.wait(2.0)
    deadline = time.monotonic() + 2.0
    while gate.remaining() <= 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert gate.remaining() > 100.0

    sibling = ActivityPoller(lambda: sibling_calls.append('one') or [], interval=0.01,
                             clock=time.monotonic, gate=gate)
    sibling.start()
    time.sleep(0.1)
    try:
        assert not sibling.running
        assert sibling_calls == []
    finally:
        limited_poller.stop()
        sibling.stop()


def test_request_refresh_polls_immediately_instead_of_waiting_for_the_interval():
    polls = []
    poller = ActivityPoller(lambda: polls.append(time.monotonic()) or [], interval=10.0,
                            clock=time.monotonic)
    poller.start()
    try:
        deadline = time.monotonic() + 2.0
        while len(polls) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(polls) == 1
        poller.request_refresh()
        deadline = time.monotonic() + 2.0
        while len(polls) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(polls) >= 2
    finally:
        poller.stop()


def test_stop_without_join_does_not_block_on_a_slow_request_and_closes_on_exit():
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def query():
        started.set()
        release.wait(3.0)
        return []

    poller = ActivityPoller(query, interval=0.01, clock=time.monotonic,
                            on_exit=closed.set)
    poller.start()
    assert started.wait(2.0)
    before = time.monotonic()
    poller.stop(join=False)
    assert time.monotonic() - before < 0.2
    assert not closed.is_set()
    release.set()
    assert closed.wait(2.0)


def test_refresh_request_during_an_in_flight_query_is_coalesced():
    calls = []
    started = threading.Event()
    release = threading.Event()

    def query():
        calls.append(time.monotonic())
        started.set()
        release.wait(3.0)
        return []

    poller = ActivityPoller(query, interval=10.0, clock=time.monotonic)
    poller.start()
    assert started.wait(2.0)
    poller.request_refresh()   # the in-flight query should satisfy this request
    release.set()
    time.sleep(0.2)
    try:
        assert len(calls) == 1
    finally:
        poller.stop()

def test_stop_during_in_flight_refresh_query_exits_without_the_interval_wait():
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    calls = []

    def query():
        calls.append(time.monotonic())
        started.set()
        release.wait(3.0)
        return []

    poller = ActivityPoller(query, interval=20.0, clock=time.monotonic,
                            on_exit=exited.set)
    poller.start()
    assert started.wait(2.0)
    poller.request_refresh()
    poller.stop(join=False)
    release.set()
    # The refresh clear must not swallow the stop wake: without the stop re-check this would
    # wait the full 20 s interval before the worker closes its session.
    assert exited.wait(2.0)
    assert len(calls) == 1
def test_refresh_cannot_shorten_a_reader_error_backoff():
    from seu_reserve.client import RemoteError
    updated, recovered = threading.Event(), threading.Event()
    starts = []
    def query():
        starts.append(time.monotonic())
        if len(starts) == 1:
            raise RemoteError('service_unavailable', http_status=503)
        recovered.set()
        return []
    reader = ActivityPoller(query, interval=60, on_update=updated.set,
                            retry_delay=lambda count, snapshot: .2)
    reader.start()
    try:
        assert updated.wait(2)
        reader.request_refresh()
        assert recovered.wait(2)
    finally:
        reader.stop()
    assert starts[1] - starts[0] >= .18
def test_policy_reschedule_recomputes_wait_without_an_immediate_extra_query():
    import threading
    import time
    from seu_reserve.poller import ActivityPoller
    starts = []
    interval = [1.0]
    first = threading.Event()
    def query():
        starts.append(time.monotonic())
        first.set()
        return []
    reader = ActivityPoller(query, interval=lambda: interval[0])
    reader.start()
    try:
        assert first.wait(1)
        interval[0] = .15
        reader.reschedule()
        time.sleep(.04)
        assert len(starts) == 1
        until = time.monotonic()+.7
        while len(starts)<2 and time.monotonic()<until:
            time.sleep(.005)
        assert len(starts)>=2
        assert .12 <= starts[1]-starts[0] < .7
    finally:
        reader.stop()

def test_merged_refresh_is_consumed_before_the_failure_cap_wait():
    """One refresh merged into an in-flight failing query must not replay a failure round."""
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def query():
        calls.append(threading.current_thread().name)
        entered.set()
        release.wait(2)
        raise RemoteError('service_unavailable', http_status=503)

    reader = ActivityPoller(query, interval=None,
                            retry_delay=lambda count, snapshot: .01,
                            max_consecutive_failures=3)
    reader.start()
    try:
        assert entered.wait(2)
        assert reader.request_refresh() == 1
        release.set()
        until = time.monotonic() + 1.5
        while len(calls) < 3 and time.monotonic() < until:
            time.sleep(.005)
        # A stale wake from the already-satisfied merged request would immediately start a
        # second three-failure round here.
        time.sleep(.15)
        assert len(calls) == 3
        # A genuinely new refresh event still gets exactly one bounded round.
        reader.request_refresh()
        until = time.monotonic() + 1.5
        while len(calls) < 6 and time.monotonic() < until:
            time.sleep(.005)
        time.sleep(.15)
        assert len(calls) == 6
    finally:
        release.set()
        reader.stop()


def test_event_driven_reader_ignores_policy_reschedule_until_a_refresh_event():
    """A phase change updates the budget but must not query an event-driven owner by itself."""
    calls = []
    first = threading.Event()

    def query():
        calls.append(time.monotonic())
        first.set()
        return []

    reader = ActivityPoller(query, interval=None)
    reader.start()
    try:
        assert first.wait(1)
        reader.reschedule()
        time.sleep(.15)
        assert len(calls) == 1
        reader.request_refresh()
        until = time.monotonic() + .7
        while len(calls) < 2 and time.monotonic() < until:
            time.sleep(.005)
        assert len(calls) == 2
        time.sleep(.1)
        assert len(calls) == 2
    finally:
        reader.stop()
