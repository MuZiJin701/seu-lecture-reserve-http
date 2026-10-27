import threading
import time

from seu_reserve.poller import ActivityPoller


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
    assert snapshot.rows == []


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
