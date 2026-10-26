"""Application entry point: one task, with replaceable external dependencies."""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import requests

from .client import AuthenticationExpired, AuthenticationRejected, Client, RemoteError, has_code
from .model import (GOAL_CATEGORIES, Lecture, Progress, REQUIRED_CATEGORIES, SHANGHAI,
                    activity_id, category_name, interval, moment, progress, reserved,
                    test_notice)
from .mail import Mailer, mail_failure_reason, mail_history
from .state import Task, open_nofollow

DEFAULT_CATEGORIES = GOAL_CATEGORIES
QUERY_FAILURE_SECONDS = 5 * 60
STEP_FAILURE_SECONDS = 5 * 60
MAJOR_MAIL_EVENTS = {'authentication_required', 'query_failure', 'task_failed', 'unexpected_failure',
                     'reservation_step_failure'}
SCAN_INTERVAL = 10
FAST_INTERVAL = 0.25
FAST_RETRY_REASONS = {'network_timeout', 'connect_timeout', 'network_failure', 'connection_failure',
                      'proxy_failure', 'invalid_json', 'invalid_response', 'http_failure',
                      'business_response_invalid', 'business_rows_invalid', 'business_rows_incomplete'}
SELECTION_REASONS = {'candidate', 'reservation_not_started', 'reservation_ended', 'capacity_full',
    'not_reservable', 'already_reserved', 'category_not_allowed', 'category_not_needed',
    'title_not_selected', 'online_disabled', 'offline_disabled', 'campus_not_selected',
    'time_conflict', 'cooldown', 'invalid_activity_time', 'notice_test_only'}
# A pending submission is verified repeatedly by activity ID before any automatic release: the
# submission is non-idempotent, so absence only becomes actionable after a short series of valid
# checks. Automatic release keeps unattended operation possible; the local log keeps the evidence.
PENDING_MISS_PAUSE = 2
PENDING_AUTO_RELEASE_CHECKS = 3
# After the candidate appears, a slow successful submission must not be aborted by the fast list
# timeout; captcha and submit keep a longer budget while the list stays fast.
SUBMIT_TIMEOUT = 8
# A list query during the opening window must survive a briefly overloaded server instead of
# aborting at 3 s; the timeout still bounds a dead connection and the fast retry re-joins quickly.
OPENING_QUERY_TIMEOUT = 6
# During the opening window, reuse the history fetched immediately before it instead of
# synchronously refreshing at the opening instant; the trade-off is bounded stale conflict data.
OPENING_HISTORY_TTL = 60.0
# 502/503/504 mean the server is overloaded, not that a rate limit is in force; recover in seconds
# during an opening instead of the 20-40-80 s limiter backoff. Explicit Retry-After still wins.
OVERLOAD_RETRY_CAP = 5.0


def terminal_prompt(label: str) -> str:
    if not sys.stdin.isatty():
        print('event=interactive_terminal_required', flush=True)
        raise EOFError
    return getpass(label)


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path('state')
    titles: tuple[str, ...] = ()
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    campuses: tuple[str, ...] = ()
    online: bool = True
    offline: bool = True
    mail_success: bool = False

