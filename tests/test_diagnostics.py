import json

import pytest

from seu_reserve.app import Settings
from seu_reserve.cli import show_status
from seu_reserve.model import SERIES_CATEGORIES
from test_activities import ActivitySchool, CLOSE, OPEN, activity
from test_app import School, execute, lecture
from test_goals import attended


def details(output):
    return json.loads(next(line.split('=', 1)[1] for line in output.splitlines()
                           if line.startswith('activity_details=')))


@pytest.mark.parametrize('excluded', [
    {'JZXL_DISPLAY': '通知公告'},
    {'JZXL_DISPLAY': SERIES_CATEGORIES[1]},
    {'HDLX_DISPLAY': '通知公告', 'HDMC': 42, 'YYKSSJ': 'invalid'},
])
@pytest.mark.parametrize('check_only', [False, True])
def test_excluded_category_missing_fields_does_not_block_target_or_status(
        tmp_path, capsys, excluded, check_only):
    school = ActivitySchool()
    school.activities = [excluded, activity()]
    settings = Settings(state_dir=tmp_path, categories=(SERIES_CATEGORIES[0],))
    assert execute(school, tmp_path, settings=settings, clock=lambda: OPEN,
                   check_only=check_only) == (0 if check_only else 130)
    if not check_only:
        capsys.readouterr()
        show_status(tmp_path)
    result = details(capsys.readouterr().out)
    assert result[0] == dict(title='', reason='category_not_allowed',
                             reservation_start=None, reservation_end=None)
    assert result[1]['reason'] == ('candidate' if check_only else 'already_reserved')
    assert school.submitted == ([] if check_only else ['native'])
    if check_only:
        assert not (tmp_path / 'task.json').exists()


@pytest.mark.parametrize('category', [None, '', ' ', 42, [], SERIES_CATEGORIES[0]])
@pytest.mark.parametrize('check_only', [False, True])
def test_missing_invalid_or_target_category_still_requires_valid_lecture(
        tmp_path, capsys, category, check_only):
    school = ActivitySchool()
    school.activities = [{'JZXL_DISPLAY': category, 'HDLX_DISPLAY': '通知公告'}, activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN, check_only=check_only) == (1 if check_only else 130)
    assert not school.submitted
    assert 'activity_schema_unknown' in capsys.readouterr().out


@pytest.mark.parametrize('changes, filters, now, reason', [
    ({}, {}, OPEN - 1, 'reservation_not_started'),
    ({}, {}, CLOSE + 1, 'reservation_ended'),
    ({'FBZT': '-1'}, {}, OPEN, 'not_reservable'),
    ({'YY_WID': 'existing'}, {}, OPEN, 'already_reserved'),
    ({'JZXL_DISPLAY': '通知公告'}, {}, OPEN, 'category_not_allowed'),
    ({}, {'titles': ('another title',)}, OPEN, 'title_not_selected'),
    ({}, {'online': False}, OPEN, 'online_disabled'),
    ({'JZMC': '【线下】测试讲座', 'JZDD': '无锡校区会议室'}, {'offline': False}, OPEN, 'offline_disabled'),
    ({'JZMC': '【线下】测试讲座', 'JZDD': '无锡校区会议室'}, {}, OPEN, 'campus_not_selected'),
    ({'JZSJ': 'invalid'}, {}, OPEN, 'invalid_activity_time'),
])
def test_read_only_diagnostics_match_actual_selection_without_side_effects(
        tmp_path, capsys, changes, filters, now, reason):
    school = ActivitySchool()
    school.activities = [activity(**changes)]
    settings = Settings(state_dir=tmp_path, **filters)
    events = []
    assert execute(school, tmp_path, settings=settings, clock=lambda: now,
                   check_only=True, notify=events.append) == 0
    result = details(capsys.readouterr().out)
    assert len(result) == 1 and result[0]['reason'] == reason
    assert result[0]['reservation_start'] == '2029-12-31T19:00:00+08:00'
    assert not tmp_path.joinpath('task.json').exists() and not events and not school.submitted
    assert not tmp_path.joinpath('diagnostics.jsonl').exists()
    assert execute(school, tmp_path, settings=settings, clock=lambda: now) == 130
    assert not school.submitted
    assert json.loads((tmp_path / 'task.json').read_text())['selection']['activities'] == result


