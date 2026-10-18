import json
import smtplib
import socket
import ssl
from unittest.mock import MagicMock

import pytest

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
    options.setdefault('clock', lambda: moment('2026-10-18 20:00:00').timestamp())
    options.setdefault('prompt', interrupted)
    return run(Settings(state_dir=directory), session=school, environ=MAIL,
               recognize=lambda _: '1234', **options)


@pytest.mark.parametrize('time, expected', [('19:59:59', 0), ('20:00:00', 1), ('23:59:59', 1)])
def test_daily_report_uses_beijing_time_and_current_progress(tmp_path, smtp, time, expected):
    school = School()
    school.mine = attended(5)
    assert mailed_run(school, tmp_path, clock=lambda: moment('2026-10-18 ' + time).timestamp()) == 130
    assert smtp.send_message.call_count == expected
    if expected:
        message = smtp.send_message.call_args.args[0]
        assert '每日进度汇报' in message['Subject']
        body = message.get_content()
        assert all(value in body for value in ('已选听：5 / 8', '待参加：0', '2026-10-18T', '+08:00'))
        assert 'PRIVATE' not in body
        assert not any(row['HDMC'] in body for row in school.mine)


@pytest.mark.parametrize('backoff', [False, True])
def test_report_wakes_at_twenty_without_early_school_query(tmp_path, smtp, backoff):
    school = School()
    school.query_statuses = [500, 200] if backoff else []
    now = moment('2026-10-18 19:59:55').timestamp()
    waits, reads = [], []
    def wait(seconds):
        nonlocal now
        waits.append(seconds)
        reads.append(sum(url.endswith('queryActivityList.do') for _, url, _ in school.requests))
        now += seconds
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert mailed_run(school, tmp_path, clock=lambda: now, sleep=wait) == 130
    assert waits[:2] == [5, 15 if backoff else 5]
    assert reads == [1, 1, 2]
    assert smtp.send_message.call_count == 1
    body = smtp.send_message.call_args.args[0].get_content()
    assert ('http_failure' in body) == backoff


@pytest.mark.parametrize('fails', [False, True])
def test_report_attempt_is_saved_before_send_and_deduplicated_across_restart_and_days(
        tmp_path, smtp, capsys, fails):
    attempts = []
    def send(message):
        saved = json.loads((tmp_path / 'task.json').read_text())
        attempts.append(saved['last_report_at'])
        if fails:
            raise TimeoutError('PRIVATE SMTP response')
    smtp.send_message.side_effect = send
    school = School()
    day = moment('2026-10-18 22:00:00').timestamp()
    for timestamp in (day, day + 3600, day + 86400):
        assert mailed_run(school, tmp_path, clock=lambda: timestamp) == 130
    assert attempts == [day, day + 86400]
    saved = json.loads((tmp_path / 'task.json').read_text())['health']
    assert saved['last_mail_event'] == 'daily_report'
    assert saved['last_mail_duration_seconds'] >= 0
    assert saved['last_mail_error'] == ('timeout' if fails else 'none')
    assert 'PRIVATE' not in capsys.readouterr().out


def test_daily_report_reuses_plan_progress_without_fetching_extra_lectures(tmp_path, smtp):
    school = School()
    school.mine = attended(5) + [lecture(f'planned-{i}', YYZT='已预约',
        HDLX_DISPLAY='人文与科学素养系列讲座_其他',
        KSSJ=f'2030-01-0{i + 1} 10:00:00', JSSJ=f'2030-01-0{i + 1} 11:00:00') for i in range(3)]
    assert mailed_run(school, tmp_path) == 130
    body = smtp.send_message.call_args.args[0].get_content()
    assert '待参加：3' in body and '已选听：5 / 8' in body
    assert not any(url.endswith('queryActivityList.do') for _, url, _ in school.requests)


def test_report_unknown_progress_is_not_zero_and_pending_never_submits(tmp_path, smtp):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    import requests
    class UnavailableHistory(School):
        def request(self, method, url, **kwargs):
            if url.endswith('queryMyActivityList.do'):
                raise requests.Timeout('PRIVATE')
            return super().request(method, url, **kwargs)
    failed = UnavailableHistory()
    assert mailed_run(failed, tmp_path) == 130
    body = smtp.send_message.call_args.args[0].get_content()
    assert '已选听：未知' in body and '待参加：未知' in body
    assert '预约结果待确认' in body
    assert not failed.submitted


def test_disabled_mail_read_only_and_completed_tasks_do_not_schedule_reports(tmp_path, smtp):
    now = lambda: moment('2026-10-18 21:00:00').timestamp()
    assert run(Settings(state_dir=tmp_path / 'disabled'), session=School(),
               environ={'SEU_USERNAME': 'test-user', 'SEU_PASSWORD': 'test-password'}, clock=now, sleep=interrupted) == 130
    assert mailed_run(School(), tmp_path / 'check', check_only=True, notify=lambda _: pytest.fail('mail in check')) == 0
    assert not (tmp_path / 'check').exists()
    school = School()
    school.mine = attended()
    for _ in range(2):
        assert mailed_run(school, tmp_path / 'completed') == 0
    assert smtp.send_message.call_count == 1
    assert '选听目标已完成' in smtp.send_message.call_args.args[0]['Subject']
    assert 'last_report_at' not in json.loads((tmp_path / 'completed' / 'task.json').read_text())


