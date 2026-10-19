"""Application entry point: one task, with replaceable external dependencies."""

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import requests

from .client import AuthenticationExpired, AuthenticationRejected, Client, RemoteError, has_code
from .model import (Lecture, Progress, REQUIRED_CATEGORIES, SERIES_CATEGORIES, SHANGHAI,
                    activity_id, interval, moment, progress, reserved)
from .mail import Mailer, mail_failure_reason, mail_history
from .state import Task, open_nofollow

DEFAULT_CATEGORIES = SERIES_CATEGORIES
QUERY_ALERT_COOLDOWN = 15 * 60
SELECTION_REASONS = {'candidate', 'reservation_not_started', 'reservation_ended', 'capacity_full',
    'not_reservable', 'already_reserved', 'category_not_allowed', 'category_not_needed',
    'title_not_selected', 'online_disabled', 'offline_disabled', 'campus_not_selected',
    'time_conflict', 'cooldown', 'invalid_activity_time'}


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

def select_candidates(rows: list[dict[str, Any]], settings: Settings, current: Progress,
                      mine: list[dict[str, Any]], cooldowns: Mapping[str, float],
                      now: float) -> tuple[list[Lecture], list[dict[str, Any]]]:
    booked = {activity_id(row) for row in mine if reserved(row)}
    conflicts = [interval(row) for row in mine if reserved(row)]
    candidates: list[Lecture] = []
    details: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = row.get('JZXL_DISPLAY', row.get('HDLX_DISPLAY'))
        if isinstance(category, str) and category.strip() and category not in settings.categories:
            window: dict[str, str | None] = {}
            for key, field in (('reservation_start', 'YYKSSJ'), ('reservation_end', 'YYJSSJ')):
                try:
                    window[key] = moment(row[field]).isoformat(timespec='seconds') if row.get(field) is not None else None
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
        elif cooldowns.get(lecture.wid, 0) > now:
            reason = 'cooldown'
        else:
            reason = 'candidate'
            candidates.append(lecture)
        details.append(dict(title=lecture.title, reason=reason,
            reservation_start=lecture.reservation_start.isoformat(timespec='seconds') if lecture.reservation_start else None,
            reservation_end=lecture.reservation_end.isoformat(timespec='seconds') if lecture.reservation_end else None))
    candidates.sort(key=lambda row: (row.category not in current.missing,
                    settings.categories.index(row.category), not row.online, row.start, row.wid))
    return candidates, details


