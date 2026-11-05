import json

import pytest

from seu_reserve.app import Settings, rotate_diagnostics
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
    stored = json.loads((tmp_path / 'task.json').read_text())['selection']['activities']
    assert [{key: value for key, value in item.items() if key != 'wid'} for item in stored] == result
    assert stored[0]['wid'] == 'native' and all('wid' not in item for item in result)


def test_legacy_cooldown_is_cleared_and_check_preserves_the_task(tmp_path, capsys):
    school = School()
    school.activities = [lecture(HDMC='PRIVATE lecture')]
    school.submit_replies = [{'code': 1, 'msg': '验证码错误'}] * 8
    assert execute(school, tmp_path, clock=lambda: 1000) == 130
    path = tmp_path / 'task.json'
    legacy = json.loads(path.read_text())
    legacy['cooldowns'] = {'one': 1300}
    path.write_text(json.dumps(legacy))
    assert execute(school, tmp_path, clock=lambda: 1001) == 130
    saved = json.loads(path.read_text())
    assert not saved['cooldowns'] and saved['selection']['activities'][0]['reason'] == 'candidate'
    before = path.read_bytes()
    capsys.readouterr()
    assert execute(school, tmp_path, check_only=True, clock=lambda: 1001) == 0
    assert details(capsys.readouterr().out)[0]['reason'] == 'candidate'
    assert path.read_bytes() == before and school.submitted == ['one'] * 8
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
    # The new history rows make the stored selection stale, so this scan records its own timestamp
    # and reason; the restart must still keep the (sanitized) selection evidence.
    assert 'selection_updated_at=1970-01-01T08:18:20+08:00' in output
    assert details(output)[0]['reason'] == 'time_conflict'
    assert 'last_mail_status=accepted' in output and 'last_mail_duration_seconds=1.25' in output
    assert 'PRIVATE EXTRA' not in output
    saved = json.loads(path.read_text())
    saved['selection']['activities'][0]['reason'] = ['PRIVATE']
    path.write_text(json.dumps(saved))
    capsys.readouterr()
    show_status(tmp_path)
    output = capsys.readouterr().out
    assert 'activity_details=unknown' in output and 'PRIVATE' not in output


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


def test_rotate_diagnostics_archives_an_oversized_log_without_losing_content(tmp_path):
    active = tmp_path / 'diagnostics.jsonl'
    first = '{"event":"old"}\n' * 4
    active.write_text(first)
    rotate_diagnostics(tmp_path, limit=10)
    archived = sorted(tmp_path.glob('diagnostics-*.jsonl'))
    assert len(archived) == 1 and archived[0].read_text() == first
    assert not active.exists()      # the next diagnostic write starts a new, empty active file

    second = '{"event":"older"}\n' * 4
    active.write_text(second)
    rotate_diagnostics(tmp_path, limit=10)
    # A same-second rotation reaches the collision suffix; both files are kept.
    archived = sorted(tmp_path.glob('diagnostics-*.jsonl'))
    assert len(archived) == 2
    assert {path.read_text() for path in archived} == {first, second}
