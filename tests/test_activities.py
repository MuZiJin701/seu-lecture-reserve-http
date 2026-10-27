import json
from urllib.parse import urlsplit

import pytest
import requests

from seu_reserve.app import Settings
from seu_reserve.model import Lecture, SERIES_CATEGORIES, moment
from test_app import School, execute, interrupted, lecture


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
    submits = [kwargs for method, url, kwargs in school.requests if url.endswith('addReservation.do')]
    assert submits and submits[0]['timeout'] == 8
    assert events == []
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()
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
    assert events == []
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
    ({'JZXL_DISPLAY': '“SEU咖啡间"系列沙龙活动'}, True),
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


@pytest.mark.parametrize('clock_now, server_now, available', [
    (OPEN, '2029-12-31 18:59:59', False),
    (OPEN, '2030-01-01 09:00:01', False),
    (OPEN - 86400, '2030-01-01 08:00:00', True),
])
def test_server_clock_controls_native_availability(tmp_path, clock_now, server_now, available):
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = server_now
    assert execute(school, tmp_path, clock=lambda: clock_now) == 130
    assert school.submitted == (['native'] if available else [])


@pytest.mark.parametrize('changes, expected', [
    ({'YYRS': 30, 'HDZRS': '30'}, 'capacity_full'),
    ({'YYRS': '29', 'HDZRS': 30}, 'candidate'),
    ({'YYRS': 1, 'HDZRS': '30'}, 'candidate'),
    ({}, 'candidate'),
])
def test_native_capacity_uses_joined_counts(changes, expected):
    row = {**activity(**changes), '_SERVER_NOW': '2030-01-01 08:00:00'}
    assert Lecture.read(row, now=OPEN).availability == expected


def test_capacity_full_activity_is_skipped_for_next_candidate(tmp_path):
    school = ActivitySchool()
    school.activities = [activity('full'), activity('open')]
    school.activities[0]['HDZRS'] = '30'
    school.activities[1]['HDZRS'] = '30'
    school.hdrs = [{'HD_WID': 'full', 'YYRS': '30'}, {'HD_WID': 'open', 'YYRS': 0}]
    assert execute(school, tmp_path, clock=lambda: OPEN) == 130
    assert school.submitted == ['open']


def test_opening_network_failure_retries_at_fast_cadence_without_counting(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130

    class Flaky(ActivitySchool):
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                raise requests.ConnectionError('SECRET network failure')
            return super().request(method, url, **kwargs)
    flaky = Flaky()
    flaky.activities = [activity()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt
    assert execute(flaky, tmp_path, clock=lambda: OPEN - 30, sleep=stop) == 130
    assert waits == [0.25]
    assert json.loads((tmp_path / 'task.json').read_text())['health']['failure_count'] == 0


def test_opening_history_failure_does_not_hide_activity_polling(tmp_path):
    class FlakyHistory(ActivitySchool):
        fail_history = False
        def request(self, method, url, **kwargs):
            if self.fail_history and url.endswith('queryMyActivityList.do'):
                raise requests.Timeout('SECRET history timeout')
            return super().request(method, url, **kwargs)
    school = FlakyHistory()
    school.activities = [activity()]
    # Prime a saved selection so the next run starts inside the fast window.
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130
    school.fail_history = True
    school.requests.clear()
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN - 1, sleep=stop) == 130
    lists = [url for _, url, _ in school.requests if url.endswith('queryActivityList.do')]
    assert len(lists) >= 2
    assert waits == [0.25, 0.25]


def test_known_opening_survives_a_valid_empty_list(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130

    class EmptyAfterFirst(ActivitySchool):
        served = False
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                if self.served:
                    self.activities = []
                self.served = True
            return super().request(method, url, **kwargs)
    empty = EmptyAfterFirst()
    empty.activities = [activity()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(empty, tmp_path, clock=lambda: OPEN - 1, sleep=stop) == 130
    # The retained window keeps the opening cadence even though the latest list was empty.
    assert waits == [0.25, 0.25]


def test_slow_captcha_path_submits_without_an_extra_list_query(tmp_path):
    class StaleActivitySchool(ActivitySchool):
        lists = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.lists += 1
                if self.lists == 2:
                    self.activities = [activity(FBZT='-1')]
            return super().request(method, url, **kwargs)
    school = StaleActivitySchool()
    school.activities = [activity()]
    now = [OPEN]
    def clock():
        return now[0]
    def recognize(_):
        now[0] += 4
        return '1234'
    assert execute(school, tmp_path, clock=clock, recognize=recognize, sleep=interrupted) == 130
    assert school.submitted == ['native']
    lines = [json.loads(line) for line in (tmp_path / 'diagnostics.jsonl').read_text().splitlines()]
    assert not any(line['event'] == 'candidate_revalidated' for line in lines)


def test_opening_history_cache_survives_forty_seconds_without_refresh(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = '2029-12-31 18:59:50'
    now = [OPEN]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += 20
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait) == 130
    history_calls = [url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]
    assert len(history_calls) == 1


def test_opening_scan_reuses_history_until_cache_expires(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130
    school.server_now = '2029-12-31 18:59:50'
    school.requests.clear()
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop) == 130
    assert sum(url.endswith('queryMyActivityList.do') for _, url, _ in school.requests) == 1
    assert waits == [0.25, 0.25, 0.25]


def test_overload_503_uses_short_bounded_backoff_in_opening(tmp_path):
    class Overloaded(ActivitySchool):
        overloaded = False
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if self.overloaded and url.endswith('queryActivityList.do'):
                response.status_code = 503
                response.headers.pop('Retry-After', None)
                response._content = b''
            return response
    school = Overloaded()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130
    school.overloaded = True
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop) == 130
    assert waits == [1, 2, 4, 5, 5]


def test_overload_503_with_retry_after_is_honored(tmp_path):
    class Overloaded(ActivitySchool):
        overloaded = False
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if self.overloaded and url.endswith('queryActivityList.do'):
                response.status_code = 503
                response.headers['Retry-After'] = '2'
                response._content = b''
            return response
    school = Overloaded()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: OPEN - 31) == 130
    school.overloaded = True
    waits = []
    def stop(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop) == 130
    assert waits == [2]


def test_recent_history_cache_survives_list_failure_and_enables_submission(tmp_path):
    open_text = moment('2029-12-31 19:00:00').strftime('%Y-%m-%d %H:%M:%S')
    pre_open_text = moment('2029-12-31 18:59:50').strftime('%Y-%m-%d %H:%M:%S')
    class CacheSchool(ActivitySchool):
        list_calls = 0
        mine_calls = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.list_calls += 1
                if self.list_calls == 2:
                    raise requests.Timeout('SECRET list timeout')
                if self.list_calls >= 3:
                    self.server_now = open_text
            if url.endswith('queryMyActivityList.do'):
                self.mine_calls += 1
                if self.mine_calls >= 2:
                    raise requests.Timeout('SECRET history timeout')
            return super().request(method, url, **kwargs)
    school = CacheSchool()
    school.activities = [activity()]
    school.server_now = pre_open_text
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop) == 130
    assert school.submitted == ['native']
    assert school.list_calls >= 3