def select_candidates(rows: list[dict[str, Any]], settings: Settings, current: Progress,
                      mine: list[dict[str, Any]], cooldowns: Mapping[str, float],
                      now: float, *, cooldown_clock_known: bool = True
                      ) -> tuple[list[Lecture], list[dict[str, Any]]]:
    booked = {activity_id(row) for row in mine if reserved(row)}
    conflicts = [interval(row) for row in mine if reserved(row)]
    candidates: list[Lecture] = []
    details: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    categories = tuple(category_name(value) for value in settings.categories)
    for row in rows:
        category = category_name(row.get('JZXL_DISPLAY', row.get('HDLX_DISPLAY')))
        if test_notice(row):
            title = row.get('JZMC', row.get('HDMC'))
            details.append(dict(title=title if isinstance(title, str) else '',
                                reason='notice_test_only',
                                reservation_start=None, reservation_end=None))
            continue
        if isinstance(category, str) and category.strip() and category not in categories:
            window: dict[str, str | None] = {}
            for key, field in (('reservation_start', 'YYKSSJ'), ('reservation_end', 'YYJSSJ')):
                try:
                    window[key] = (moment(row[field]).isoformat(timespec='seconds')
                                   if row.get(field) is not None else None)
                except ValueError:
                    window[key] = None
            title = row.get('JZMC', row.get('HDMC'))
            details.append(dict(title=title if isinstance(title, str) else '',
                                reason='category_not_allowed', **window))
            continue
        try:
            wid = activity_id(row)
            if wid in seen:
                if seen[wid] != row:
                    raise ValueError('conflicting_activity')
                continue
            seen[wid] = row
            lecture = Lecture.read(row, now=now)
        except ValueError as exc:
            if str(exc) not in ('time_missing', 'time_invalid', 'interval_invalid'):
                raise RemoteError('activity_schema_unknown') from None
            window = {key: moment(row[field]).isoformat(timespec='seconds') if row.get(field) is not None else None
                      for key, field in (('reservation_start', 'YYKSSJ'), ('reservation_end', 'YYJSSJ'))}
            details.append(dict(title=row.get('JZMC', row.get('HDMC')),
                                reason='invalid_activity_time', **window))
            continue
        if lecture.availability != 'candidate':
            reason = lecture.availability
        elif lecture.wid in booked:
            reason = 'already_reserved'
        elif not current.needs(lecture.category):
            reason = 'category_not_needed'
        elif settings.titles and lecture.title not in settings.titles:
            reason = 'title_not_selected'
        elif lecture.online and not settings.online:
            reason = 'online_disabled'
        elif not lecture.online and not settings.offline:
            reason = 'offline_disabled'
        elif not lecture.online and lecture.campus not in settings.campuses:
            reason = 'campus_not_selected'
        elif any(lecture.start <= end and lecture.end >= start for start, end in conflicts):
            reason = 'time_conflict'
        elif lecture.wid in cooldowns and (not cooldown_clock_known or cooldowns[lecture.wid] > now):
            reason = 'cooldown'
        else:
            reason = 'candidate'
            candidates.append(lecture)
        details.append(dict(title=lecture.title, reason=reason,
            reservation_start=lecture.reservation_start.isoformat(timespec='seconds') if lecture.reservation_start else None,
            reservation_end=lecture.reservation_end.isoformat(timespec='seconds') if lecture.reservation_end else None))
    candidates.sort(key=lambda row: (row.category not in current.missing,
                    categories.index(row.category), not row.online, row.start, row.wid))
    return candidates, details


def opening_windows(details: list[dict[str, Any]]) -> list[tuple[float, float]]:
    openings: list[tuple[float, float]] = []
    for item in details if isinstance(details, list) else []:
        if not isinstance(item, dict) or item.get('reason') not in (
                'reservation_not_started', 'candidate', 'capacity_full', 'not_reservable'):
            continue
        try:
            start = datetime.fromisoformat(item['reservation_start'])
            end = (datetime.fromisoformat(item['reservation_end']) if item.get('reservation_end')
                   else start + timedelta(minutes=10))
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                continue
            openings.append((start.timestamp(), min(end.timestamp(), start.timestamp() + 600)))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return openings


def merge_openings(known: list[tuple[float, float]], details: list[dict[str, Any]],
                   now: float) -> list[tuple[float, float]]:
    """Keep known, unexpired windows even when the latest valid list happens to be empty."""
    merged = {window for window in known if window[1] >= now - 1}
    merged.update(opening_windows(details))
    return sorted(merged)[:128]


def scan_delay(details: list[dict[str, Any]], now: float, elapsed: float,
               interval: float, known: list[tuple[float, float]] | None = None) -> tuple[float, bool]:
    openings = opening_windows(details) if known is None else known
    fast = any(start - 30 <= now <= end for start, end in openings)
    delay = max(0, min(interval, FAST_INTERVAL) - elapsed) if fast else interval
    boundaries = [start - 30 - now for start, _ in openings if start - 30 > now]
    return min([delay, *boundaries]), fast


def _school_offset(rows: list[dict[str, Any]], now: float) -> float | None:
    server_now = next((row.get('_SERVER_NOW') for row in rows
                       if isinstance(row.get('_SERVER_NOW'), str)), None)
    if not server_now:
        return None
    try:
        offset = moment(server_now).timestamp() - now
    except ValueError:
        return None
    # A day-scale offset means a broken clock or a test fixture, not useful scheduling input.
    return offset if abs(offset) <= 3600 else None


def school_clock_offset(rows: list[dict[str, Any]], now: float) -> float:
    """Estimate the school clock offset so opening windows use the school's time, not the host's."""
    offset = _school_offset(rows, now)
    return 0.0 if offset is None else offset


def school_clock_known(rows: list[dict[str, Any]], now: float) -> bool:
    """Whether this response carries a usable school clock for time-basis decisions."""
    return _school_offset(rows, now) is not None


