import hashlib
import json

import pytest

import seu_reserve.app as app_module
from seu_reserve.app import Settings
from seu_reserve.model import (COFFEE_CATEGORY, GOAL_CATEGORIES, SERIES_CATEGORIES,
                               category_name, moment, progress, test_notice as is_test_notice)
from seu_reserve.poller import Snapshot
from test_app import FakePoller, School, execute, interrupted, lecture


PSYCHOLOGY, LAW, ART, OTHER = SERIES_CATEGORIES


def attended(count=8):
    """Valid reservations spread across the four formal goal categories.

    The historical name is kept because other test modules import it; attendance is irrelevant
    to the current reference target, so ``SFDK`` is only present as an ignored school field.
    """
    return [lecture(f'history-{i}', YYZT='已预约', SFDK='1',
                    HDLX_DISPLAY=SERIES_CATEGORIES[i % 4],
                    KSSJ=f'2020-01-{i + 1:02} 10:00:00', JSSJ=f'2020-01-{i + 1:02} 11:00:00')
            for i in range(count)]


def saved_task(directory):
    return json.loads((directory / 'task.json').read_text())


def test_eight_valid_reservations_cover_the_reference_target():
    current = progress(attended(8), 0)
    assert current.total == 8
    assert current.complete
    assert not current.missing
    assert current.reserved == {PSYCHOLOGY: 2, LAW: 2, ART: 2, OTHER: 2}

    short = progress(attended(7), 0)
    assert short.total == 7
    assert not short.complete
    # The three required categories are covered; only the total count is short.
    assert not short.missing


def test_reference_target_requires_eight_reservations_and_each_required_category():
    all_psychology = attended(8)
    for row in all_psychology:
        row['HDLX_DISPLAY'] = PSYCHOLOGY
    current = progress(all_psychology, 0)
    assert current.total == 8
    assert not current.complete
    assert current.missing == frozenset({LAW, ART})

    without_art = attended(8)
    for row in without_art:
        if row['HDLX_DISPLAY'] == ART:
            row['HDLX_DISPLAY'] = OTHER
    current = progress(without_art, 0)
    assert current.total == 8
    assert not current.complete
    assert current.missing == frozenset({ART})


@pytest.mark.parametrize('changes', [
    {'SFDK': '1'}, {'SFDK': '0'}, {'SFDK': None}, {'SFDK': ''}, {'SFDK': 'unknown'},
    {'SFDK': None, 'SFWY': None},
])
def test_attendance_fields_no_longer_affect_the_reference_target(changes):
    rows = attended(8)
    for row in rows:
        row.update(changes)
    current = progress(rows, 0)
    assert current.total == 8 and current.complete


def test_breached_reservation_is_not_counted():
    breach = lecture('breach', YYZT='已预约', SFDK='1', SFWY='1')
    current = progress(attended(7) + [breach], 0)
    assert current.total == 7
    assert not current.complete


def test_withdrawn_reservation_is_not_counted():
    withdrawn = lecture('withdrawn', YYZT='已预约', FBZT='-1')
    current = progress(attended(7) + [withdrawn], 0)
    assert current.total == 7
    assert not current.complete


@pytest.mark.parametrize('category', [COFFEE_CATEGORY, '“SEU咖啡间"系列沙龙活动'])
@pytest.mark.parametrize('checked', ['0', '1', None])
def test_salon_reservations_are_not_counted(category, checked):
    assert category_name(category) == COFFEE_CATEGORY
    assert COFFEE_CATEGORY not in GOAL_CATEGORIES
    row = lecture('salon-history', YYZT='已预约', SFDK=checked, HDLX_DISPLAY=category)
    assert progress(attended(7) + [row], 0) == progress(attended(7), 0)
    complete = progress(attended(8) + [row], 0)
    assert complete.total == 8 and complete.complete


@pytest.mark.parametrize('category', ['通知公告', '', None, 42, '未知类别'])
def test_unreadable_or_non_goal_categories_are_ignored(category):
    row = lecture('other', YYZT='已预约', HDLX_DISPLAY=category)
    assert progress([row], 0).total == 0


def test_identical_duplicate_activity_is_counted_once():
    rows = [{'WID': f'booking-{i}', 'HD_WID': f'activity-{i}', 'FBZT': '1',
             'SFDK': '1', 'SFWY': '0', 'JZXL_DISPLAY': SERIES_CATEGORIES[i % 4],
             'JZSJ': f'2020-01-{i + 1:02} 10:00:00', 'HDJSSJ': f'2020-01-{i + 1:02} 11:00:00'}
            for i in range(8)]
    assert progress(rows + [dict(rows[0])], 0) == progress(rows, 0)
    assert progress(rows, 0).total == 8 and progress(rows, 0).complete


