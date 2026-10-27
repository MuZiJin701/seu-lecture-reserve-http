import json
import smtplib
import socket
import ssl
from unittest.mock import MagicMock

import pytest
import requests

from seu_reserve.app import Settings, run
from seu_reserve.cli import main, show_status
from seu_reserve.model import moment
from test_app import School, execute, interrupted, lecture
from test_goals import attended


MAIL = {'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'PRIVATE school password',
        'SMTP_USERNAME': 'sender@gmail.com', 'SMTP_PASSWORD': 'PRIVATE mail password'}


@pytest.fixture
def smtp(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr('smtplib.SMTP_SSL', connection)
    return connection.return_value.__enter__.return_value


def mailed_run(school, directory, **options):
    options.setdefault('sleep', interrupted)
    options.setdefault('clock', lambda: 1000)
    options.setdefault('prompt', interrupted)
    return run(Settings(state_dir=directory), session=school, environ=MAIL,
               recognize=lambda _: '1234', **options)


def clear_retry(directory):
    path = directory / 'task.json'
    saved = json.loads(path.read_text())
    saved['retry_at'] = 0
    path.write_text(json.dumps(saved))


def advancing(start, stop_after=5):
    """Advance simulated time until the five-minute query alert is due.

    The bounded short backoff retries every few seconds, so reaching five minutes needs many
    iterations; stop_after is kept for call-site compatibility.
    """
    state = {'now': start}
    waits = []
    def wait(seconds):
        waits.append(seconds)
        state['now'] += seconds
        if state['now'] - start >= 310 or len(waits) >= 300:
            raise KeyboardInterrupt
    return state, waits, wait


def test_booking_and_goal_completion_are_recorded_but_not_mailed(tmp_path, smtp):
    school = School()
    school.mine = attended(7)
    school.activities = [lecture(SFDK='1')]
    assert mailed_run(school, tmp_path, sleep=interrupted, clock=lambda: 1000) == 0
    assert smtp.send_message.call_count == 0
    log = (tmp_path / 'events.log').read_text()
    assert 'event=reservation_succeeded' in log and 'event=goal_completed' in log


def test_no_daily_report_at_twenty_and_no_report_timestamp(tmp_path, smtp):
    school = School()
    school.mine = attended(5)
    now = moment('2026-10-18 20:00:00').timestamp()
    assert mailed_run(school, tmp_path, clock=lambda: now) == 130
    assert smtp.send_message.call_count == 0
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert 'last_report_at' not in saved and saved['health']['last_mail_event'] == 'none'


def test_query_failure_mails_once_after_five_minutes_and_recovery_does_not(tmp_path, smtp):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    school.query_statuses = [500] * 300
    state, waits, wait = advancing(1000.0, 5)
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    assert waits[:4] == [1, 2, 4, 5]
    assert max(waits) <= 5
    assert smtp.send_message.call_count == 1
    assert '持续查询失败' in smtp.send_message.call_args.args[0]['Subject']
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['query_failure_since'] == 1000 and 'query_failure' in saved['alerts']
    clear_retry(tmp_path)
    school.query_statuses = []
    def stop(seconds):
        raise KeyboardInterrupt
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=stop) == 130
    assert smtp.send_message.call_count == 1
    assert 'event=query_recovered' in (tmp_path / 'events.log').read_text()
    recovered = json.loads((tmp_path / 'task.json').read_text())
    assert 'query_failure_since' not in recovered and 'query_failure' not in recovered['alerts']


def test_new_outage_after_recovery_alerts_again_after_five_minutes(tmp_path, smtp):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    school.query_statuses = [500] * 300
    state, waits, wait = advancing(1000.0, 5)
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    clear_retry(tmp_path)
    school.query_statuses = []
    def stop(seconds):
        raise KeyboardInterrupt
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=stop) == 130
    assert smtp.send_message.call_count == 1
    school.query_statuses = [500] * 300
    state, waits, wait = advancing(state['now'], 5)
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    assert max(waits) <= 5 and smtp.send_message.call_count == 2


