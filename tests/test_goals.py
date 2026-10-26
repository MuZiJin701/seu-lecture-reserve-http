import hashlib
import json
from collections import Counter

import pytest

from seu_reserve.model import (COFFEE_CATEGORY, GOAL_CATEGORIES, SERIES_CATEGORIES, Progress,
                               category_name, progress, test_notice as is_test_notice)
from test_app import School, execute, lecture


PSYCHOLOGY, LAW, ART, OTHER = SERIES_CATEGORIES


def attended(count=8):
    return [lecture(f'history-{i}', YYZT='已预约', SFDK='1',
                    HDLX_DISPLAY=SERIES_CATEGORIES[i % 4],
                    KSSJ=f'2020-01-{i + 1:02} 10:00:00', JSSJ=f'2020-01-{i + 1:02} 11:00:00')
            for i in range(count)]


def test_only_eight_attended_with_all_required_categories_complete_goal(tmp_path):
    school = School()
    school.mine = attended()
    events = []
    assert execute(school, tmp_path, notify=events.append) == 0
    assert events == []
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['version'] == 2 and saved['status'] == 'done'
    assert not saved['activity']
    assert 'event=goal_completed' in (tmp_path / 'events.log').read_text()
    requests_before_restart = len(school.requests)
    assert execute(school, tmp_path, notify=events.append) == 0
    assert len(school.requests) > requests_before_restart
    assert execute(school, tmp_path, new_task=True, notify=events.append) == 0
    assert events == []
    assert not school.submitted


