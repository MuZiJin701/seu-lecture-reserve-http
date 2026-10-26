import json

import pytest
import requests

from seu_reserve.state import task_running
from test_app import School, execute, lecture


@pytest.mark.parametrize('code', [None, False, True, '1'])
@pytest.mark.parametrize('message', ['验证码错误', '名额已满'])
def test_malformed_submission_reply_is_confirmed_before_any_resubmit(tmp_path, code, message):
    school = School()
    school.activities = [lecture(), lecture('two')]
    reply = {'msg': message} if code is None else {'code': code, 'msg': message}
    school.submit_replies = [reply] * 8
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one']
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'pending' and saved['activity'] == 'one'


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('field, value', [
    ('last_mail_at', -1), ('last_mail_status', 'PRIVATE unknown'),
    ('last_mail_error', 'PRIVATE unknown'), ('last_mail_event', 'PRIVATE unknown'),
    ('last_mail_duration_seconds', -1),
])
def test_invalid_mail_history_is_rejected_without_overwriting_pending_task(tmp_path, field, value, version):
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1}]
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['version'] = version
    saved['health'][field] = value
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    school.requests.clear()
    assert execute(school, tmp_path) == 1
    assert path.read_bytes() == before
    assert not school.requests and not task_running(tmp_path)


@pytest.mark.parametrize('message', ['人数已满', '该活动不可预约'])
def test_unavailable_message_variants_do_not_stay_pending(tmp_path, message):
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1, 'msg': message}]
    assert execute(school, tmp_path) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning' and not saved['activity']


def test_pending_timeout_waits_for_history_before_claiming_success(tmp_path):
    class RecoveringSchool(School):
        fail_history = True
        def request(self, method, url, **kwargs):
            if self.fail_history and self.submitted and url.endswith('queryMyActivityList.do'):
                raise requests.Timeout('PRIVATE query failure')
            return super().request(method, url, **kwargs)
    school = RecoveringSchool()
    school.activities = [lecture()]
    school.query_statuses = [500] * 3
    school.submit_timeout = True
    events, waits = [], []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=wait, clock=lambda: 1000) == 130
    assert events == []
    assert school.submitted == ['one']
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'
    school.fail_history = False
    school.submit_timeout = False
    waits.clear()
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, notify=events.append, sleep=stop, clock=lambda: 5000) == 130
    assert events == []
    log = (tmp_path / 'events.log').read_text()
    assert all(event in log for event in (
        'event=reservation_unconfirmed', 'event=reservation_unconfirmed_released',
        'event=reservation_succeeded'))
    assert school.submitted == ['one', 'one']


@pytest.mark.parametrize('field, value', [('retry_at', -1), ('retry_at', 1e20),
                                       ('cooldowns', {'one': -1}), ('cooldowns', {'one': 1e20})])
def test_invalid_recovery_deadlines_are_not_used_or_overwritten(tmp_path, field, value):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved[field] = value
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    school.requests.clear()
    assert execute(school, tmp_path) == 1
    assert path.read_bytes() == before
    assert not school.requests and not task_running(tmp_path)


@pytest.mark.parametrize('header', ['9' * 400, '9' * 30, 'Sun, 06 Nov 1994 08:49:37 GMT'])
def test_unusable_retry_after_cannot_persist_an_unloadable_task(tmp_path, header):
    class ThrottledSchool(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('queryActivityList.do'):
                response.status_code = 429
                response.headers['Retry-After'] = header
            return response
    assert execute(ThrottledSchool(), tmp_path, clock=lambda: 1000) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['retry_at'] == 1020
    assert saved['health']['last_error'] == 'server_backoff'


@pytest.mark.parametrize('field', ['updated_at', 'last_mail_at'])
def test_oversized_snapshot_time_is_unknown_without_crashing(tmp_path, capsys, field):
    from seu_reserve.cli import show_status
    assert execute(School(), tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved['health'][field] = 10 ** 400
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    capsys.readouterr()
    show_status(tmp_path)
    assert 'phase=unknown' in capsys.readouterr().out
    assert path.read_bytes() == before