def test_partial_recovery_does_not_clear_query_alert(tmp_path, smtp):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    school.query_statuses = [500] * 300
    state, waits, wait = advancing(1000.0, 5)
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    assert smtp.send_message.call_count == 1
    clear_retry(tmp_path)
    school.query_statuses = [500]
    def stop_after_one(seconds):
        raise KeyboardInterrupt
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=stop_after_one) == 130
    assert smtp.send_message.call_count == 1
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert 'query_failure_since' in saved and 'query_failure' in saved['alerts']
    clear_retry(tmp_path)
    school.query_statuses = []
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=stop_after_one) == 130
    assert smtp.send_message.call_count == 1
    cleared = json.loads((tmp_path / 'task.json').read_text())
    assert 'query_failure_since' not in cleared and 'query_failure' not in cleared['alerts']


def test_authentication_alert_attempt_survives_restart_even_if_mail_failed(tmp_path):
    school = School(sms=True)
    events = []
    def notify(event):
        events.append(event)
        raise OSError('PRIVATE SMTP failure')
    for _ in range(2):
        assert execute(school, tmp_path, notify=notify, prompt=interrupted) == 130
    assert events == ['authentication_required']


def test_query_alert_attempt_survives_restart_even_if_mail_failed(tmp_path):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    school.query_statuses = [500] * 300
    events = []
    def notify(event):
        events.append(event)
        raise OSError('PRIVATE SMTP failure')
    state, waits, wait = advancing(1000.0, 5)
    assert execute(school, tmp_path, notify=notify, clock=lambda: state['now'], sleep=wait) == 130
    assert events == ['query_failure']
    clear_retry(tmp_path)
    state, waits, wait = advancing(state['now'], 6)
    assert execute(school, tmp_path, notify=notify, clock=lambda: state['now'], sleep=wait) == 130
    assert events == ['query_failure']


def test_pending_query_recovery_does_not_claim_reservation_success(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1}]
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['alerts']['query_failure'] = 999
    saved['query_failure_since'] = 900
    path.write_text(json.dumps(saved))
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == []
    assert 'event=query_recovered' in (tmp_path / 'events.log').read_text()
    assert json.loads(path.read_text())['status'] == 'pending'
    assert school.submitted == ['one']


def test_disabling_mail_does_not_consume_a_new_persistent_alert(tmp_path):
    school = School(sms=True)
    assert execute(school, tmp_path, notify=None) == 130
    assert not json.loads((tmp_path / 'task.json').read_text())['alerts']
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == ['authentication_required']