def test_check_and_scan_report_conflicts_and_first_filter_reason(tmp_path, capsys):
    school = School()
    school.mine = [lecture('old', YYZT='已预约')]
    school.activities = [lecture('conflict'), lecture('excluded', HDMC='excluded', IS_ONLINE=False)]
    settings = Settings(state_dir=tmp_path, titles=('测试讲座',))
    assert execute(school, tmp_path, settings=settings, check_only=True) == 0
    assert [item['reason'] for item in details(capsys.readouterr().out)] == ['time_conflict', 'title_not_selected']
    assert not (tmp_path / 'task.json').exists()


def test_candidates_and_cooldowns_are_explained_but_check_ignores_local_cooldown(tmp_path, capsys):
    school = School()
    school.activities = [lecture(HDMC='PRIVATE lecture')]
    school.submit_replies = [{'code': 1, 'msg': '验证码错误'}] * 4
    assert execute(school, tmp_path, clock=lambda: 1000) == 130
    assert execute(school, tmp_path, clock=lambda: 1001) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['selection']['activities'][0]['reason'] == 'cooldown'
    before = (tmp_path / 'task.json').read_bytes()
    capsys.readouterr()
    assert execute(school, tmp_path, check_only=True, clock=lambda: 1001) == 0
    assert details(capsys.readouterr().out)[0]['reason'] == 'candidate'
    assert (tmp_path / 'task.json').read_bytes() == before
    assert school.submitted == ['one'] * 4
    assert 'PRIVATE' not in (tmp_path / 'events.log').read_text()


def test_selection_and_mail_evidence_survive_restart_and_unknown_fields_are_not_printed(tmp_path, capsys):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    assert execute(school, tmp_path, clock=lambda: 1000) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    saved['selection']['activities'][0]['extra'] = 'PRIVATE EXTRA'
    saved['health'].update(last_mail_at=900, mail_status='accepted', last_mail_status='accepted',
                           last_mail_event='daily_report', last_mail_duration_seconds=1.25)
    path = tmp_path / 'task.json'
    path.write_text(json.dumps(saved))
    school.mine = attended(5) + [lecture(f'planned-{i}', YYZT='已预约', HDLX_DISPLAY=SERIES_CATEGORIES[3],
        KSSJ=f'2030-01-0{i + 1} 10:00:00', JSSJ=f'2030-01-0{i + 1} 11:00:00') for i in range(3)]
    assert execute(school, tmp_path, notify=None, clock=lambda: 1100) == 130
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'selection_updated_at=1970-01-01T08:16:40+08:00' in output
    assert details(output)[0]['reason'] == 'reservation_not_started'
    assert 'last_mail_status=accepted' in output and 'last_mail_duration_seconds=1.25' in output
    assert 'PRIVATE EXTRA' not in output
    saved = json.loads(path.read_text())
    saved['selection']['activities'][0]['reason'] = ['PRIVATE']
    path.write_text(json.dumps(saved))
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'activity_details=unknown' in output and 'PRIVATE' not in output


def test_completed_task_reopens_on_school_change_and_notifies_only_on_new_completion(tmp_path):
    school = School()
    school.mine = attended()
    events = []
    assert execute(school, tmp_path, notify=events.append) == 0
    school.mine[-1]['SFWY'] = '1'
    school.activities = [lecture('replacement', HDLX_DISPLAY=SERIES_CATEGORIES[3])]
    assert execute(school, tmp_path, notify=events.append) == 130
    assert school.submitted == ['replacement']
    assert events == []
    school.mine[-1]['SFDK'] = '1'
    assert execute(school, tmp_path, notify=events.append) == 0
    for new_task in (False, True):
        assert execute(school, tmp_path, notify=events.append, new_task=new_task) == 0
    assert events == []
    log = (tmp_path / 'events.log').read_text()
    assert log.count('event=goal_completed') == 2
    assert log.count('event=reservation_succeeded') == 1


@pytest.mark.parametrize('field, value', [('query_failure_since', float('nan')),
    ('query_failure_since', True), ('query_failure_since', -1),
    ('alerts', []), ('alerts', {'query_failure': float('inf')}),
    ('alerts', {'PRIVATE': 1000}), ('alerts', {'query_failure': -1})])
