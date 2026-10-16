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
COFFEE_CATEGORY = '“SEU咖啡间"系列沙龙活动'


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
        return (category in SERIES_CATEGORIES and (category in self.missing
                or sum(self.completed.values()) + sum(self.planned.values()) + len(self.missing) < 8))


def progress(rows: list[dict[str, Any]], now: float) -> Progress:
    completed: Counter[str] = Counter()
    planned: Counter[str] = Counter()
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
        category = row.get('JZXL_DISPLAY', row.get('HDLX_DISPLAY'))
        if not isinstance(category, str) or not category:
            raise ValueError('attendance_category_unknown')
        if category == COFFEE_CATEGORY:
            continue
        if category not in SERIES_CATEGORIES:
            raise ValueError('attendance_category_unknown')
        checked, breached = row.get('SFDK'), row.get('SFWY')
        if checked not in ('0', '1') or breached not in ('0', '1'):
            raise ValueError('attendance_status_unknown')
        end = interval(row)[1].timestamp()
        if breached == '1':
            continue
        if checked == '1':
            completed[category] += 1
        elif end > now:
            planned[category] += 1
        else:
            raise ValueError('attendance_result_pending')
    return Progress(completed, planned)


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

    @classmethod
    def read(cls, row: dict[str, Any]) -> 'Lecture':
        if row.get('YYZT') != '预约':
            raise ValueError('not_reservable')
        title, category = row.get('HDMC'), row.get('HDLX_DISPLAY')
        online, location = row.get('IS_ONLINE'), row.get('JZDD')
        # shortcut: accept explicit fields only; extend mappings after validating real activity responses.
        if not isinstance(title, str) or not isinstance(category, str) or not isinstance(online, bool):
            raise ValueError('lecture_schema_unknown')
        if not isinstance(location, str):
            raise ValueError('location_missing')
        match = re.match(r'^(.+?校区)', location)
        start, end = interval(row)
        return cls(activity_id(row), title, category, match[1] if match else location, online, start, end)