def test_mail_history_survives_disabled_restart_and_old_unknown_duration(tmp_path, capsys):
    assert execute(School(), tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['health'].update(last_mail_at=900, mail_status='failed', last_mail_status='failed',
                           last_mail_error='tls_failed', last_mail_event='daily_report',
                           last_mail_duration_seconds=1.25)
    path.write_text(json.dumps(saved))
    assert execute(School(), tmp_path, notify=None) == 130
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'mail_status=disabled' in output and 'last_mail_status=failed' in output
    assert 'last_mail_error=tls_failed' in output and 'last_mail_event=daily_report' in output
    assert 'last_mail_duration_seconds=1.25' in output
    saved = json.loads(path.read_text())
    for key in ('last_mail_error', 'last_mail_event', 'last_mail_duration_seconds', 'last_mail_status'):
        saved['health'].pop(key)
    saved['health']['mail_status'] = 'failed'
    path.write_text(json.dumps(saved))
    assert execute(School(), tmp_path, notify=None) == 130
    health = json.loads(path.read_text())['health']
    assert health['last_mail_error'] == 'unknown' and health['last_mail_duration_seconds'] is None


@pytest.mark.parametrize('error, reason', [
    (socket.gaierror('PRIVATE'), 'dns_failed'), (ConnectionRefusedError('PRIVATE'), 'connection_failed'),
    (ssl.SSLEOFError('PRIVATE'), 'tls_failed'), (TimeoutError('PRIVATE'), 'timeout'),
    (smtplib.SMTPAuthenticationError(535, b'PRIVATE'), 'authentication_failed'),
    (smtplib.SMTPSenderRefused(550, b'PRIVATE', 'private@example.org'), 'sender_refused'),
    (smtplib.SMTPRecipientsRefused({'private@example.org': (550, b'PRIVATE')}), 'recipient_refused'),
    (smtplib.SMTPDataError(554, b'PRIVATE'), 'message_rejected'),
    (smtplib.SMTPServerDisconnected('PRIVATE'), 'connection_failed'),
    (smtplib.SMTPException('PRIVATE'), 'smtp_failed'), (OSError('PRIVATE'), 'network_failed'),
    (RuntimeError('PRIVATE'), 'unknown'),
])
def test_test_mail_and_runtime_share_safe_failure_classification(tmp_path, smtp, monkeypatch, capsys, error, reason):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('os.environ', {})
    (tmp_path / '.env').write_text('SMTP_USERNAME=sender@gmail.com\nSMTP_PASSWORD=PRIVATE\n')
    smtp.send_message.side_effect = error
    assert main(['--test-mail']) == 1
    assert not (tmp_path / 'state').exists()
    class FailedRuntime(School):
        def request(self, method, url, **kwargs):
            raise error
    school = FailedRuntime()
    assert mailed_run(school, tmp_path / 'runtime') == 1
    output = capsys.readouterr().out
    assert output.count('event=notification_failed reason=' + reason) == 2
    assert 'PRIVATE' not in output and 'private@example.org' not in output
    saved = json.loads((tmp_path / 'runtime' / 'task.json').read_text())['health']
    assert saved['last_mail_error'] == reason


def test_alert_mail_uses_current_confirmed_progress_without_details(tmp_path, smtp):
    school = School()
    school.mine = attended(3)
    school.mine[0]['HDMC'] = 'PRIVATE lecture title'
    school.query_statuses = [500] * 300
    state, waits, wait = advancing(1000.0, 5)
    assert mailed_run(school, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    assert smtp.send_message.call_count == 1
    body = smtp.send_message.call_args.args[0].get_content()
    assert '已选听：3 / 8' in body and '待参加：0' in body
    assert 'PRIVATE' not in body
    assert 'PRIVATE' not in (tmp_path / 'events.log').read_text()


def test_alert_unknown_progress_is_not_zero_and_pending_never_submits(tmp_path, smtp):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    clear_retry(tmp_path)
    class UnavailableHistory(School):
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do'):
                raise requests.Timeout('PRIVATE')
            return super().request(method, url, **kwargs)
    failed = UnavailableHistory()
    state, waits, wait = advancing(5000.0, 5)
    assert mailed_run(failed, tmp_path, clock=lambda: state['now'], sleep=wait) == 130
    assert smtp.send_message.call_count == 1
    body = smtp.send_message.call_args.args[0].get_content()
    assert '已选听：未知' in body and '待参加：未知' in body
    assert '预约结果待确认' in body
    assert not failed.submitted


def test_disabled_mail_read_only_and_completed_tasks_do_not_mail(tmp_path, smtp):
    now = lambda: moment('2026-10-18 21:00:00').timestamp()
    assert run(Settings(state_dir=tmp_path / 'disabled'), session=School(),
               environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'},
               clock=now, sleep=interrupted) == 130
    assert mailed_run(School(), tmp_path / 'check', check_only=True,
                      notify=lambda _: pytest.fail('mail in check')) == 0
    assert not (tmp_path / 'check').exists()
    school = School()
    school.mine = attended()
    for _ in range(2):
        assert mailed_run(school, tmp_path / 'completed') == 0
    assert smtp.send_message.call_count == 0
    saved = json.loads((tmp_path / 'completed' / 'task.json').read_text())
    assert 'last_report_at' not in saved and saved['health']['last_mail_event'] == 'none'


