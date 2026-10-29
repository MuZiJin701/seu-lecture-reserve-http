import threading
import time

from seu_reserve.poller import ActivityPoller, WaitGate


def test_poll_once_stores_rows_and_timestamp():
    clock = [100.0]
    poller = ActivityPoller(lambda: [{'WID': 'one'}], interval=0.25, clock=lambda: clock[0])
    assert poller.poll_once() is True
    snapshot = poller.snapshot()
    assert snapshot is not None
    assert snapshot.rows == [{'WID': 'one'}]
    assert snapshot.at == 100.0
    assert snapshot.error is None
    assert not poller.running


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


def test_plain_failure_is_not_marked_server_directed():
    class Broken(Exception):
        pass

    def query():
        raise Broken('boom')

    poller = ActivityPoller(query, interval=0.25, clock=lambda: 55.0)
    assert poller.poll_once() is False
    snapshot = poller.snapshot()
    assert snapshot is not None
    assert snapshot.error == 'Broken'
    assert snapshot.failed_at == 55.0
    assert snapshot.server_directed is False


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


def _started_poller(starts, *, interval):
    """Poller whose query records its start time on the real clock."""

    def query():
        starts.append(time.monotonic())
        return []

    return ActivityPoller(query, interval=interval, clock=time.monotonic)


def _wait_for_polls(poller, count, timeout=3.0):
    """Wait until ``count`` queries have completed; returns the completed count."""
    deadline = time.monotonic() + timeout
    while poller.polls < count and time.monotonic() < deadline:
        poller.wait_update(0.25)
    return poller.polls


def test_polls_counts_completed_queries():
    class Broken(Exception):
        pass

    calls = []

    def query():
        calls.append('one')
        if len(calls) == 2:
            raise Broken('boom')
        return [{'WID': 'one'}]

    poller = ActivityPoller(query, interval=0.25, clock=lambda: 100.0)
    assert poller.polls == 0
    assert poller.poll_once() is True
    assert poller.polls == 1
    assert poller.poll_once() is False
    assert poller.polls == 2


def test_wait_update_wakes_when_a_poll_completes():
    poller = ActivityPoller(lambda: [], interval=0.01, clock=time.monotonic)
    poller.start()
    try:
        assert poller.wait_update(2.0) is True
    finally:
        poller.stop()
    assert poller.polls >= 1


def test_wait_update_wakes_on_a_failed_poll():
    class Broken(Exception):
        pass

    gate = threading.Event()

    def query():
        gate.wait(3.0)
        raise Broken('boom')

    poller = ActivityPoller(query, interval=0.01, clock=time.monotonic)
    poller.start()
    timer = threading.Timer(0.1, gate.set)
    timer.start()
    try:
        assert poller.wait_update(5.0) is True
    finally:
        timer.cancel()
        poller.stop()
    assert poller.polls == 1
    snapshot = poller.snapshot()
    assert snapshot is not None and snapshot.error == 'Broken'


def test_wait_update_times_out_while_idle():
    poller = ActivityPoller(lambda: [], interval=0.25, clock=time.monotonic)
    started = time.monotonic()
    assert poller.wait_update(0.2) is False
    assert time.monotonic() - started >= 0.15


def test_align_at_places_the_next_poll_near_the_target():
    starts = []
    poller = _started_poller(starts, interval=0.5)
    target = time.monotonic() + 0.15
    poller.align_at(target)
    poller.start()
    try:
        assert _wait_for_polls(poller, 2) >= 2
    finally:
        poller.stop()
    # The first poll runs immediately; the one-shot alignment puts the next one on target.
    assert len(starts) >= 2
    assert target - 0.05 <= starts[1] <= target + 0.3


def test_align_at_is_one_shot():
    starts = []
    poller = _started_poller(starts, interval=0.4)
    poller.align_at(time.monotonic() + 0.15)
    poller.start()
    try:
        assert _wait_for_polls(poller, 3) >= 3
    finally:
        poller.stop()
    # Only the first wait was pulled to the target; the poll after it is regular again.
    assert starts[2] - starts[1] >= 0.3


def test_align_at_ignores_a_target_beyond_two_intervals():
    starts = []
    poller = _started_poller(starts, interval=0.3)
    poller.align_at(time.monotonic() + 2.0)
    poller.start()
    try:
        assert _wait_for_polls(poller, 2) >= 2
    finally:
        poller.stop()
    assert 0.2 <= starts[1] - starts[0] <= 0.6


def test_align_at_none_cancels_a_pending_target():
    starts = []
    poller = _started_poller(starts, interval=0.3)
    poller.align_at(time.monotonic() + 0.15)
    poller.align_at(None)
    poller.start()
    try:
        assert _wait_for_polls(poller, 2) >= 2
    finally:
        poller.stop()
    assert starts[1] - starts[0] >= 0.2


def test_wait_update_reports_a_poll_completed_before_the_wait_started():
    poller = ActivityPoller(lambda: [], interval=10.0, clock=time.monotonic)
    assert poller.poll_once() is True
    started = time.monotonic()
    assert poller.wait_update(0.5, since=0) is True
    # The finished poll must be reported immediately, not after the timeout.
    assert time.monotonic() - started < 0.2


def test_wait_update_ignores_a_poll_the_caller_already_consumed():
    poller = ActivityPoller(lambda: [], interval=10.0, clock=time.monotonic)
    assert poller.poll_once() is True
    assert poller.wait_update(0.05, since=poller.polls) is False


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

def test_wait_gate_is_updated_by_the_limited_thread_immediately():
    class Limited(Exception):
        retry_after = 120.0
        http_status = 429

    gate = WaitGate(time.monotonic)
    poller = ActivityPoller(lambda: (_ for _ in ()).throw(Limited()), interval=0.01,
                            clock=time.monotonic, gate=gate)
    started = time.monotonic()
    assert poller.poll_once() is False
    assert gate.remaining() > 100.0
    assert time.monotonic() - started < 1.0


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


def test_rate_limit_without_retry_after_also_updates_the_gate():
    class Limited(Exception):
        http_status = 429

    gate = WaitGate(lambda: 50.0)
    poller = ActivityPoller(lambda: (_ for _ in ()).throw(Limited()), interval=0.01,
                            clock=lambda: 50.0, gate=gate)
    assert poller.poll_once() is False
    assert gate.remaining() > 0.5

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


def test_exit_callback_runs_once_for_a_finished_thread():
    exits = []
    poller = ActivityPoller(lambda: [], interval=10.0, clock=time.monotonic,
                            on_exit=lambda: exits.append('closed'))
    poller.start()
    try:
        assert poller.wait_update(2.0, since=0) is True
    finally:
        poller.stop()
    assert exits == ['closed']