def _state_event(settings: Settings, event: str) -> None:
    print('event=' + event, flush=True)
    try:
        fd = open_nofollow(settings.state_dir / 'events.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, 'a') as target:
            target.write(f'{time.time():.0f} event={event}\n')
    except OSError:
        pass


def resolve_pending(settings: Settings, *, session: Any = None,
                    environ: Mapping[str, str] | None = None,
                    prompt: Callable[[str], str] = terminal_prompt) -> int:
    """Explicit operator resolution of an uncertain submission; never submits anything."""
    environ = os.environ if environ is None else environ
    created_session = session is None
    task = Task(settings.state_dir, environ.get('SEU_USERNAME', ''))
    client: Client | None = None
    try:
        client = Client(session or requests.Session(), environ, prompt, lambda _: None)
        if task.data['status'] != 'pending':
            _state_event(settings, 'pending_not_found')
            return 1
        pending_wid = task.data['activity']
        try:
            client.authenticate()
            history = client.mine()
            booked = {activity_id(row) for row in history if reserved(row)}
        except (RemoteError, ValueError):
            _state_event(settings, 'pending_check_failed')
            return 1
        if pending_wid in booked:
            task.data.update(status='scanning', activity='')
            task.data.pop('pending_checks', None)
            task.data.pop('pending_since', None)
            task.save()
            _state_event(settings, 'pending_confirmed')
            return 0
        _state_event(settings, 'pending_not_in_history')
        answer = prompt('学校记录仍未显示该预约。请先人工核对“我的活动”；确认原提交未生效并接受可能'
                        '重复预约的风险后，输入 confirm 清除待确认状态（其他输入取消）：')
        if answer.strip() != 'confirm':
            _state_event(settings, 'pending_resolution_cancelled')
            return 1
        task.data.update(status='scanning', activity='')
        task.data.pop('pending_checks', None)
        task.data.pop('pending_since', None)
        task.save()
        _state_event(settings, 'pending_released_by_operator')
        return 0
    finally:
        task.close()
        if created_session and client is not None:
            client.session.close()


def run(settings: Settings, *, session: Any = None,
        environ: Mapping[str, str] | None = None, prompt: Callable[[str], str] = terminal_prompt,
        notify: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        recognize: Callable[[bytes], str] | None = None,
        check_only: bool = False, new_task: bool = False) -> int:
    environ = os.environ if environ is None else environ
    announced: set[str] = set()
    alerts: dict[str, float] = {}
    deferred_mail: list[str] = []
    mail_blocked = False
    latest_progress: Progress | None = None
    progress_at = 0.0
    task: Task | None = None
    client = None
    mail_state_ready = False
    final_phase = 'stopped'
    stage = 'configuration'
    snapshot: dict[str, Any] = dict(phase='starting', last_query_at=0, last_error='none',
        last_error_at=0, failure_count=0, next_check_at=0, mail_status='disabled',
        last_mail_at=0, last_mail_status='never', last_mail_error='none', last_mail_event='none',
        last_mail_duration_seconds=None, activity_count=None, candidate_count=None)
    def health(**changes: Any) -> None:
        snapshot.update(changes, updated_at=clock())
        if task is not None:
            task.data['health'] = dict(snapshot)
            try:
                task.save()
            except (OSError, ValueError):
                print('event=health_write_failed', flush=True)
    def record(event: str) -> None:
        print(('' if check_only else f'{clock():.0f} ') + 'event=' + event, flush=True)
        if check_only:
            return
        try:
            fd = open_nofollow(settings.state_dir / 'events.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(fd, 'a') as target:
                target.write(f'{clock():.0f} event={event}\n')
        except OSError:
            print('event=local_log_failed', flush=True)
    def diagnostic(event: str, **fields: Any) -> None:
        # Detailed history stays in the private state directory, never SMTP or stdout.
        if check_only:
            return
        try:
            fd = open_nofollow(settings.state_dir / 'diagnostics.jsonl',
                               os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(fd, 'a') as target:
                target.write(json.dumps(dict(at=clock(), event=event, **fields),
                                        ensure_ascii=False, allow_nan=False) + '\n')
        except (OSError, ValueError):
            record('diagnostic_write_failed')
    def send_mail(event: str) -> None:
        if notify is None:
            return
        health(mail_status='sending', last_mail_status='sending', last_mail_error='none',
               last_mail_at=clock(), last_mail_event=event, last_mail_duration_seconds=None)
        # shortcut: SMTP blocks scanning; add a worker if measured send times cause missed scans.
        started = time.monotonic()
        try:
            notify(event)
            health(mail_status='accepted', last_mail_status='accepted', last_mail_at=clock())
        except Exception as exc:
            reason = mail_failure_reason(exc)
            record('notification_failed reason=' + reason)
            health(mail_status='failed', last_mail_status='failed', last_mail_error=reason, last_mail_at=clock())
        finally:
            duration = round(max(0, time.monotonic() - started), 3)
            health(last_mail_duration_seconds=duration)
            record(f'notification_finished mail_event={event} duration_seconds={duration}')

    def flush_mail() -> None:
        """Send alerts held back during an opening window; recovered failures never go out."""
        while deferred_mail:
            event = deferred_mail.pop(0)
            if event in ('query_failure', 'reservation_step_failure') and event not in announced:
                record('notification_skipped_recovered')
                continue
            send_mail(event)

    def send_success_mail(event: str) -> None:
        """Send success mail off the scanning thread so SMTP latency cannot delay reservations."""
        if notify is None:
            return
        notify_success = notify
        def worker() -> None:
            try:
                notify_success(event)
            except Exception as exc:
                print('event=notification_failed reason=' + mail_failure_reason(exc), flush=True)
        threading.Thread(target=worker, name='success-mail', daemon=True).start()

    def emit(event: str) -> None:
        record(event)
        if check_only:
            return
        if event == 'reservation_succeeded':
            # Success is not a deduplicated major alert: each confirmed reservation may notify.
            # It is sent from a worker thread; scanning must not wait for SMTP.
            if settings.mail_success and notify is not None:
                send_success_mail(event)
            return
        if event not in MAJOR_MAIL_EVENTS:
            return
        alert = 'task_failed' if event == 'unexpected_failure' else event
        if alert in announced:
            return
        announced.add(alert)
        if notify and task is not None and mail_state_ready:
            alerts[alert] = clock()
            task.save()
        if notify:
            if mail_blocked and event in ('query_failure', 'reservation_step_failure'):
                # A 20 s SMTP send during the opening window can cost the seats that caused the
                # failures; hold the alert until the fast window ends and skip it if recovered.
                deferred_mail.append(event)
                record('notification_deferred')
                return
            send_mail(event)

    def clear_alert(event: str) -> None:
        announced.discard(event)
        if task is not None and event in alerts:
            del alerts[event]
            task.save()

    def query_failed(started: float) -> None:
        if task is not None and task.data.get('query_failure_since') is None:
            task.data['query_failure_since'] = started
            task.save()

    def query_recovered() -> None:
        if task is not None and task.data.pop('query_failure_since', None) is not None:
            task.save()
        if 'query_failure' in announced or 'query_failure' in alerts:
            clear_alert('query_failure')
            emit('query_recovered')

    def step_failed(started: float) -> None:
        if task is not None and task.data.get('step_failure_since') is None:
            task.data['step_failure_since'] = started
            task.save()

    def step_recovered() -> None:
        if task is not None and task.data.pop('step_failure_since', None) is not None:
            task.save()
        if 'reservation_step_failure' in announced or 'reservation_step_failure' in alerts:
            clear_alert('reservation_step_failure')
            record('reservation_step_recovered')

    def notification_details() -> str:
        counts = '已选听：未知\n待参加：未知\n必需类别缺口：未知'
        if latest_progress is not None:
            missing = '、'.join(label for category, label in zip(REQUIRED_CATEGORIES, ('心理健康', '法律', '艺术'))
                               if category in latest_progress.missing) or '无（含待参加计划）'
            counts = (f'已选听：{sum(latest_progress.completed.values())} / 8\n'
                      f'待参加：{sum(latest_progress.planned.values())}\n必需类别缺口：{missing}')
        progress_time = datetime.fromtimestamp(progress_at, SHANGHAI).isoformat(timespec='seconds') if progress_at else '尚未取得'
        query_time = (datetime.fromtimestamp(snapshot['last_query_at'], SHANGHAI).isoformat(timespec='seconds')
                      if snapshot['last_query_at'] else '尚无成功查询')
        issue = ('预约结果待确认，已暂停其他提交' if task is not None and task.data['status'] == 'pending'
                 else snapshot['last_error'] if snapshot['failure_count'] else '无')
        next_check = (datetime.fromtimestamp(snapshot['next_check_at'], SHANGHAI).isoformat(timespec='seconds')
                      if snapshot['next_check_at'] else '尚未安排或任务已停止')
        return (f'{counts}\n进度记录时间：{progress_time}\n最近成功查询：{query_time}\n'
                   f'连续失败：{snapshot["failure_count"]} 次\n待处理问题：{issue}\n'
                   f'最近业务错误：{snapshot["last_error"]}\n下次检查计划：{next_check}\n'
                   '进度为最近一次成功核对的记录；未知或较旧时请检查本地运行状态。')

    def pause(seconds: float) -> None:
        while True:
            duration = seconds
            for event, since, limit in (
                    ('query_failure', task.data.get('query_failure_since') if task else None,
                     QUERY_FAILURE_SECONDS),
                    ('reservation_step_failure', task.data.get('step_failure_since') if task else None,
                     STEP_FAILURE_SECONDS)):
                if notify is not None and since is not None and event not in announced:
                    remaining = since + limit - clock()
                    if remaining <= 0:
                        emit(event)
                    else:
                        duration = min(duration, remaining)
            if seconds <= 0:
                return
            sleep(duration)
            seconds -= duration
    def ask(label: str) -> str:
        health(phase='manual_required', next_check_at=0)
        return prompt(label)
    try:
        if not check_only:
            if notify is None and Mailer.configured(environ):
                try:
                    mailer = Mailer(environ)
                    notify = lambda event: mailer(event, notification_details())
                except ValueError:
                    print('event=mail_configuration_invalid', flush=True)
                    print('邮件配置不完整或格式错误。QQ、163、126、Gmail 只需在 .env 中填写 SMTP_USERNAME（完整邮箱地址）'
                          '和 SMTP_PASSWORD（授权码或应用专用密码）；其他邮箱需指定 SMTP_HOST。'
                          '高级配置请检查 SMTP_PORT、MAIL_FROM、MAIL_TO。清空邮件配置可关闭通知。', flush=True)
                    return 1
            snapshot['mail_status'] = 'ready' if notify else 'disabled'
        client = Client(session or requests.Session(), environ, ask, emit)
        if check_only:
            stage = 'authentication'
            client.authenticate()
            stage = 'my_activities'
            mine = client.mine()
            current = progress(mine, clock())
            stage = 'activities'
            _, selection_details = select_candidates(client.activities(), settings, current, mine, {}, clock())
            print('activity_details=' + json.dumps(selection_details, ensure_ascii=False), flush=True)
            stage = 'captcha'
            client.captcha()
            print('event=read_only_complete', flush=True)
            return 0
        task = Task(settings.state_dir, environ.get('SEU_USERNAME', ''))
        alerts = task.data.setdefault('alerts', {})
        if notify:
            announced.update(alerts)
        previous = task.data.get('health', {})
        if isinstance(previous, dict):
            snapshot.update(mail_history(previous))
            if notify and snapshot['last_mail_status'] in ('accepted', 'failed', 'sending'):
                snapshot['mail_status'] = snapshot['last_mail_status']
        mail_state_ready = True
        health(phase='starting')
        if new_task and task.data['status'] != 'done':
            raise ValueError('active_task_cannot_be_reset')
        record('task_started')
        checkpoint = task
        def defer(seconds: float) -> None:
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.save()
            health(phase='backoff', next_check_at=checkpoint.data['retry_at'])
            pause(seconds)
            checkpoint.data['retry_at'] = 0
            checkpoint.save()
        remaining = task.data.get('retry_at', 0) - clock()
        if remaining > 0:
            defer(remaining)
        authenticated = False
        failures = 0
        unconfirmed_checks = 0
        last_timing_at = 0.0
        server_offset = 0.0
        offset_known = False
        history: list[dict[str, Any]] | None = None
        mine_at = 0.0
        progress_probe_attempted = False
        previous_selection = task.data.get('selection')
        details: list[dict[str, Any]] = (previous_selection.get('activities', [])
                                         if isinstance(previous_selection, dict) else [])
        known_openings: list[tuple[float, float]] = opening_windows(details)
        while True:
            try:
                scan_started = clock()
                school_now = clock() + server_offset
                known_openings = [window for window in known_openings if window[1] >= school_now - 1]
                opening_scan = scan_delay(details, school_now, 0, SCAN_INTERVAL, known_openings)[1]
                client.timeout = OPENING_QUERY_TIMEOUT if opening_scan else 10
                mail_blocked = opening_scan
                if not authenticated:
                    health(phase='authentication', next_check_at=0)
                    client.authenticate()
                    authenticated = True
                    clear_alert('authentication_required')
                    record('authentication_succeeded')
                confirmed = False
                mine_seconds = 0.0
                refresh_mine = False
                if task.data['status'] == 'pending':
                    # While a submission is unconfirmed, verify by activity ID and never submit.
                    health(phase='my_activities', next_check_at=0)
                    mine_started = clock()
                    history = client.mine()
                    mine_at = clock()
                    mine_seconds = max(0, clock() - mine_started)
                    refresh_mine = True
                    booked = {activity_id(row) for row in history if reserved(row)}
                    pending_wid = task.data['activity']
                    if pending_wid in booked:
                        confirmed = True
                    else:
                        failures = 0
                        health(last_query_at=clock(), failure_count=0)
                        query_recovered()
                        if task.data.get('pending_since') is None:
                            task.data['pending_since'] = clock()
                        unconfirmed_checks = int(task.data.get('pending_checks', 0)) + 1
                        task.data['pending_checks'] = unconfirmed_checks
                        task.save()
                        diagnostic('reservation_checked', wid=pending_wid, checks=unconfirmed_checks)
                        if unconfirmed_checks == 1:
                            emit('reservation_unconfirmed')
                        if unconfirmed_checks >= PENDING_AUTO_RELEASE_CHECKS:
                            # Unattended operation: after several valid checks the original
                            # submission is treated as not applied and scanning resumes. The
                            # school is expected to reject duplicate reservations for the same
                            # activity; the evidence stays in the private diagnostic log.
                            task.data.update(status='scanning', activity='')
                            task.data.pop('pending_checks', None)
                            task.data.pop('pending_since', None)
                            task.save()
                            clear_alert('reservation_unconfirmed')
                            diagnostic('reservation_unconfirmed_released', wid=pending_wid,
                                       checks=unconfirmed_checks)
                            record('reservation_unconfirmed_released')
                            unconfirmed_checks = 0
                            continue
                        pause_value = PENDING_MISS_PAUSE
                        health(phase='confirming', next_check_at=clock() + pause_value)
                        pause(pause_value)
                        continue
                    if confirmed:
                        diagnostic('reservation_confirmed', wid=pending_wid)
                        task.data.update(status='scanning', activity='')
                        task.data.pop('pending_checks', None)
                        task.data.pop('pending_since', None)
                        task.save()
                        clear_alert('reservation_unconfirmed')
                        health(last_query_at=clock(), failure_count=0)
                        step_recovered()
                        emit('reservation_succeeded')
                        unconfirmed_checks = 0
                # Query the opening-critical list before the slower history refresh: a slow or
                # failing my-activities endpoint must not hide the next reservation opening.
                health(phase='activities')
                activities_started = clock()
                rows = client.activities()
                activities_seconds = max(0, clock() - activities_started)
                if school_clock_known(rows, clock()):
                    server_offset = school_clock_offset(rows, clock())
                    offset_known = True
                # Prune cooldowns only after the school clock is known: on restart the stored
                # deadlines are in school time, and pruning them against an uncalibrated local
                # clock would delete still-valid cooldowns when the school clock runs behind.
                if offset_known:
                    school_now = clock() + server_offset
                    fresh = {wid: until for wid, until in task.data['cooldowns'].items()
                             if until > school_now}
                    if len(fresh) != len(task.data['cooldowns']):
                        task.data['cooldowns'] = fresh
                        task.save()
                if not refresh_mine and not (
                        history is not None and opening_scan and 0 <= clock() - mine_at < OPENING_HISTORY_TTL):
                    health(phase='my_activities', next_check_at=0,
                           activity_count=None, candidate_count=None)
                    mine_started = clock()
                    history = client.mine()
                    mine_at = clock()
                    mine_seconds = max(0, clock() - mine_started)
                    refresh_mine = True
                assert history is not None
                current = progress(history, clock() + server_offset)
                latest_progress, progress_at = current, mine_at
                if current.done:
                    newly_completed = task.data['status'] != 'done'
                    task.data.update(status='done', activity='')
                    task.save()
                    final_phase = 'done'
                    health(phase='done', last_query_at=clock(), failure_count=0)
                    query_recovered()
                    clear_alert('task_failed')
                    if newly_completed:
                        emit('goal_completed')
                    return 0
                if task.data['status'] == 'done':
                    task.data.update(status='scanning', activity='')
                    task.save()
                if not any(current.needs(category) for category in settings.categories):
                    opening_scan = False
                    mail_blocked = False
                    flush_mail()
                    failures = 0
                    health(phase='waiting_for_attendance', last_query_at=clock(), failure_count=0,
                           next_check_at=clock() + 10)
                    query_recovered()
                    clear_alert('task_failed')
                    pause(10)
                    continue
                candidates, details = select_candidates(rows, settings, current, history,
                    task.data['cooldowns'], clock() + server_offset,
                    cooldown_clock_known=offset_known)
                query_recovered()
                if not candidates:
                    # No submission step can be attempted in this scan; do not keep an old
                    # captcha/submit failure timer alive while only list queries run.
                    step_recovered()
                known_openings = merge_openings(known_openings, details, clock() + server_offset)
                opening_scan = scan_delay(details, clock() + server_offset, 0, SCAN_INTERVAL,
                                          known_openings)[1]
                mail_blocked = opening_scan
                client.timeout = SUBMIT_TIMEOUT if opening_scan else 10
                previous_selection = task.data.get('selection')
                if (not isinstance(previous_selection, dict)
                        or previous_selection.get('activities') != details):
                    diagnostic('selection_changed', activities=details)
                task.data['selection'] = dict(updated_at=clock(), activities=details)
                health(last_query_at=clock(), failure_count=failures, activity_count=len(rows),
                       candidate_count=len(candidates), phase='captcha' if candidates else 'waiting')
                for lecture in candidates:
                    for attempt in range(4):
                        health(phase='captcha')
                        try:
                            binary = client.captcha()
                        except RemoteError as exc:
                            step_failed(clock())
                            diagnostic('captcha_error', wid=lecture.wid, reason=str(exc),
                                       attempt=attempt + 1)
                            raise
                        if recognize is None:
                            started = clock()
                            import ddddocr
                            recognize = ddddocr.DdddOcr(show_ad=False).classification
                            diagnostic('ocr_ready', duration_seconds=max(0, clock() - started))
                        code = recognize(binary).strip()
                        if not code or not code.isascii() or not code.isalnum() or len(code) > 16:
                            diagnostic('ocr_invalid', wid=lecture.wid, attempt=attempt + 1)
                            if attempt == 3:
                                task.data['cooldowns'][lecture.wid] = clock() + server_offset + 300
                                task.save()
                                diagnostic('captcha_cooldown', wid=lecture.wid, seconds=300)
                            continue
                        # Persist before sending: a crash must recover by querying, never resubmitting.
                        task.data.update(status='pending', activity=lecture.wid,
                                         pending_checks=0, pending_since=clock())
                        task.save()
                        unconfirmed_checks = 0
                        health(phase='submitting')
                        submission_started = clock()
                        diagnostic('submission_started', wid=lecture.wid, attempt=attempt + 1)
                        try:
                            reply = client.submit(lecture.wid, code)
                        except RemoteError as exc:
                            step_failed(clock())
                            diagnostic('submission_error', wid=lecture.wid, reason=str(exc),
                                       duration_seconds=max(0, clock() - submission_started))
                            health(last_error=str(exc), last_error_at=clock())
                            if str(exc) == 'connect_timeout':
                                # The connection never formed, so this submission was not sent;
                                # revert pending and retry rather than blocking the whole task.
                                task.data.update(status='scanning', activity='')
                                task.data.pop('pending_checks', None)
                                task.data.pop('pending_since', None)
                                task.save()
                                diagnostic('submission_connect_failed', wid=lecture.wid)
                                break
                            if isinstance(exc, AuthenticationExpired):
                                authenticated = False
                            defer(max(1, exc.retry_after) if opening_scan else max(20, exc.retry_after))
                            break
                        step_recovered()
                        message = reply.get('msg', '')
                        captcha_wrong = (isinstance(message, str) and '验证码' in message
                                         and any(word in message for word in ('错误', '不正确', '失效', '过期')))
                        unavailable = isinstance(message, str) and any(
                            word in message for word in ('已满', '名额已满', '名额不足', '不可预约',
                                                         '预约已结束', '预约未开始'))
                        outcome = ('reported_success' if has_code(reply, 0) else 'captcha_wrong' if captcha_wrong
                                   else 'unavailable' if unavailable else 'unconfirmed')
                        diagnostic('submission_reply', wid=lecture.wid, outcome=outcome,
                                   feedback=reply.get('feedback') if reply.get('feedback') in (
                                       'unrecognized_message', 'no_message') else None,
                                   http_status=reply.get('http_status') if type(reply.get('http_status')) is int
                                       and 100 <= reply['http_status'] <= 599 else None,
                                   duration_seconds=max(0, clock() - submission_started))
                        if (has_code(reply, 0) or type(reply.get('code')) is not int
                                or not (captcha_wrong or unavailable)):
                            break
                        task.data.update(status='scanning', activity='')
                        if captcha_wrong and attempt == 3:
                            task.data['cooldowns'][lecture.wid] = clock() + server_offset + 300
                            diagnostic('captcha_cooldown', wid=lecture.wid, seconds=300)
                        task.save()
                        if unavailable:
                            break
                    if task.data['status'] == 'pending':
                        break
                failures = 0
                clear_alert('task_failed')
                if task.data['status'] != 'pending':
                    elapsed = max(0, clock() - scan_started)
                    delay, fast = scan_delay(details, clock() + server_offset, elapsed, SCAN_INTERVAL,
                                             known_openings)
                    if (fast != opening_scan or fast or not last_timing_at
                            or clock() - last_timing_at >= 300):
                        diagnostic('scan_timing', mode='opening' if fast else 'normal',
                                   duration_seconds=elapsed, wait_seconds=delay,
                                   history_reused=not refresh_mine,
                                   mine_seconds=mine_seconds, activities_seconds=activities_seconds)
                        last_timing_at = clock()
                    opening_scan = fast
                    if not fast:
                        flush_mail()
                    health(phase='waiting', next_check_at=clock() + delay, failure_count=0)
                    query_recovered()
                    pause(delay)
                else:
                    health(phase='confirming', failure_count=0)
            except AuthenticationRejected:
                raise
            except AuthenticationExpired:
                history = None
                authenticated = False
                unconfirmed_checks = 0
                failures += 1
                query_failed(scan_started)
                health(last_error='authentication_expired', last_error_at=clock(), failure_count=failures)
                defer(min(300, 10 * 2 ** min(failures, 5)))
            except (RemoteError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    # A structurally invalid history is not a cache we can keep using.
                    history = None
                unconfirmed_checks = 0
                reason = str(exc)
                safe_reason = reason if isinstance(exc, RemoteError) or reason in (
                    'attendance_result_pending', 'attendance_category_unknown',
                    'attendance_status_unknown', 'attendance_record_conflict') else 'invalid_activity_data'
                record(safe_reason)
                query_failed(scan_started)
                # Never issue another request before a rate-limit wait: the probe exists only to
                # fill progress for a later alert, not to bypass the server's Retry-After.
                server_directed_wait = (isinstance(exc, RemoteError)
                                        and (exc.retry_after > 0 or str(exc) == 'server_backoff'))
                if (authenticated and not progress_probe_attempted and latest_progress is None
                        and not server_directed_wait):
                    # One best-effort history read so a sustained query-failure alert can still
                    # report confirmed progress; never submit from this probe.
                    progress_probe_attempted = True
                    try:
                        history = client.mine()
                        mine_at = clock()
                        latest_progress = progress(history, clock() + server_offset)
                        progress_at = mine_at
                    except (RemoteError, ValueError):
                        history = None
                opening_scan = scan_delay(details, clock() + server_offset, 0, SCAN_INTERVAL,
                                          known_openings)[1]
                quick = (opening_scan and authenticated and isinstance(exc, RemoteError)
                         and reason in FAST_RETRY_REASONS and not exc.retry_after)
                retry = exc.retry_after if isinstance(exc, RemoteError) else 0
                if reason == 'service_unavailable':
                    # Temporary overload/gateway failure: recover within seconds, not the limiter
                    # 20-40-80 s schedule. An explicit Retry-After still takes precedence.
                    failures += 1
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    if retry > 0:
                        delay = retry
                    else:
                        delay = min(OVERLOAD_RETRY_CAP, 2.0 ** min(failures - 1, 3))
                elif quick:
                    # Opening seconds are scarce: retry transient request failures at the fast cadence.
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    delay = FAST_INTERVAL
                else:
                    failures += 1
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    delay = retry if retry > 0 else min(300, 10 * 2 ** min(failures, 5))
                defer(delay)
    except (KeyboardInterrupt, EOFError):
        return 130
    except (AuthenticationRejected, RemoteError, ValueError, OSError) as exc:
        final_phase = 'failed'
        health(last_error=str(exc) if isinstance(exc, RemoteError) else 'configuration_or_state_invalid',
               last_error_at=clock())
        failure_details = ''
        if check_only:
            reason = str(exc) if isinstance(exc, RemoteError) else type(exc).__name__
            failure_details = f' stage={stage} reason={reason}'
            if isinstance(exc, RemoteError) and exc.http_status is not None:
                failure_details += f' http_status={exc.http_status}'
            if isinstance(exc, RemoteError) and exc.host is not None:
                failure_details += f' host={exc.host}'
            if isinstance(exc, RemoteError) and exc.tls_reason is not None:
                failure_details += f' tls_reason={exc.tls_reason}'
            if isinstance(exc, RemoteError) and exc.proxy_used is not None:
                failure_details += f' proxy_used={str(exc.proxy_used).lower()}'
        if check_only:
            print('event=task_failed' + failure_details, flush=True)
        elif isinstance(exc, AuthenticationRejected):
            record('task_failed')
        else:
            emit('task_failed')
        if not check_only:
            mail_blocked = False
            flush_mail()
        return 1
    except Exception:
        final_phase = 'failed'
        health(last_error='unexpected_failure', last_error_at=clock())
        if check_only:
            print('event=unexpected_failure', flush=True)
        else:
            emit('unexpected_failure')
            mail_blocked = False
            flush_mail()
        return 1
    finally:
        if task:
            health(phase='manual_required' if snapshot['phase'] == 'manual_required' and final_phase == 'stopped'
                   else final_phase, next_check_at=0)
            task.close()
        if session is None and client is not None:
            client.session.close()
