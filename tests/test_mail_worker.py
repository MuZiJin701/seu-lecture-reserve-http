import threading

from seu_reserve.mail import MailWorker


def test_mail_queue_sends_serially_without_blocking_its_owner():
    entered, release = threading.Event(), threading.Event()
    sent = []
    def send(event, details, at):
        entered.set()
        assert release.wait(2)
        sent.append((event, details, at, threading.current_thread().name))
    worker = MailWorker(send, lambda: None)
    worker.submit('authentication_required', 'frozen details', at=123.0)
    assert entered.wait(2)
    worker.submit('reservation_succeeded', 'second', track=False)
    assert not sent
    release.set()
    assert worker.close(2)
    assert [(event, details) for event, details, _, _ in sent] == [
        ('authentication_required', 'frozen details'), ('reservation_succeeded', 'second')]
    assert sent[0][2] == 123.0 and sent[1][2] is None
    assert {name for *_, name in sent} == {'mail-sender'}
    assert [r.status for r in worker.results()] == ['sending', 'accepted', 'sending', 'accepted']


def test_mail_error_is_reported_without_secret_or_automatic_retry():
    calls = []
    def send(event, details, at):
        calls.append(event)
        raise TimeoutError('SECRET SMTP response')
    worker = MailWorker(send, lambda: None)
    worker.submit('query_failure', '')
    assert worker.close(2)
    result = worker.results()[-1]
    assert result.status == 'failed' and result.reason == 'timeout'
    assert 'SECRET' not in repr(result)
    assert calls == ['query_failure']
