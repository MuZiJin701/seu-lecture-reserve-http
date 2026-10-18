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


@pytest.mark.parametrize('changes, filters, now, reason', [
    ({}, {}, OPEN - 1, 'reservation_not_started'),
    ({}, {}, CLOSE + 1, 'reservation_ended'),
    ({'FBZT': '-1'}, {}, OPEN, 'not_reservable'),
    ({'YY_WID': 'existing'}, {}, OPEN, 'already_reserved'),
    ({'JZXL_DISPLAY': '“SEU咖啡间"系列沙龙活动'}, {}, OPEN, 'category_not_allowed'),
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
    assert events == ['goal_completed', 'reservation_succeeded']
    school.mine[-1]['SFDK'] = '1'
    assert execute(school, tmp_path, notify=events.append) == 0
    for new_task in (False, True):
        assert execute(school, tmp_path, notify=events.append, new_task=new_task) == 0
    assert events == ['goal_completed', 'reservation_succeeded', 'goal_completed']


@pytest.mark.parametrize('field, value', [('last_report_at', float('nan')), ('last_report_at', True),
    ('last_query_alert_at', -1), ('alerts', []), ('alerts', {'query_failure': float('inf')}),
    ('alerts', {'PRIVATE': 1000})])
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
