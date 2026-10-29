"""Lecture data is accepted only when its meaning can be established."""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
SERIES_CATEGORIES = ('人文与科学素养系列讲座_心理健康', '人文与科学素养系列讲座_法律',
                     '人文与科学素养系列讲座-艺术类', '人文与科学素养系列讲座_其他')
REQUIRED_CATEGORIES = SERIES_CATEGORIES[:3]
COFFEE_CATEGORY = '“SEU咖啡间”系列沙龙活动'
GOAL_CATEGORIES = SERIES_CATEGORIES


def category_name(value: Any) -> Any:
    # The school response uses both ASCII and full-width closing quotes for the salon series.
    return COFFEE_CATEGORY if value == '“SEU咖啡间"系列沙龙活动' else value


def test_notice(row: dict[str, Any]) -> bool:
    title = row.get('HDMC', row.get('JZMC'))
    return isinstance(title, str) and any(word in title for word in ('系统测试', '请勿预约', '购票'))


@dataclass(frozen=True)
class Progress:
    completed: Counter[str]
    planned: Counter[str]

    @property
    def done(self) -> bool:
        return sum(self.completed.values()) >= 8 and all(self.completed[c] for c in REQUIRED_CATEGORIES)

    @property
    def missing(self) -> set[str]:
        return {c for c in REQUIRED_CATEGORIES if not self.completed[c] + self.planned[c]}

    def needs(self, category: str) -> bool:
        category = category_name(category)
        return ((category in GOAL_CATEGORIES or category == COFFEE_CATEGORY) and (category in self.missing
                or sum(self.completed.values()) + sum(self.planned.values()) + len(self.missing) < 8))


def progress(rows: list[dict[str, Any]], now: float) -> Progress:
    completed: Counter[str] = Counter()
    planned: Counter[str] = Counter()
    records: list[tuple[str, str, datetime, datetime, str]] = []
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        wid = activity_id(row)
        if wid in seen:
            if seen[wid] != row:
                raise ValueError('attendance_record_conflict')
            continue
        seen[wid] = row
        if 'FBZT' in row and row['FBZT'] not in ('1', '-1'):
            raise ValueError('publication_status_unknown')
        if not reserved(row) or row.get('FBZT') == '-1':
            continue
        if test_notice(row):
            continue
        category = category_name(row.get('JZXL_DISPLAY', row.get('HDLX_DISPLAY')))
        if not isinstance(category, str) or not category:
            raise ValueError('attendance_category_unknown')
        if category == COFFEE_CATEGORY:
            continue
        if category not in GOAL_CATEGORIES:
            raise ValueError('attendance_category_unknown')
        start, end = interval(row)
        checked, breached = row.get('SFDK'), row.get('SFWY')
        if checked not in ('0', '1') or breached not in ('0', '1'):
            # A freshly created reservation can arrive without attendance/breach fields (the
            # school fills them after the event). For a record that has not ended yet, a missing
            # field means "not attended/violated yet", so it counts as planned rather than
            # blocking every later booking on an impossible future attendance result.
            if end.timestamp() > now:
                if checked is None or checked == '':
                    checked = '0'
                if breached is None or breached == '':
                    breached = '0'
            if checked not in ('0', '1') or breached not in ('0', '1'):
                raise ValueError('attendance_status_unknown')
        if breached == '1':
            continue
        records.append((checked, category, start, end, wid))
    result = Progress(completed, planned)
    # shortcut: greedy priorities do not maximize all schedule combinations; revisit if optimal planning is needed.
    while records:
        missing = result.missing
        checked, category, start, end, _ = min(records, key=lambda record: (
            record[0] != '1', record[1] not in missing, record[3], record[4]))
        if checked == '1':
            completed[category] += 1
        elif end.timestamp() > now:
            planned[category] += 1
        else:
            raise ValueError('attendance_result_pending')
        records = [record for record in records if record[2] > end or record[3] < start]
    return result


def conflicting_records(rows: list[dict[str, Any]]) -> bool:
    """Whether one activity ID appears in two different records.

    ``progress`` raises ``attendance_record_conflict`` for this condition. Callers that read
    rows without going through ``progress`` (a pending-submission check, for example) must
    apply the same rule: contradictory records cannot be success or absence evidence.
    """
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            wid = activity_id(row)
        except ValueError:
            continue
        if wid in seen and seen[wid] != row:
            return True
        seen[wid] = row
    return False


