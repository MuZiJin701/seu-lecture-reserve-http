import json

import pytest

from seu_reserve.model import moment, reservation_display
from test_app import School, execute, lecture


def test_current_display_keeps_all_future_bookings_including_salons_and_overlap():
    rows = [lecture('one', YYZT='已预约'), lecture('two', YYZT='已预约'),
            lecture('salon', YYZT='已预约', HDLX_DISPLAY='“SEU咖啡间”系列沙龙活动'),
            lecture('cancelled', YYZT='已取消'),
            lecture('past', YYZT='已预约', KSSJ='2025-01-01 10:00:00', JSSJ='2025-01-01 11:00:00')]
    now = moment('2026-10-30 10:00:00').timestamp()
    result = reservation_display(rows, now)
    assert {row['wid'] for row in result} == {'one', 'two', 'salon'}
    assert all(set(row) == {'wid', 'title', 'category', 'location', 'online', 'start', 'end', 'status'} for row in result)
    with pytest.raises(ValueError, match='attendance_record_conflict'):
        reservation_display([rows[0], {**rows[0], 'YYZT': '已取消'}], now)


def test_engine_persists_minimal_display_without_extra_school_queries_and_preserves_on_failure(tmp_path):
    school = School()
    school.mine = [lecture(YYZT='已预约')]
    assert execute(school, tmp_path) == 130
    saved = json.loads((tmp_path / 'task.json').read_text())['reservations']
    assert saved['items'][0]['wid'] == 'one'
    assert len([url for _, url, _ in school.requests if url.endswith('queryMyActivityList.do')]) == 1
    class BrokenSchool(School):
        def request(self, method, url, **options):
            if url.endswith('queryMyActivityList.do'):
                from requests import Timeout
                raise Timeout('synthetic')
            return super().request(method, url, **options)
    assert execute(BrokenSchool(), tmp_path) == 130
    assert json.loads((tmp_path / 'task.json').read_text())['reservations'] == saved


def test_display_maps_school_fields_and_orders_rows_by_start_time():
    rows = [
        lecture('later', HDMC='后一场', KSSJ='2030-01-01 14:00:00', JSSJ='2030-01-01 15:00:00',
                YYZT='已预约', JZDD='九龙湖校区', IS_ONLINE=False,
                HDLX_DISPLAY='人文与科学素养系列讲座_心理健康'),
        lecture('earlier', HDMC='【线上】前一场', KSSJ='2030-01-01 08:00:00', JSSJ='2030-01-01 09:00:00',
                YYZT='已预约', JZDD='线上', IS_ONLINE=False,
                HDLX_DISPLAY='人文与科学素养系列讲座-艺术类'),
    ]
    now = moment('2026-10-30 10:00:00').timestamp()
    result = reservation_display(rows, now)
    assert [row['wid'] for row in result] == ['earlier', 'later']
    earlier, later = result
    assert earlier == dict(
        wid='earlier', title='【线上】前一场', category='人文与科学素养系列讲座-艺术类',
        location='线上', online=True, start=moment('2030-01-01 08:00:00').timestamp(),
        end=moment('2030-01-01 09:00:00').timestamp(), status='reserved')
    assert later['title'] == '后一场' and later['category'] == '人文与科学素养系列讲座_心理健康'
    assert later['online'] is False and later['location'] == '九龙湖校区'
    assert later['start'] == moment('2030-01-01 14:00:00').timestamp()
    assert later['end'] == moment('2030-01-01 15:00:00').timestamp()