@pytest.mark.parametrize('changes', [
    {'SFDK': '0'}, {'SFWY': '1'}, {'HDLX_DISPLAY': LAW}, {'HDMC': 'another title'},
])
def test_conflicting_duplicate_activity_is_rejected(changes):
    row = attended(1)[0]
    with pytest.raises(ValueError, match='attendance_record_conflict'):
        progress([row, {**row, **changes}], 0)


def test_unknown_publication_status_is_rejected():
    with pytest.raises(ValueError, match='publication_status_unknown'):
        progress([lecture('bad', YYZT='已预约', FBZT='unknown')], 0)


def test_unknown_reservation_status_is_rejected():
    with pytest.raises(ValueError, match='reservation_status_unknown'):
        progress([lecture('bad', YYZT='unknown')], 0)


def test_reaching_the_reference_target_keeps_running_and_booking(tmp_path):
    school = School()
    school.mine = attended()
    school.activities = [lecture('extra', HDLX_DISPLAY=OTHER)]
    events = []
    # The scan only ends through the injected interruption: a reached reference target must not
    # stop, complete or park the task.
    assert execute(school, tmp_path, notify=events.append) == 130
    assert school.submitted == ['extra']
    assert events == []
    saved = saved_task(tmp_path)
    assert saved['version'] == 2 and saved['status'] == 'scanning'
    snapshot = saved['health']['progress']
    assert set(snapshot) == {'reserved', 'total', 'missing', 'complete', 'at'}
    assert snapshot['reserved'] == {PSYCHOLOGY: 2, LAW: 2, ART: 2, OTHER: 3}
    assert snapshot['total'] == 9 and snapshot['complete'] is True
    assert snapshot['missing'] == []
    assert snapshot['at'] == moment('2026-10-18 12:00:00').timestamp()
    assert saved['health']['phase'] not in ('done', 'waiting_for_attendance')
    log = (tmp_path / 'events.log').read_text()
    assert log.count('event=goal_completed') == 1
    assert 'done' not in saved['status']

    # A restart rechecks the school record and keeps scanning; it must not treat the target as a
    # terminal status.
    assert execute(school, tmp_path, notify=events.append) == 130
    assert school.submitted == ['extra']
    assert saved_task(tmp_path)['status'] == 'scanning'


