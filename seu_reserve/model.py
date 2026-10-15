"""Lecture data is accepted only when its meaning can be established."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


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
