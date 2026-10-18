import json

import pytest

from test_app import School, execute, lecture


@pytest.mark.parametrize('reply', [{}, {'code': -1, 'msg': 'PRIVATE unknown error'}, {'code': '0'}])
def test_unknown_preflight_response_alerts_instead_of_silent_wait(tmp_path, reply):
    class UnknownPreflight(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('appiontCheck.do'):
                response._content = json.dumps(reply).encode()
            return response
    school = UnknownPreflight()
    school.activities = [lecture()]
    events, waits = [], []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, notify=events.append) == 130
    assert not school.submitted
    assert events == ['query_failure']
    assert waits == [20, 40, 80]


def test_non_ascii_ocr_is_retried_without_submitting(tmp_path):
    school = School()
    school.activities = [lecture()]
    assert execute(school, tmp_path, recognize=lambda _: '中文验证码') == 130
    assert school.submitted == []
    assert 'one' in json.loads((tmp_path / 'task.json').read_text())['cooldowns']


def test_duplicate_candidates_do_not_bypass_ocr_retry_limit_and_cooldown(tmp_path):
    school = School()
    school.activities = [lecture(), lecture()]
    school.submit_replies = [{'code': 1, 'msg': '验证码错误'}] * 8
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['one'] * 4


def test_conflicting_duplicate_candidates_pause_before_any_submission(tmp_path):
    school = School()
    school.activities = [lecture(), lecture(HDMC='different title')]
    assert execute(school, tmp_path) == 130
    assert not school.submitted
    assert json.loads((tmp_path / 'task.json').read_text())['health']['last_error'] == 'activity_schema_unknown'


def test_explicit_preflight_unavailability_still_allows_next_candidate(tmp_path):
    class FullLecture(School):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if url.endswith('appiontCheck.do') and kwargs['params']['wid'] == 'one':
                response._content = json.dumps({'code': 1, 'msg': '名额已满'}).encode()
            return response
    school = FullLecture()
    school.activities = [lecture(), lecture('two')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['two']