def test_unreadable_category_reservation_is_excluded_but_not_blocking(tmp_path):
    # The unknown-category record cannot count toward the reference target, but its readable
    # interval must still protect the conflict check; a non-overlapping candidate is booked.
    uncertain = lecture('uncertain', YYZT='已预约', HDLX_DISPLAY='未知类别')
    assert progress([uncertain], 0).total == 0
    school = School()
    school.mine = [uncertain]
    school.activities = [lecture('next-law', HDLX_DISPLAY=LAW,
                                 KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['next-law']


@pytest.mark.parametrize('changes', [
    {'FBZT': 'unknown'}, {'KSSJ': 'invalid'}, {'YYZT': 'unknown'},
])
def test_structural_history_errors_still_block_new_submissions(tmp_path, changes):
    row = lecture('uncertain', YYZT='已预约')
    row.update(changes)
    school = School()
    school.mine = [row]
    school.activities = [lecture('next-law', HDLX_DISPLAY=LAW,
                                 KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    assert execute(school, tmp_path) == 130
    assert not school.submitted


def test_conflicting_duplicate_history_blocks_new_submissions(tmp_path):
    row = lecture('duplicate', YYZT='已预约')
    school = School()
    school.mine = [row, {**row, 'SFWY': '1'}]
    school.activities = [lecture('next-law', HDLX_DISPLAY=LAW,
                                 KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00')]
    assert execute(school, tmp_path) == 130
    assert not school.submitted
    assert 'event=attendance_record_conflict' in (tmp_path / 'events.log').read_text()


@pytest.mark.parametrize('category', [COFFEE_CATEGORY, '“SEU咖啡间"系列沙龙活动'])
@pytest.mark.parametrize('enabled', [True, False])
def test_salon_booking_follows_configuration_without_counting(tmp_path, category, enabled):
    school = School()
    school.activities = [lecture(HDLX_DISPLAY=category)]
    settings = Settings(state_dir=tmp_path,
                        categories=GOAL_CATEGORIES + ((category,) if enabled else ()))
    assert execute(school, tmp_path, settings=settings) == 130
    assert school.submitted == (['one'] if enabled else [])
    current = progress(school.mine, 0)
    assert current.total == 0 and not current.complete


def test_salon_reservation_still_blocks_conflicting_lectures(tmp_path):
    school = School()
    salon = lecture('coffee-booked', YYZT='已预约', HDLX_DISPLAY=COFFEE_CATEGORY,
                    SFDK=None, SFWY=None)
    school.mine = [dict(salon)]
    school.activities = [lecture('conflicting'), lecture('safe',
        KSSJ='2030-01-01 12:00:00', JSSJ='2030-01-01 13:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['safe']
    assert school.mine[0] == salon
    saved = saved_task(tmp_path)
    assert saved['selection']['activities'][0]['reason'] == 'time_conflict'
    # The salon never counts, but the newly booked formal lecture does.
    assert saved['health']['progress']['reserved'] == {PSYCHOLOGY: 1}


def test_salon_with_unknown_conflict_time_still_blocks_new_submissions(tmp_path):
    school = School()
    school.mine = [lecture('coffee-booked', YYZT='已预约', HDLX_DISPLAY=COFFEE_CATEGORY,
                           KSSJ='unknown')]
    school.activities = [lecture()]
    assert execute(school, tmp_path) == 130
    assert not school.submitted


def test_confirmed_salon_hold_keeps_conflict_protection_without_counting(tmp_path):
    school = School()
    assert execute(school, tmp_path) == 130
    path = tmp_path / 'task.json'
    saved = saved_task(tmp_path)
    now = moment('2026-10-18 12:00:00').timestamp()
    saved['holds'] = {'coffee-held': dict(
        start=moment('2030-01-01 10:00:00').timestamp(),
        end=moment('2030-01-01 11:00:00').timestamp(),
        since=now, until=now + 600, attempts=0, confirmed=True, category=COFFEE_CATEGORY)}
    path.write_text(json.dumps(saved))
    school.mine = attended(7)
    school.activities = [lecture('conflicting', HDLX_DISPLAY=OTHER),
                         lecture('safe', HDLX_DISPLAY=OTHER,
                                 KSSJ='2030-01-01 12:00:00', JSSJ='2030-01-01 13:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['safe']
    saved = saved_task(tmp_path)
    assert saved['holds']['coffee-held']['confirmed'] is True
    assert saved['selection']['activities'][0]['reason'] == 'time_conflict'


@pytest.mark.parametrize('title', ['系统测试讲座', '请勿预约', '购票公告'])
def test_notice_titles_are_excluded_from_progress_and_selection(tmp_path, title):
    row = lecture('notice', YYZT='已预约', HDLX_DISPLAY=COFFEE_CATEGORY, HDMC=title)
    assert is_test_notice(row)
    assert progress(attended(7) + [row], 0).total == 7
    school = School()
    school.activities = [lecture('notice-activity', HDLX_DISPLAY=COFFEE_CATEGORY, HDMC=title)]
    assert execute(school, tmp_path) == 130
    assert not school.submitted
    saved = saved_task(tmp_path)
    assert saved['selection']['activities'][0]['reason'] == 'notice_test_only'


def test_real_school_reservation_fields_are_counted_and_duplicates_are_not():
    rows = [{'WID': f'booking-{i}', 'HD_WID': f'activity-{i}', 'FBZT': '1',
             'SFDK': '1', 'SFWY': '0', 'JZXL_DISPLAY': SERIES_CATEGORIES[i % 4],
             'JZSJ': f'2020-01-{i + 1:02} 10:00:00', 'HDJSSJ': f'2020-01-{i + 1:02} 11:00:00'}
            for i in range(8)]
    assert progress(rows + [dict(rows[0])], 0) == progress(rows, 0)
    assert progress(rows, 0).total == 8 and progress(rows, 0).complete
    rows[-1]['SFWY'] = '1'
    current = progress(rows, 0)
    assert current.total == 7 and not current.complete
    rows[-1].update(SFWY='0', FBZT='-1')
    current = progress(rows, 0)
    assert current.total == 7 and not current.complete


def test_read_only_check_rejects_structural_history_errors_without_creating_state(tmp_path):
    school = School()
    school.mine = [lecture('uncertain', YYZT='已预约', FBZT='unknown')]
    directory = tmp_path / 'unused'
    assert execute(school, directory, check_only=True) == 1
    assert not directory.exists() and not school.submitted


def test_legacy_one_success_reopens_without_old_generic_wait(tmp_path):
    saved = {'version': 1, 'account': hashlib.sha256(b'test-user').hexdigest(),
             'status': 'done', 'activity': 'old-success', 'cooldowns': {}, 'retry_at': 200}
    path = tmp_path / 'task.json'
    path.write_text(json.dumps(saved))
    school = School()
    waits = []
    def wait(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt
    assert execute(school, tmp_path, clock=lambda: 100, sleep=wait) == 130
    migrated = json.loads(path.read_text())
    assert migrated['version'] == 2 and migrated['status'] == 'scanning'
    # A legacy generic wait has no server-directed marker, so it is clamped to the short schedule.
    assert not migrated['activity'] and migrated['retry_at'] == 0
    assert len(waits) == 1 and 4 <= waits[0] <= 6 and school.authenticated
    saved.update(status='pending', activity='uncertain', retry_at=0)
    path.write_text(json.dumps(saved))
    school.mine = attended()
    assert execute(school, tmp_path) == 130
    assert json.loads(path.read_text())['activity'] == 'uncertain'
    assert not school.submitted


class EventHistoryReader:
    """Deterministic event-driven replacement for the background "my activities" reader.

    The production reader answers explicit refresh events only (no periodic schedule); this
    stand-in performs the query synchronously when the scan requests it and records how often a
    refresh was actually asked for.  ``fail_next`` injects one transient request failure.
    """

    rows = []
    queries = 0
    fail_next = False
    instances = []

    def __init__(self, query, *, interval, clock, **_kwargs):
        self.clock = clock
        self.running = True
        self.polls = 0
        self.value = Snapshot(rows=list(type(self).rows), at=clock())
        type(self).instances.append(self)

    def start(self):
        self.running = True
        self.request_refresh()

    def stop(self, *, join=True):
        self.running = False

    def request_refresh(self):
        reader = type(self)
        reader.queries += 1
        self.polls += 1
        if reader.fail_next:
            reader.fail_next = False
            self.value = Snapshot(rows=self.value.rows, at=self.clock(), error='Timeout',
                                  reason='network_timeout', failed_at=self.clock(),
                                  started=self.clock())
        else:
            self.value = Snapshot(rows=list(reader.rows), at=self.clock(), started=self.clock())
        return self.polls

    def snapshot(self):
        return self.value

    def snapshot_with_version(self):
        return self.value, self.polls


def event_driven_history(monkeypatch, school):
    """Route the scan loop through the deterministic list/history readers."""
    FakePoller.instances.clear()
    FakePoller.next_rows = []
    FakePoller.rows_override = None
    FakePoller.next_error = None
    FakePoller.at_offset = 0.0
    FakePoller.on_timeout = None
    EventHistoryReader.rows = school.mine
    EventHistoryReader.queries = 0
    EventHistoryReader.fail_next = False
    EventHistoryReader.instances.clear()
    monkeypatch.setattr(app_module, 'ActivityPoller', FakePoller)
    monkeypatch.setattr(app_module, 'new_history_poller',
                        lambda query, **kwargs: EventHistoryReader(query, **kwargs))


def background_options(directory):
    return dict(background_poller=True, clock=lambda: 1000.0, sleep=interrupted,
                settings=Settings(state_dir=directory, prewarm=False, captcha_prefetch=False))


def test_history_snapshot_is_reused_without_a_ttl(tmp_path, monkeypatch):
    school = School()
    school.mine = attended(2)
    event_driven_history(monkeypatch, school)
    now = [1000.0]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += 100
        if len(waits) == 3:
            raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    options = background_options(tmp_path)
    options['clock'] = lambda: now[0]
    assert execute(school, tmp_path, **options) == 130
    assert len(waits) == 3
    # Three scans over 300 virtual seconds and exactly one baseline history query: the snapshot
    # never expires and no periodic refresh exists.
    assert EventHistoryReader.queries == 1
    snapshot = saved_task(tmp_path)['health']['progress']
    assert snapshot['total'] == 2 and snapshot['complete'] is False
    assert snapshot['at'] == 1000.0


def test_appearing_candidate_submits_without_a_history_refresh(tmp_path, monkeypatch):
    class WatchingSchool(School):
        queries_after_baseline = None
        queries_at_submit = None

        def request(self, method, url, **kwargs):
            if url.endswith('addReservation.do'):
                WatchingSchool.queries_at_submit = EventHistoryReader.queries
            return super().request(method, url, **kwargs)

    school = WatchingSchool()
    school.mine = attended()
    event_driven_history(monkeypatch, school)
    now = [1000.0]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            candidate = lecture('extra', HDLX_DISPLAY=OTHER)
            school.activities = [candidate]
            FakePoller.rows_override = [candidate]
            now[0] += 10
            WatchingSchool.queries_after_baseline = EventHistoryReader.queries
            return
        raise KeyboardInterrupt
    FakePoller.on_timeout = wait
    options = background_options(tmp_path)
    options['clock'] = lambda: now[0]
    assert execute(school, tmp_path, **options) == 130
    assert WatchingSchool.queries_after_baseline == 1
    assert school.submitted == ['extra']
    # A candidate must use the baseline without issuing or waiting for a history query.
    assert WatchingSchool.queries_at_submit == 1
    assert saved_task(tmp_path)['status'] == 'scanning'
    assert saved_task(tmp_path)['health']['progress']['total'] == 9
    assert (tmp_path / 'events.log').read_text().count('event=goal_completed') == 1