def run(settings: Settings, *, session: Any = None,
        environ: Mapping[str, str] | None = None, prompt: Callable[[str], str] = terminal_prompt,
        notify: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        recognize: Callable[[bytes], str] | None = None,
        check_only: bool = False, new_task: bool = False) -> int:
    environ = os.environ if environ is None else environ
    announced: set[str] = set()
    alerts: dict[str, float] = {}
    latest_progress: Progress | None = None
    progress_at = 0.0
    task: Task | None = None
    client = None
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
    def emit(event: str) -> None:
        if check_only:
            record(event)
            return
        if event in ('authentication_required', 'reservation_unconfirmed', 'query_failure'):
            if event in announced:
                return
            if event == 'query_failure' and notify and task is not None:
                previous_alert = task.data.get('last_query_alert_at', 0)
                if previous_alert and clock() - previous_alert < QUERY_ALERT_COOLDOWN:
                    return
            announced.add(event)
            if notify and task is not None:
                alerts[event] = clock()
                if event == 'query_failure':
                    task.data['last_query_alert_at'] = clock()
                task.save()
        record(event)
        if notify:
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

    def clear_alert(event: str) -> None:
        announced.discard(event)
        if task is not None and event in alerts:
            del alerts[event]
            task.save()

    def query_recovered() -> None:
        if 'query_failure' in announced or 'query_failure' in alerts:
            clear_alert('query_failure')
            emit('query_recovered')

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

    def daily_report() -> None:
        if task is None or notify is None:
            return
        now = datetime.fromtimestamp(clock(), SHANGHAI)
        previous = datetime.fromtimestamp(task.data.get('last_report_at', 0), SHANGHAI)
        if now.hour < 20 or previous.date() >= now.date():
            return
        # Save the attempt before SMTP: restarting must not send the same day's report again.
        task.data['last_report_at'] = now.timestamp()
        task.save()
        emit('daily_report')

    def pause(seconds: float) -> None:
        while seconds > 0:
            daily_report()
            duration = seconds
            if notify is not None:
                now = datetime.fromtimestamp(clock(), SHANGHAI)
                due = now.replace(hour=20, minute=0, second=0, microsecond=0)
                if now >= due:
                    due += timedelta(days=1)
                duration = min(duration, (due - now).total_seconds())
            sleep(duration)
            seconds -= duration
        daily_report()
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
        health(phase='starting')
        if new_task and task.data['status'] != 'done':
            raise ValueError('active_task_cannot_be_reset')
        record('task_started')
        checkpoint = task
        def defer(seconds: float) -> None:
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.save()
            health(phase='backoff', next_check_at=checkpoint.data['retry_at'])
            if snapshot['failure_count'] >= 3:
                emit('query_failure')
            pause(seconds)
            checkpoint.data['retry_at'] = 0
            checkpoint.save()
        remaining = task.data.get('retry_at', 0) - clock()
        if remaining > 0:
            defer(remaining)
        authenticated = False
        failures = 0
        while True:
            try:
                fresh = {wid: until for wid, until in task.data['cooldowns'].items() if until > clock()}
                if len(fresh) != len(task.data['cooldowns']):
                    task.data['cooldowns'] = fresh
                    task.save()
                if not authenticated:
                    health(phase='authentication', next_check_at=0)
                    client.authenticate()
                    authenticated = True
                    clear_alert('authentication_required')
                    record('authentication_succeeded')
                health(phase='my_activities', next_check_at=0, activity_count=None, candidate_count=None)
                mine = client.mine()
                booked = {activity_id(row) for row in mine if reserved(row)}
                confirmed = task.data['status'] == 'pending' and task.data['activity'] in booked
                if task.data['status'] == 'pending' and not confirmed:
                    failures = 0
                    health(last_query_at=clock(), failure_count=0)
                    query_recovered()
                    emit('reservation_unconfirmed')
                    ask('预约结果待确认；核实后按回车重新查询（Ctrl+C 停止）：')
                    continue
                current = progress(mine, clock())
                latest_progress, progress_at = current, clock()
                if confirmed:
                    task.data.update(status='scanning', activity='')
                    task.save()
                    clear_alert('reservation_unconfirmed')
                    health(last_query_at=clock(), failure_count=0)
                    emit('reservation_succeeded')
                if current.done:
                    newly_completed = task.data['status'] != 'done'
                    task.data.update(status='done', activity='')
                    task.save()
                    final_phase = 'done'
                    health(phase='done', last_query_at=clock(), failure_count=0)
                    query_recovered()
                    if newly_completed:
                        emit('goal_completed')
                    return 0
                if task.data['status'] == 'done':
                    task.data.update(status='scanning', activity='')
                    task.save()
                if not any(current.needs(category) for category in settings.categories):
                    failures = 0
                    health(phase='waiting_for_attendance', last_query_at=clock(), failure_count=0,
                           next_check_at=clock() + 10)
                    query_recovered()
                    pause(10)
                    continue
                health(phase='activities')
                rows = client.activities()
                candidates, selection_details = select_candidates(rows, settings, current, mine, task.data['cooldowns'], clock())
                task.data['selection'] = dict(updated_at=clock(), activities=selection_details)
                health(last_query_at=clock(), failure_count=failures, activity_count=len(rows),
                       candidate_count=len(candidates), phase='preflight' if candidates else 'waiting')
                for lecture in candidates:
                    health(phase='preflight')
                    if not client.preflight(lecture.wid):
                        continue
                    for attempt in range(4):
                        health(phase='captcha')
                        binary = client.captcha()
                        if recognize is None:
                            import ddddocr
                            recognize = ddddocr.DdddOcr(show_ad=False).classification
                        code = recognize(binary).strip()
                        if not code or not code.isascii() or not code.isalnum() or len(code) > 16:
                            if attempt == 3:
                                task.data['cooldowns'][lecture.wid] = clock() + 300
                                task.save()
                            continue
                        # Persist before sending: a crash must recover by querying, never resubmitting.
                        task.data.update(status='pending', activity=lecture.wid)
                        task.save()
                        health(phase='submitting')
                        try:
                            reply = client.submit(lecture.wid, code)
                        except RemoteError as exc:
                            health(last_error=str(exc), last_error_at=clock())
                            if isinstance(exc, AuthenticationExpired):
                                authenticated = False
                            defer(max(20, exc.retry_after))
                            break
                        message = reply.get('msg', '')
                        captcha_wrong = (isinstance(message, str) and '验证码' in message
                                         and any(word in message for word in ('错误', '不正确', '失效', '过期')))
                        unavailable = isinstance(message, str) and message in ('已满', '名额已满', '不可预约', '预约已结束', '预约未开始')
                        if (has_code(reply, 0) or type(reply.get('code')) is not int
                                or not (captcha_wrong or unavailable)):
                            break
                        task.data.update(status='scanning', activity='')
                        if captcha_wrong and attempt == 3:
                            task.data['cooldowns'][lecture.wid] = clock() + 300
                        task.save()
                        if unavailable:
                            break
                    if task.data['status'] == 'pending':
                        break
                failures = 0
                if task.data['status'] != 'pending':
                    health(phase='waiting', next_check_at=clock() + 10, failure_count=0)
                    query_recovered()
                    pause(10)
                else:
                    health(phase='confirming', failure_count=0)
            except AuthenticationRejected:
                raise
            except AuthenticationExpired:
                authenticated = False
                failures += 1
                health(last_error='authentication_expired', last_error_at=clock(), failure_count=failures)
                defer(min(300, 10 * 2 ** min(failures, 5)))
            except (RemoteError, ValueError) as exc:
                failures += 1
                reason = str(exc)
                safe_reason = reason if isinstance(exc, RemoteError) or reason in (
                    'attendance_result_pending', 'attendance_category_unknown',
                    'attendance_status_unknown', 'attendance_record_conflict') else 'invalid_activity_data'
                record(safe_reason)
                health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                retry = exc.retry_after if isinstance(exc, RemoteError) else 0
                defer(max(retry, min(300, 10 * 2 ** min(failures, 5))))
    except (KeyboardInterrupt, EOFError):
        return 130
    except (AuthenticationRejected, RemoteError, ValueError, OSError) as exc:
        final_phase = 'failed'
        health(last_error=str(exc) if isinstance(exc, RemoteError) else 'configuration_or_state_invalid',
               last_error_at=clock())
        details = ''
        if check_only:
            reason = str(exc) if isinstance(exc, RemoteError) else type(exc).__name__
            details = f' stage={stage} reason={reason}'
            if isinstance(exc, RemoteError) and exc.http_status is not None:
                details += f' http_status={exc.http_status}'
            if isinstance(exc, RemoteError) and exc.host is not None:
                details += f' host={exc.host}'
            if isinstance(exc, RemoteError) and exc.tls_reason is not None:
                details += f' tls_reason={exc.tls_reason}'
            if isinstance(exc, RemoteError) and exc.proxy_used is not None:
                details += f' proxy_used={str(exc.proxy_used).lower()}'
        if check_only:
            print('event=task_failed' + details, flush=True)
        elif isinstance(exc, AuthenticationRejected):
            record('task_failed')
        else:
            emit('task_failed')
        return 1
    except Exception:
        final_phase = 'failed'
        health(last_error='unexpected_failure', last_error_at=clock())
        if check_only:
            print('event=unexpected_failure', flush=True)
        else:
            emit('unexpected_failure')
        return 1
    finally:
        if task:
            health(phase='manual_required' if snapshot['phase'] == 'manual_required' and final_phase == 'stopped'
                   else final_phase, next_check_at=0)
            task.close()
        if session is None and client is not None:
            client.session.close()