def test_captcha_cooldown_uses_school_clock_basis(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = moment('2029-12-31 19:10:00').strftime('%Y-%m-%d %H:%M:%S')
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop, recognize=lambda _: '') == 130
    captchas = [url for _, url, _ in school.requests if url.endswith('vcode.do')]
    assert len(captchas) == 4


def test_cooldown_survives_restart_before_clock_calibration(tmp_path):
    school = ActivitySchool()
    school.activities = [activity(YYKSSJ='2029-12-31 18:00:00', YYJSSJ='2029-12-31 20:00:00')]
    school.server_now = moment('2029-12-31 18:50:00').strftime('%Y-%m-%d %H:%M:%S')
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop, recognize=lambda _: '') == 130
    assert sum(url.endswith('vcode.do') for _, url, _ in school.requests) == 4
    school.requests.clear()
    waits.clear()
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop, recognize=lambda _: '') == 130
    assert sum(url.endswith('vcode.do') for _, url, _ in school.requests) == 0


def test_cooldown_waits_for_clock_after_restart_then_expires(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    school.server_now = '2029-12-31 19:00:00'
    now = [OPEN + 600]
    options = dict(clock=lambda: now[0], sleep=interrupted, recognize=lambda _: '')
    assert execute(school, tmp_path, **options) == 130
    deadline = json.loads((tmp_path / 'task.json').read_text())['cooldowns']['native']
    assert deadline == OPEN + 300

    school.requests.clear()
    school.server_now = None
    assert execute(school, tmp_path, **options) == 130
    assert not any(url.endswith('vcode.do') for _, url, _ in school.requests)
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['cooldowns']['native'] == deadline
    assert saved['selection']['activities'][0]['reason'] == 'cooldown'

    school.server_now = '2029-12-31 19:05:01'
    now[0] += 301
    assert execute(school, tmp_path, **options) == 130
    assert sum(url.endswith('vcode.do') for _, url, _ in school.requests) == 4


@pytest.mark.parametrize('slow_path', [False, True])
def test_missing_clock_keeps_calibration_for_candidate_and_cooldown(tmp_path, slow_path):
    class MissingClockSchool(ActivitySchool):
        lists = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.lists += 1
                if self.lists > 1:
                    self.server_now = None
            return super().request(method, url, **kwargs)

    school = MissingClockSchool()
    school.activities = [activity(YYJSSJ='2029-12-31 19:05:00')]
    school.server_now = '2029-12-31 19:00:00'
    now = [OPEN + 600]
    def recognize(_):
        if slow_path:
            now[0] += 4
            return '1234'
        return ''
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=stop,
                   recognize=recognize) == 130
    if slow_path:
        assert school.submitted == ['native']
        assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()
    else:
        assert sum(url.endswith('vcode.do') for _, url, _ in school.requests) == 4
        saved = json.loads((tmp_path / 'task.json').read_text())
        assert saved['selection']['activities'][0]['reason'] == 'cooldown'
