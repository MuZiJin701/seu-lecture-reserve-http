import json
import threading

import pytest

from seu_reserve.runtime import CaptchaWorker, LocalLogs, PreparedCaptcha, WorkSignal


def test_publication_between_consumption_and_wait_is_not_lost():
    signal = WorkSignal()
    signal.set('activities')
    signal.clear()
    signal.set('history')
    signal.set('mail')
    assert signal.wait(0)
    assert signal.sources == {'history', 'mail'}
    signal.clear()
    assert not signal.wait(.001)


def test_condition_spurious_notification_does_not_report_new_work():
    signal = WorkSignal()
    result = []
    waiter = threading.Thread(target=lambda: result.append(signal.wait(.05)))
    waiter.start()
    with signal._condition:
        signal._condition.notify_all()
    waiter.join(1)
    assert result == [False]


def test_preparation_publishes_only_after_the_session_is_released():
    release = threading.Event()
    notified = threading.Event()
    def fetch():
        assert release.wait(1)
        return PreparedCaptcha('1234', 10, .1, .01, None, False)
    job = CaptchaWorker(notified.set)
    job.submit(fetch, generation=1)
    try:
        assert not job.ready
        with pytest.raises(RuntimeError, match='captcha_job_pending'):
            job.submit(fetch, generation=2)
        with pytest.raises(RuntimeError, match='captcha_result_unavailable'):
            job.result()
        release.set()
        assert notified.wait(1)
        with pytest.raises(RuntimeError, match='captcha_job_pending'):
            job.submit(fetch, generation=2)
        assert job.result().code == '1234'
        notified.clear()
        first_thread = job._thread
        job.submit(fetch, generation=2)
        assert notified.wait(1)
        assert job.number == 2 and job.generation == 2
        assert job._thread is first_thread
        assert job.result().code == '1234'
    finally:
        release.set()
        job.close()
    with pytest.raises(RuntimeError, match='captcha_worker_closed'):
        job.submit(fetch, generation=3)


def test_preparation_failure_returns_to_the_owner():
    notified = threading.Event()
    def fetch():
        raise ValueError('constructed_failure')
    job = CaptchaWorker(notified.set)
    job.submit(fetch, generation=1)
    try:
        assert notified.wait(1)
        with pytest.raises(ValueError, match='constructed_failure'):
            job.result()
        notified.clear()
        job.submit(lambda: PreparedCaptcha('1234', 10, .1, .01, None, False), generation=2)
        assert notified.wait(1)
        assert job.result().code == '1234'
    finally:
        job.close()


def test_logs_reuse_safe_descriptors_and_flush_complete_concurrent_lines(tmp_path, monkeypatch):
    import seu_reserve.runtime as runtime
    opened = []
    original = runtime.open_nofollow
    def counted(path, flags):
        opened.append(path.name)
        return original(path, flags)
    monkeypatch.setattr(runtime, 'open_nofollow', counted)
    logs = LocalLogs(tmp_path)
    def write(worker):
        for sequence in range(40):
            logs.append('diagnostics.jsonl', json.dumps([worker, sequence]))
    workers = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    rows = [json.loads(line) for line in (tmp_path/'diagnostics.jsonl').read_text().splitlines()]
    assert len(rows) == 160 and len({tuple(row) for row in rows}) == 160
    assert opened == ['diagnostics.jsonl']
    logs.close()
    (tmp_path/'diagnostics.jsonl').unlink()
    logs.append('diagnostics.jsonl', 'late reader callback')
    assert not (tmp_path/'diagnostics.jsonl').exists()


def test_failed_log_open_can_retry_and_does_not_accept_other_paths(tmp_path, monkeypatch):
    import seu_reserve.runtime as runtime
    original = runtime.open_nofollow
    def fail(path, flags):
        raise OSError('constructed_failure')
    logs = LocalLogs(tmp_path)
    monkeypatch.setattr(runtime, 'open_nofollow', fail)
    with pytest.raises(OSError):
        logs.append('events.log', 'first')
    monkeypatch.setattr(runtime, 'open_nofollow', original)
    logs.append('events.log', 'second')
    with pytest.raises(ValueError):
        logs.append('../task.json', 'invalid')
    logs.close()
    assert (tmp_path/'events.log').read_text().splitlines() == ['second']
