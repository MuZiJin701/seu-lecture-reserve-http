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


def test_confirmed_target_ends_the_opening_window_while_other_categories_are_needed(tmp_path, monkeypatch):
    import seu_reserve.app as app_module
    from seu_reserve.poller import Snapshot
    from test_app import FakeHistoryPoller, FakePoller
    school = ActivitySchool()
    school.activities = [activity()]
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(FakePoller, 'instances', [])
    monkeypatch.setattr(FakePoller, 'snapshot', lambda self: Snapshot(rows=school.activities, at=OPEN))
    monkeypatch.setattr(FakeHistoryPoller, 'snapshot', lambda self: Snapshot(rows=list(school.mine), at=OPEN))
    readers = []
    def history_reader(query, **kwargs):
        reader = FakeHistoryPoller(query, **kwargs)
        readers.append(reader)
        return reader
    monkeypatch.setattr(app_module, 'new_history_poller', history_reader)
    waits = []
    def stop(seconds):
        waits.append(seconds)
        assert FakePoller.instances and readers
        assert all(reader.running for reader in [*FakePoller.instances, *readers])
        raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop, background_poller=True) == 130
    assert len(waits) == 1 and 4 <= waits[0] <= 6
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['selection']['activities'][0]['reason'] == 'already_reserved'
    assert saved['health']['progress']['missing']


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


@pytest.mark.parametrize('clock_now, server_now, available', [
    (OPEN, '2029-12-31 18:59:59', True),
    (OPEN, '2030-01-01 09:00:01', True),
    (OPEN - 1, '2030-01-01 08:00:00', False),
    (CLOSE + 1, '2030-01-01 08:00:00', False),
    (OPEN, 'invalid', True),
])
def test_native_availability_uses_host_clock(tmp_path, clock_now, server_now, available):
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
    row = activity(**changes)
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
    assert len(waits) == 1 and .5 <= waits[0] <= 1.5
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
    assert len(waits) == 2 and all(.05 <= seconds <= .1 for seconds in waits)


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
    assert len(waits) == 2 and all(.05 <= wait <= .10 for wait in waits)


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


def test_opening_overload_and_rate_limit_recover_without_extra_probes(tmp_path):
    now = [OPEN - 31]
    calls = []
    class Congested(ActivitySchool):
        errors = []
        def request(self, method, url, **kwargs):
            endpoint = urlsplit(url).path.rsplit('/', 1)[-1]
            if endpoint == 'queryActivityList.do' and self.submitted:
                raise KeyboardInterrupt
            if '/modules/hdyy/' in url:
                calls.append((endpoint, kwargs['timeout']))
                now[0] += {'queryActivityList.do': 0.4, 'queryMyActivityList.do': 0.2,
                           'vcode.do': 0.3, 'addReservation.do': 0.4}[endpoint]
            response = super().request(method, url, **kwargs)
            if endpoint == 'queryActivityList.do' and self.errors:
                response.status_code = self.errors.pop(0)
                response._content = b''
                if response.status_code == 429:
                    response.headers['Retry-After'] = '2'
            return response
    school = Congested()
    school.activities = [activity()]
    assert execute(school, tmp_path, clock=lambda: now[0]) == 130
    now[0] = OPEN
    calls.clear()
    school.errors = [503, 503, 503, 504, 429]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        assert len(waits) <= 5
        now[0] += seconds
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=wait) == 130
    assert waits == [1, 2, 4, 5, 2]
    assert calls == [*[('queryActivityList.do', 6)] * 6, ('queryMyActivityList.do', 6),
                     ('vcode.do', 8), ('addReservation.do', 8), ('queryMyActivityList.do', 6)]
    assert school.submitted == ['native']
    assert 'event=reservation_succeeded' in (tmp_path / 'events.log').read_text()


def test_recent_history_cache_survives_list_failure_and_enables_submission(tmp_path):
    now = [OPEN - 10]
    class CacheSchool(ActivitySchool):
        list_calls = 0
        mine_calls = 0
        def request(self, method, url, **kwargs):
            if url.endswith('queryActivityList.do'):
                self.list_calls += 1
                if self.list_calls == 2:
                    raise requests.Timeout('SECRET list timeout')
            if url.endswith('queryMyActivityList.do'):
                self.mine_calls += 1
                if self.mine_calls >= 2:
                    raise requests.Timeout('SECRET history timeout')
            return super().request(method, url, **kwargs)
    school = CacheSchool()
    school.activities = [activity()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        now[0] = OPEN
        if len(waits) == 5:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: now[0], sleep=stop) == 130
    assert school.submitted == ['native']
    assert school.list_calls >= 3


def test_invalid_ocr_retries_on_the_next_scan_without_cooldown(tmp_path):
    school = ActivitySchool()
    school.activities = [activity()]
    waits = []
    def stop(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: OPEN, sleep=stop, recognize=lambda _: '') == 130
    captchas = [url for _, url, _ in school.requests if url.endswith('vcode.do')]
    assert len(captchas) == 8
    assert not json.loads((tmp_path / 'task.json').read_text())['cooldowns']