def activity_id(row: dict[str, Any]) -> str:
    value = row.get('HD_WID', row.get('WID'))
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('activity_id_missing')
    return value


def moment(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError('time_missing')
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d/%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise ValueError('time_invalid')


def interval(row: dict[str, Any]) -> tuple[datetime, datetime]:
    start = moment(row.get('KSSJ', row.get('HDKSSJ', row.get('JZSJ'))))
    end = moment(row.get('JSSJ', row.get('HDJSSJ')))
    if end <= start:
        raise ValueError('interval_invalid')
    return start, end


def headcount(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def reserved(row: dict[str, Any]) -> bool:
    # The school UI identifies my reservations by HD_WID and publication status.
    if 'YYZT' not in row and isinstance(row.get('HD_WID'), str) and row['HD_WID']:
        if row.get('FBZT') == '1':
            return True
        if row.get('FBZT') == '-1':
            return False
    status = row.get('YYZT')
    if status in ('已预约', '预约成功', '取消预约'):
        return True
    if status in ('已取消', '取消成功', '未预约'):
        return False
    raise ValueError('reservation_status_unknown')


@dataclass(frozen=True)
class Lecture:
    wid: str
    title: str
    category: str
    campus: str
    online: bool
    start: datetime
    end: datetime
    availability: str
    reservation_start: datetime | None
    reservation_end: datetime | None

    @classmethod
    def read(cls, row: dict[str, Any], *, now: float) -> 'Lecture':
        native = 'YYZT' not in row or any(key in row for key in ('JZMC', 'JZXL_DISPLAY', 'YYKSSJ', 'YYJSSJ'))
        title = row.get('JZMC' if native else 'HDMC')
        category = category_name(row.get('JZXL_DISPLAY' if native else 'HDLX_DISPLAY'))
        location = row.get('JZDD')
        if not isinstance(title, str) or not title.strip() or not isinstance(category, str) or not category.strip():
            raise ValueError('lecture_schema_unknown')
        if not isinstance(location, str) or not location.strip():
            raise ValueError('location_missing')
        opens = closes = None
        if native:
            if not title.startswith(('【线上】', '【线下】')) or row.get('FBZT') not in ('1', '-1'):
                raise ValueError('lecture_schema_unknown')
            online = title.startswith('【线上】')
            try:
                opens, closes = moment(row.get('YYKSSJ')), moment(row.get('YYJSSJ'))
            except ValueError:
                raise ValueError('lecture_schema_unknown') from None
            if closes <= opens or row.get('YY_WID') is not None and not isinstance(row['YY_WID'], str):
                raise ValueError('lecture_schema_unknown')
            # The school UI decides availability from the server clock and the joined head counts.
            if row['FBZT'] != '1':
                availability = 'not_reservable'
            elif row.get('YY_WID'):
                availability = 'already_reserved'
            else:
                server_now = row.get('_SERVER_NOW')
                try:
                    current = moment(server_now).timestamp() if server_now else now
                except ValueError:
                    raise ValueError('lecture_schema_unknown') from None
                taken, capacity = headcount(row.get('YYRS')), headcount(row.get('HDZRS'))
                if current < opens.timestamp():
                    availability = 'reservation_not_started'
                elif current > closes.timestamp():
                    availability = 'reservation_ended'
                elif taken is not None and capacity is not None and taken >= capacity:
                    availability = 'capacity_full'
                else:
                    availability = 'candidate'
        else:
            explicit_online = row.get('IS_ONLINE')
            if not isinstance(explicit_online, bool) or row['YYZT'] not in (
                    '预约', '已预约', '预约成功', '取消预约', '已取消', '取消成功', '未预约',
                    '未开放', '已结束', '已满', '活动取消'):
                raise ValueError('lecture_schema_unknown')
            online = explicit_online
            availability = {'预约': 'candidate', '未开放': 'reservation_not_started',
                            '已结束': 'reservation_ended', '已满': 'capacity_full',
                            '已预约': 'already_reserved', '预约成功': 'already_reserved',
                            '取消预约': 'already_reserved'}.get(row['YYZT'], 'not_reservable')
        wid = activity_id(row)
        match = re.match(r'^(.+?校区)', location)
        start, end = interval(row)
        return cls(wid, title, category, match[1] if match else location, online, start, end,
                   availability, opens, closes)
