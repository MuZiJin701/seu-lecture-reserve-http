import json
from urllib.parse import urlsplit

import pytest

from seu_reserve.app import Settings
from seu_reserve.model import SERIES_CATEGORIES, moment
from test_app import School, execute, lecture


OPEN = moment('2029-12-31 19:00:00').timestamp()
CLOSE = moment('2030-01-01 09:00:00').timestamp()


def activity(wid='native', **changes):
    # Constructed values, using the field names and types observed in the school response.
    return {'WID': wid, 'JZMC': '【线上】测试讲座', 'JZXL_DISPLAY': SERIES_CATEGORIES[0],
            'JZDD': '腾讯会议', 'JZSJ': '2030-01-01 10:00:00',
            'HDJSSJ': '2030-01-01 11:00:00', 'FBZT': '1', 'SFKSYY': 0,
            'YYKSSJ': '2029-12-31 19:00:00', 'YYJSSJ': '2030-01-01 09:00:00',
            **changes}


class ActivitySchool(School):
    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        if urlsplit(url).path.endswith('addReservation.do') and self.mine:
            self.mine[-1].pop('YYZT', None)
            self.mine[-1].update(SFDK='0', SFWY='0')
        return response


def test_native_open_activity_is_submitted_then_confirmed_by_activity_id(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    events = []
    assert execute(school, tmp_path, clock=lambda: OPEN, notify=events.append) == 130
    assert school.submitted == ['native']
    assert events == ['reservation_succeeded']
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['status'] == 'scanning' and not saved['activity']


@pytest.mark.parametrize('now, available', [(OPEN - 1, False), (OPEN, True),
                                          (CLOSE, True), (CLOSE + 1, False)])
def test_native_reservation_window_boundaries(tmp_path, now, available):
    school = ActivitySchool()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: now) == 130
    assert school.submitted == (['native'] if available else [])


@pytest.mark.parametrize('changes', [
    {'FBZT': 'unknown'}, {'FBZT': None}, {'JZMC': None}, {'JZMC': '测试讲座'},
    {'JZXL_DISPLAY': None}, {'JZDD': None}, {'YYKSSJ': None},
    {'YYJSSJ': 'invalid'}, {'YYJSSJ': '2029-12-31 18:00:00'}, {'WID': ''}, {'YY_WID': 42},
])
def test_unknown_native_schema_alerts_even_before_opening(tmp_path, changes):
    school = School()
    school.activities = [activity(**changes)]
    events, delays = [], []
    def wait(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN - 1,
                   notify=events.append, sleep=wait) == 130
    assert events == ['query_failure']
    assert school.submitted == []
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['health']['last_error'] == 'activity_schema_unknown'
    assert saved['health']['last_query_at'] == 0


@pytest.mark.parametrize('check_only', [False, True])
def test_completely_unknown_schema_cannot_look_like_successful_empty_scan(tmp_path, check_only, capsys):
    school = School()
    school.activities = [{'WID': 'unknown', 'NEW_TITLE_FIELD': 'PRIVATE title'}]
    assert execute(school, tmp_path, check_only=check_only) == (1 if check_only else 130)
    assert school.submitted == []
    output = capsys.readouterr().out
    assert 'activity_schema_unknown' in output
    assert 'PRIVATE' not in output
    assert 'read_only_complete' not in output


@pytest.mark.parametrize('changes, allowed', [
    ({'JZMC': '【线上】测试讲座'}, True),
    ({'JZMC': '【线下】测试讲座', 'JZDD': '无锡校区会议室'}, True),
    ({'JZMC': '【线下】测试讲座', 'JZDD': '九龙湖校区报告厅'}, False),
    ({'FBZT': '-1'}, False),
    ({'YY_WID': 'existing-reservation'}, False),
    ({'JZXL_DISPLAY': '“SEU咖啡间"系列沙龙活动'}, False),
])
def test_native_fields_obey_existing_campus_and_goal_filters(tmp_path, changes, allowed):
    school = ActivitySchool()
    school.activities = [activity(**changes)]
    settings = Settings(state_dir=tmp_path, campuses=('无锡校区',))
    assert execute(school, tmp_path, settings=settings, clock=lambda: OPEN) == 130
    assert school.submitted == (['native'] if allowed else [])


def test_native_activity_still_obeys_existing_reservation_conflicts(tmp_path):
    school = School()
    school.activities = [activity()]
    school.mine = [lecture('booked', YYZT='已预约')]
    assert execute(school, tmp_path, clock=lambda: OPEN) == 130
    assert not school.submitted


@pytest.mark.parametrize('changes, expected', [({}, 0), ({'JZMC': None}, 1)])
def test_read_only_check_validates_native_schema_without_booking_or_state(tmp_path, changes, expected):
    school = School()
    school.activities = [activity(**changes)]
    assert execute(school, tmp_path, clock=lambda: OPEN - 1, check_only=True) == expected
    assert not school.submitted
    assert not (tmp_path / 'task.json').exists()