def test_invalid_alert_metadata_does_not_overwrite_original_or_contact_school(tmp_path, field, value):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = json.loads(path.read_text())
    saved[field] = value
    path.write_text(json.dumps(saved))
    original = path.read_bytes()
    school.requests.clear()
    assert execute(school, tmp_path) == 1
    assert path.read_bytes() == original and not school.requests


def diagnostics(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]


def test_scan_timing_is_logged_periodically_after_the_first_scan(tmp_path):
    school = School()
    school.activities = [lecture(YYZT='未开放', HDMC='PRIVATE TITLE')]
    now = [1000.0]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += 301
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait) == 130
    timings = [line for line in diagnostics(tmp_path) if line['event'] == 'scan_timing']
    assert len(timings) >= 2
    assert all(line['mode'] == 'normal' for line in timings)


def test_diagnostics_record_scan_timing_once_and_selection_only_on_change(tmp_path, capsys):
    school = School()
    school.activities = [lecture(YYZT='未开放', HDMC='PRIVATE TITLE')]
    now = 1000.0
    waits = []
    def wait(seconds):
        nonlocal now
        waits.append(seconds)
        now += seconds
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: now, sleep=wait) == 130
    lines = diagnostics(tmp_path)
    timings = [line for line in lines if line['event'] == 'scan_timing']
    assert len(timings) == 1
    timing = timings[0]
    assert timing['at'] == 1000 and timing['mode'] == 'normal'
    assert timing['wait_seconds'] == 10 and timing['duration_seconds'] >= 0
    assert timing['history_reused'] is False
    assert sum(line['event'] == 'selection_changed' for line in lines) == 1
    assert 'PRIVATE TITLE' not in capsys.readouterr().out
    assert 'private title' in (tmp_path / 'diagnostics.jsonl').read_text().lower()
    assert 'password' not in (tmp_path / 'diagnostics.jsonl').read_text().lower()


def test_selection_change_is_not_duplicated_across_restart(tmp_path):
    school = School()
    school.activities = [lecture(YYZT='未开放')]
    assert execute(school, tmp_path) == 130
    assert execute(school, tmp_path) == 130
    lines = diagnostics(tmp_path)
    assert sum(line['event'] == 'selection_changed' for line in lines) == 1


def test_pending_checks_keep_task_pending_and_are_diagnosed(tmp_path):
    school = School()
    school.activities = [lecture()]
    school.submit_timeout = True
    assert execute(school, tmp_path) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())
    saved['retry_at'] = 0
    (tmp_path / 'task.json').write_text(json.dumps(saved))
    school.submit_timeout = False
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, sleep=wait, clock=lambda: 2000) == 130
    lines = diagnostics(tmp_path)
    checks = [line['checks'] for line in lines if line['event'] == 'reservation_checked']
    assert checks == [1, 2, 3, 4, 5]
    assert not any(line['event'] == 'reservation_unconfirmed_released' for line in lines)
    errors = [line for line in lines if line['event'] == 'submission_error']
    assert errors and errors[0]['wid'] == 'one' and errors[0]['reason'] == 'network_timeout'
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'pending'


def test_ocr_failures_cooldown_and_submission_reply_are_diagnosed(tmp_path):
    school = School()
    school.activities = [lecture()]
    assert execute(school, tmp_path, recognize=lambda _: '') == 130
    lines = diagnostics(tmp_path)
    assert sum(line['event'] == 'ocr_invalid' for line in lines) == 4
    assert any(line['event'] == 'captcha_cooldown' and line['seconds'] == 300 for line in lines)
    school = School()
    school.activities = [lecture()]
    school.submit_replies = [{'code': -1, 'msg': 'PRIVATE server text'}]
    other = tmp_path / 'other'
    assert execute(school, other) == 130
    lines = diagnostics(other)
    assert [line['outcome'] for line in lines if line['event'] == 'submission_reply'] == ['unconfirmed']
    assert [line['attempt'] for line in lines if line['event'] == 'submission_started'] == [1]
    assert 'PRIVATE' not in (other / 'diagnostics.jsonl').read_text()