@pytest.mark.parametrize('problem', ['authentication', 'pending', 'query'])
def test_unresolved_alert_attempt_survives_restart_even_if_mail_failed(tmp_path, problem):
    school = School(sms=problem == 'authentication')
    if problem == 'pending':
        school.activities = [lecture()]
        school.submit_replies = [{'code': -1}]
    events = []
    def notify(event):
        events.append(event)
        raise OSError('PRIVATE SMTP failure')
    for attempt in range(2):
        school.query_statuses = [500] * 3 if problem == 'query' else []
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise KeyboardInterrupt
        assert execute(school, tmp_path, sleep=wait, notify=notify, clock=lambda: 1000 + attempt * 1000) == 130
    assert events == [{'authentication': 'authentication_required',
                       'pending': 'reservation_unconfirmed', 'query': 'query_failure'}[problem]]
    if problem == 'pending':
        assert school.submitted == ['one']


def test_recovery_after_restart_and_next_outage_waits_for_alert_interval(tmp_path):
    school = School()
    school.query_statuses = [500] * 3
    events, waits = [], []
    now = 1000.0
    def wait(seconds):
        nonlocal now
        waits.append(seconds)
        now += seconds
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, clock=lambda: now, sleep=wait) == 130
    last_alert = json.loads((tmp_path / 'task.json').read_text())['last_query_alert_at']
    school.query_statuses = [200] + [500] * 20
    waits.clear()
    def stop_at_new_alert(seconds):
        nonlocal now
        now += seconds
        if events.count('query_failure') == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, clock=lambda: now, sleep=stop_at_new_alert) == 130
    assert events == ['query_failure', 'query_recovered', 'query_failure']
    assert json.loads((tmp_path / 'task.json').read_text())['last_query_alert_at'] - last_alert >= 900


def test_partial_recovery_does_not_clear_query_alert(tmp_path):
    school = School()
    events, waits = [], []
    school.query_statuses = [500] * 3
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=stop, clock=lambda: 1000) == 130
    # Authentication and history succeed; the lecture list still fails.
    school.query_statuses = [500]
    assert execute(school, tmp_path, notify=events.append, clock=lambda: 1500) == 130
    assert events == ['query_failure']
    school.query_statuses = []
    assert execute(school, tmp_path, notify=events.append, clock=lambda: 1600) == 130
    assert events == ['query_failure', 'query_recovered']


def test_pending_query_recovery_does_not_claim_reservation_success(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1}]
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['alerts']['query_failure'] = 999
    path.write_text(json.dumps(saved))
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == ['query_recovered']
    assert json.loads(path.read_text())['status'] == 'pending'
    assert school.submitted == ['one']


def test_disabling_mail_does_not_consume_a_new_persistent_alert(tmp_path):
    school = School(sms=True)
    assert execute(school, tmp_path, notify=None) == 130
    assert not json.loads((tmp_path / 'task.json').read_text())['alerts']
    events = []
    assert execute(school, tmp_path, notify=events.append) == 130
    assert events == ['authentication_required']


def test_mail_history_survives_disabled_restart_and_old_unknown_duration(tmp_path, smtp, capsys):
    smtp.send_message.side_effect = ssl.SSLEOFError('PRIVATE')
    assert mailed_run(School(), tmp_path) == 130
    path = tmp_path / 'task.json'
    before = json.loads(path.read_text())['health']
    assert execute(School(), tmp_path, notify=None) == 130
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'mail_status=disabled' in output and 'last_mail_status=failed' in output
    assert 'last_mail_error=tls_failed' in output and 'last_mail_event=daily_report' in output
    assert 'last_mail_duration_seconds=' + str(before['last_mail_duration_seconds']) in output
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
    assert mailed_run(School(), tmp_path / 'runtime') == 130
    output = capsys.readouterr().out
    assert output.count('event=notification_failed reason=' + reason) == 2
    assert 'PRIVATE' not in output and 'private@example.org' not in output
    saved = json.loads((tmp_path / 'runtime' / 'task.json').read_text())['health']
    assert saved['last_mail_error'] == reason


def test_success_mail_uses_current_confirmed_progress_without_details(tmp_path, smtp):
    school = School()
    school.mine = attended(3)
    school.activities = [lecture(HDMC='PRIVATE lecture title')]
    assert mailed_run(school, tmp_path, clock=lambda: 1000) == 130
    body = smtp.send_message.call_args.args[0].get_content()
    assert '预约成功' in body and '已选听：3 / 8' in body and '待参加：1' in body
    assert 'PRIVATE' not in body
    assert 'PRIVATE' not in (tmp_path / 'events.log').read_text()
