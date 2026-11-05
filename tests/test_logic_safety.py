import json

from test_app import School, execute, lecture


def test_non_ascii_ocr_is_retried_without_submitting(tmp_path):
    school = School()
    school.activities = [lecture()]
    assert execute(school, tmp_path, recognize=lambda _: '中文验证码') == 130
    assert school.submitted == []
    assert not json.loads((tmp_path / 'task.json').read_text())['cooldowns']


def test_duplicate_candidates_do_not_bypass_the_per_scan_ocr_retry_limit(tmp_path):
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