def test_success_continues_and_missing_categories_precede_filler(tmp_path):
    school = School()
    school.mine = attended(5)
    # Both required categories are already covered: make art missing.
    school.mine[2]['HDLX_DISPLAY'] = PSYCHOLOGY
    school.activities = [lecture('filler', HDLX_DISPLAY=OTHER),
                         lecture('art', HDLX_DISPLAY=ART, KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00'),
                         lecture('last', HDLX_DISPLAY=OTHER, KSSJ='2030-01-03 10:00:00', JSSJ='2030-01-03 11:00:00'),
                         lecture('extra', HDLX_DISPLAY=OTHER, KSSJ='2030-01-04 10:00:00', JSSJ='2030-01-04 11:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['art', 'filler', 'last']
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'


def test_filler_leaves_room_for_missing_required_categories(tmp_path):
    school = School()
    school.mine = attended(6)
    for row in school.mine:
        row['HDLX_DISPLAY'] = PSYCHOLOGY
    school.activities = [lecture('unneeded', HDLX_DISPLAY=OTHER)]
    assert execute(school, tmp_path) == 130
    assert not school.submitted


@pytest.mark.parametrize('change', ['removed', 'cancelled', 'breached'])
def test_coverage_waits_cancellation_or_breach_is_replaced_and_attendance_finishes(tmp_path, change):
    school = School()
    school.mine = attended(5) + [lecture(f'planned-{i}', YYZT='已预约', HDLX_DISPLAY=OTHER,
        KSSJ=f'2030-01-0{i + 1} 10:00:00', JSSJ=f'2030-01-0{i + 1} 11:00:00') for i in range(3)]
    school.activities = [lecture('replacement', HDLX_DISPLAY=OTHER,
                                KSSJ='2030-01-04 10:00:00', JSSJ='2030-01-04 11:00:00')]
    waits = []
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            assert not school.submitted
            if change == 'removed':
                school.mine.pop()
            elif change == 'cancelled':
                school.mine[-1]['YYZT'] = '已取消'
            else:
                school.mine[-1]['SFWY'] = '1'
        elif len(waits) == 2:
            assert school.submitted == ['replacement']
            for row in school.mine:
                row['SFDK'] = '1'
        else:
            pytest.fail('goal should finish after school confirms attendance')
    assert execute(school, tmp_path, sleep=wait) == 0
    assert waits == [10, 10]


def test_eight_attended_without_art_still_books_art(tmp_path):
    school = School()
    school.mine = attended()
    for row in school.mine:
        if row['HDLX_DISPLAY'] == ART:
            row['HDLX_DISPLAY'] = OTHER
    school.activities = [lecture('extra-other', HDLX_DISPLAY=OTHER), lecture('needed-art', HDLX_DISPLAY=ART)]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['needed-art']


def test_salon_counts_toward_goal_and_both_quote_variants_match(tmp_path):
    from seu_reserve.app import Settings
    coffee_ascii = '“SEU咖啡间"系列沙龙活动'
    assert category_name(coffee_ascii) == COFFEE_CATEGORY
    assert category_name(COFFEE_CATEGORY) == COFFEE_CATEGORY
    assert COFFEE_CATEGORY in GOAL_CATEGORIES
    school = School()
    school.activities = [lecture(HDLX_DISPLAY=coffee_ascii)]
    assert execute(school, tmp_path, settings=Settings(state_dir=tmp_path, categories=GOAL_CATEGORIES)) == 130
    assert school.submitted == ['one']
    school.mine = attended(7) + [lecture('coffee-history', YYZT='已预约', SFDK='1',
                                        HDLX_DISPLAY=coffee_ascii)]
    assert progress(school.mine, 0).done


def test_salon_uses_goal_gap_but_never_displaces_missing_required_category():
    covered = Counter({PSYCHOLOGY: 3, LAW: 2, ART: 1, OTHER: 1})
    assert sum(covered.values()) == 7 and Progress(covered, Counter()).needs(COFFEE_CATEGORY)
    missing_art = Counter({PSYCHOLOGY: 3, LAW: 2, OTHER: 2})
    assert not Progress(missing_art, Counter()).needs(COFFEE_CATEGORY)
    assert Progress(missing_art, Counter()).needs(ART)


@pytest.mark.parametrize('title', ['系统测试讲座', '请勿预约', '购票公告'])
def test_notice_titles_are_excluded_from_progress_and_selection(tmp_path, title):
    row = lecture('notice', YYZT='已预约', SFDK='1', HDLX_DISPLAY=COFFEE_CATEGORY, HDMC=title)
    assert is_test_notice(row)
    assert not progress(attended(7) + [row], 0).done
    school = School()
    school.activities = [lecture('notice-activity', HDLX_DISPLAY=COFFEE_CATEGORY, HDMC=title)]
    assert execute(school, tmp_path) == 130
    assert not school.submitted
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['selection']['activities'][0]['reason'] == 'notice_test_only'


def test_real_school_fields_are_counted_and_duplicates_are_not():
    rows = [{'WID': f'booking-{i}', 'HD_WID': f'activity-{i}', 'FBZT': '1',
             'SFDK': '1', 'SFWY': '0', 'JZXL_DISPLAY': SERIES_CATEGORIES[i % 4],
             'JZSJ': f'2020-01-{i + 1:02} 10:00:00', 'HDJSSJ': f'2020-01-{i + 1:02} 11:00:00'} for i in range(8)]
    assert progress(rows + [dict(rows[0])], 0).done
    rows[-1]['SFWY'] = '1'
    assert not progress(rows, 0).done
    rows[-1].update(SFWY='0', FBZT='-1')
    assert not progress(rows, 0).done


@pytest.mark.parametrize('changes', [
    {'SFDK': None}, {'SFWY': 'unknown'}, {'HDLX_DISPLAY': ''}, {'HDLX_DISPLAY': '未知类别'},
    {'KSSJ': 'invalid'}, {'FBZT': 'unknown'},
    {'SFDK': '0', 'KSSJ': '2020-01-01 10:00:00', 'JSSJ': '2020-01-01 11:00:00'},
])
def test_uncertain_attendance_blocks_new_submissions(tmp_path, changes):
    school = School()
    school.mine = [lecture('uncertain', YYZT='已预约', **changes)]
    school.activities = [lecture()]
    assert execute(school, tmp_path) == 130
    assert not school.submitted


def test_conflicting_duplicate_activity_is_rejected():
    row = attended(1)[0]
    with pytest.raises(ValueError, match='attendance_record_conflict'):
        progress([row, {**row, 'SFDK': '0'}], 0)


@pytest.mark.parametrize('checked', ['0', '1'])
def test_chained_overlaps_keep_nonconflicting_lectures_in_either_input_order(checked):
    rows = [lecture('a', SFDK=checked, KSSJ='2030-01-01 10:00:00', JSSJ='2030-01-01 11:00:00'),
            lecture('b', SFDK=checked, KSSJ='2030-01-01 10:30:00', JSSJ='2030-01-01 11:30:00'),
            lecture('c', SFDK=checked, KSSJ='2030-01-01 11:10:00', JSSJ='2030-01-01 12:00:00')]
    for row in rows:
        row['YYZT'] = '已预约'
    original = [dict(row) for row in rows]
    for ordered in (rows, list(reversed(rows))):
        current = progress(ordered, 0)
        assert current.completed == ({PSYCHOLOGY: 2} if checked == '1' else {})
        assert current.planned == ({PSYCHOLOGY: 2} if checked == '0' else {})
    assert rows == original


def test_completed_overlap_precedes_unattended_required_category():
    rows = [lecture('planned-law', YYZT='已预约', HDLX_DISPLAY=LAW),
            lecture('attended-other', YYZT='已预约', SFDK='1', HDLX_DISPLAY=OTHER,
                    JSSJ='2030-01-01 12:00:00')]
    current = progress(rows, 0)
    assert current.completed == {OTHER: 1}
    assert not current.planned


def test_overlapping_choice_fills_missing_category_before_earlier_finish():
    rows = attended(2) + [
        lecture('psychology', YYZT='已预约', SFDK='1'),
        lecture('art', YYZT='已预约', SFDK='1', HDLX_DISPLAY=ART, JSSJ='2030-01-01 12:00:00')]
    assert progress(rows, 0).completed == {PSYCHOLOGY: 1, LAW: 1, ART: 1}


def test_missing_categories_are_updated_after_each_choice():
    rows = [lecture('first-psychology', YYZT='已预约'),
            lecture('repeat-psychology', YYZT='已预约',
                    KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 11:00:00'),
            lecture('law', YYZT='已预约', HDLX_DISPLAY=LAW,
                    KSSJ='2030-01-02 10:00:00', JSSJ='2030-01-02 12:00:00')]
    assert progress(rows, 0).planned == {PSYCHOLOGY: 1, LAW: 1}


@pytest.mark.parametrize('psychology_end', ['2030-01-01 12:00:00', '2030-01-01 11:00:00'])
def test_overlap_ties_use_finish_then_activity_id_independent_of_input_order(psychology_end):
    rows = [lecture('z', YYZT='已预约', JSSJ=psychology_end),
            lecture('a', YYZT='已预约', HDLX_DISPLAY=LAW)]
    for ordered in (rows, list(reversed(rows))):
        assert progress(ordered, 0).planned == {LAW: 1}


def test_touching_endpoints_count_only_one_lecture():
    rows = [lecture('first', YYZT='已预约'),
            lecture('touching', YYZT='已预约',
                    KSSJ='2030-01-01 11:00:00', JSSJ='2030-01-01 12:00:00')]
    assert progress(rows, 0).planned == {PSYCHOLOGY: 1}


def test_unselected_ended_overlap_does_not_pause_confirmed_attendance():
    rows = [lecture('pending', YYZT='已预约'), lecture('checked', YYZT='已预约', SFDK='1')]
    current = progress(rows, float('inf'))
    assert current.completed == {PSYCHOLOGY: 1}
    assert not current.planned


@pytest.mark.parametrize('changes', [{'SFDK': 'unknown'}, {'HDLX_DISPLAY': '未知'}, {'KSSJ': 'invalid'}])
def test_unknown_overlapping_record_still_requires_validation(changes):
    rows = [lecture('checked', YYZT='已预约', SFDK='1'),
            lecture('unknown', YYZT='已预约', **changes)]
    with pytest.raises(ValueError):
        progress(rows, 0)


def test_unselected_reservation_still_blocks_new_conflicting_booking(tmp_path):
    school = School()
    school.mine = [lecture('selected', YYZT='已预约'),
                   lecture('unselected', YYZT='已预约',
                           KSSJ='2030-01-01 10:30:00', JSSJ='2030-01-01 11:30:00')]
    original = [dict(row) for row in school.mine]
    school.activities = [lecture('blocked', HDLX_DISPLAY=LAW,
                                 KSSJ='2030-01-01 11:10:00', JSSJ='2030-01-01 12:00:00'),
                         lecture('safe', HDLX_DISPLAY=ART,
                                 KSSJ='2030-01-01 12:10:00', JSSJ='2030-01-01 13:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['safe']
    assert school.mine[:2] == original
    assert not any('cancel' in url.lower() for _, url, _ in school.requests)


def test_recheck_completed_task_does_not_count_eight_overlapping_attendances(tmp_path):
    school = School()
    school.mine = attended()
    assert execute(school, tmp_path) == 0
    for row in school.mine:
        row.update(KSSJ='2020-01-01 10:00:00', JSSJ='2020-01-01 11:00:00')
    events = []
    assert execute(school, tmp_path, new_task=True, notify=events.append) == 130
    assert not events
    assert not school.submitted
    assert json.loads((tmp_path / 'task.json').read_text())['status'] == 'scanning'


def test_read_only_check_validates_attendance_schema_without_creating_state(tmp_path):
    school = School()
    school.mine = [lecture('unknown', YYZT='已预约', SFDK='unknown')]
    directory = tmp_path / 'unused'
    assert execute(school, directory, check_only=True) == 1
    assert not directory.exists() and not school.submitted


def test_future_reservation_with_missing_attendance_fields_counts_as_planned():
    row = {'WID': 'booking', 'HD_WID': 'future-art', 'FBZT': '1', 'SFDK': '0',
           'JZXL_DISPLAY': ART, 'JZSJ': '2026-10-27 18:30:00', 'HDJSSJ': '2026-10-27 20:30:00'}
    current = progress([row], 1793000000)
    assert current.planned == {ART: 1}
    assert not current.completed


def test_ended_reservation_with_missing_attendance_status_still_blocks():
    row = {'WID': 'booking', 'HD_WID': 'past-art', 'FBZT': '1', 'SFDK': None,
           'JZXL_DISPLAY': ART, 'JZSJ': '2020-01-01 10:00:00', 'HDJSSJ': '2020-01-01 11:00:00'}
    with pytest.raises(ValueError):
        progress([row], 1793000000)


def test_future_reservation_missing_breach_field_does_not_block_next_submission(tmp_path):
    school = School()
    school.mine = [{'WID': 'booking', 'HD_WID': 'future-art', 'FBZT': '1', 'SFDK': '0',
                    'JZXL_DISPLAY': ART, 'JZSJ': '2030-01-01 10:00:00', 'HDJSSJ': '2030-01-01 11:00:00'}]
    school.activities = [lecture('next-law', HDLX_DISPLAY=LAW,
                                 KSSJ='2030-01-01 12:00:00', JSSJ='2030-01-01 13:00:00')]
    assert execute(school, tmp_path) == 130
    assert school.submitted == ['next-law']


def test_legacy_one_success_reopens_and_preserves_wait_and_pending(tmp_path):
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
    assert not migrated['activity'] and migrated['retry_at'] == 200
    assert waits == [100] and not school.requests
    saved.update(status='pending', activity='uncertain', retry_at=0)
    path.write_text(json.dumps(saved))
    school.mine = attended()
    assert execute(school, tmp_path) == 130
    assert json.loads(path.read_text())['activity'] == 'uncertain'
    assert not school.submitted
