import hashlib
import json

import pytest

from seu_reserve.model import SERIES_CATEGORIES, progress
from test_app import School, execute, lecture


PSYCHOLOGY, LAW, ART, OTHER = SERIES_CATEGORIES


def attended(count=8):
    return [lecture(f'history-{i}', YYZT='已预约', SFDK='1',
                    HDLX_DISPLAY=SERIES_CATEGORIES[i % 4],
                    KSSJ='2020-01-01 10:00:00', JSSJ='2020-01-01 11:00:00')
            for i in range(count)]


def test_only_eight_attended_with_all_required_categories_complete_goal(tmp_path):
    school = School()
    school.mine = attended()
    events = []
    assert execute(school, tmp_path, notify=events.append) == 0
    assert events == ['goal_completed']
    saved = json.loads((tmp_path / 'task.json').read_text())
    assert saved['version'] == 2 and saved['status'] == 'done'
    assert not saved['activity']
    requests_before_restart = len(school.requests)
    assert execute(school, tmp_path) == 0
    assert len(school.requests) == requests_before_restart
    assert execute(school, tmp_path, new_task=True) == 0
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


def test_coffee_is_excluded_even_with_legacy_explicit_configuration(tmp_path):
    from seu_reserve.app import Settings
    coffee = '“SEU咖啡间"系列沙龙活动'
    school = School()
    school.activities = [lecture(HDLX_DISPLAY=coffee)]
    assert execute(school, tmp_path, settings=Settings(state_dir=tmp_path, categories=(coffee,))) == 130
    assert not school.submitted
    school.mine = attended(7) + [lecture('coffee-history', YYZT='已预约', SFDK='1', HDLX_DISPLAY=coffee)]
    assert not progress(school.mine, 0).done


def test_real_school_fields_are_counted_and_duplicates_are_not():
    rows = [{'WID': f'booking-{i}', 'HD_WID': f'activity-{i}', 'FBZT': '1',
             'SFDK': '1', 'SFWY': '0', 'JZXL_DISPLAY': SERIES_CATEGORIES[i % 4],
             'JZSJ': '2020-01-01 10:00:00', 'HDJSSJ': '2020-01-01 11:00:00'} for i in range(8)]
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


def test_read_only_check_validates_attendance_schema_without_creating_state(tmp_path):
    school = School()
    school.mine = [lecture('unknown', YYZT='已预约', SFDK=None)]
    directory = tmp_path / 'unused'
    assert execute(school, directory, check_only=True) == 1
    assert not directory.exists() and not school.submitted


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
