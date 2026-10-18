"""Application entry point: one task, with replaceable external dependencies."""

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any

import requests

from .client import AuthenticationExpired, AuthenticationRejected, Client, RemoteError, has_code
from .model import Lecture, SERIES_CATEGORIES, activity_id, interval, progress, reserved
from .mail import Mailer
from .state import Task, open_nofollow

DEFAULT_CATEGORIES = SERIES_CATEGORIES


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


def run(settings: Settings, *, session: Any = None,
        environ: Mapping[str, str] | None = None, prompt: Callable[[str], str] = terminal_prompt,
        notify: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        recognize: Callable[[bytes], str] | None = None,
        check_only: bool = False, new_task: bool = False) -> int:
    environ = os.environ if environ is None else environ
    announced: set[str] = set()
    task = None
    client = None
    final_phase = 'stopped'
    stage = 'configuration'
    snapshot: dict[str, Any] = dict(phase='starting', last_query_at=0, last_error='none',
        last_error_at=0, failure_count=0, next_check_at=0, mail_status='disabled',
        last_mail_at=0, activity_count=None, candidate_count=None)
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
        if event in ('authentication_required', 'reservation_unconfirmed'):
            if event in announced:
                return
            announced.add(event)
        record(event)
        if notify:
            health(mail_status='sending')
            try:
                notify(event)
                health(mail_status='accepted', last_mail_at=clock())
            except Exception:
                record('notification_failed')
                health(mail_status='failed', last_mail_at=clock())
    def ask(label: str) -> str:
        health(phase='manual_required', next_check_at=0)
        return prompt(label)
    def read_lecture(row: dict[str, Any]) -> Lecture | None:
        try:
            return Lecture.read(row, now=clock())
        except ValueError as exc:
            if str(exc) in ('lecture_schema_unknown', 'activity_id_missing', 'location_missing'):
                raise RemoteError('activity_schema_unknown') from None
            return None
    try:
        if not check_only:
            if notify is None and Mailer.configured(environ):
                try:
                    notify = Mailer(environ)
                except ValueError:
                    print('event=mail_configuration_invalid', flush=True)
                    print('邮件配置不完整或格式错误。Gmail 只需在 .env 中填写 SMTP_USERNAME（完整邮箱地址）'
                          '和 SMTP_PASSWORD（应用专用密码）；其他邮箱需指定 SMTP_HOST。'
                          '高级配置请检查 SMTP_PORT、MAIL_FROM、MAIL_TO。清空邮件配置可关闭通知。', flush=True)
                    return 1
            snapshot['mail_status'] = 'ready' if notify else 'disabled'
        client = Client(session or requests.Session(), environ, ask, emit)
        if check_only:
            stage = 'authentication'
            client.authenticate()
            stage = 'my_activities'
            progress(client.mine(), clock())
            stage = 'activities'
            for row in client.activities():
                read_lecture(row)
            stage = 'captcha'
            client.captcha()
            print('event=read_only_complete', flush=True)
            return 0
        task = Task(settings.state_dir, environ.get('SEU_USERNAME', ''))
        health(phase='starting')
        if new_task:
            if task.data['status'] != 'done':
                raise ValueError('active_task_cannot_be_reset')
            task.data.update(status='scanning', activity='', cooldowns={})
            task.save()
        if task.data['status'] == 'done':
            final_phase = 'done'
            return 0
        record('task_started')
        checkpoint = task
        def defer(seconds: float) -> None:
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.save()
            health(phase='backoff', next_check_at=checkpoint.data['retry_at'])
            sleep(seconds)
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
                    announced.discard('authentication_required')
                    record('authentication_succeeded')
                health(phase='my_activities', next_check_at=0, activity_count=None, candidate_count=None)
                mine = client.mine()
                booked = {activity_id(row) for row in mine if reserved(row)}
                if task.data['status'] == 'pending':
                    if task.data['activity'] in booked:
                        task.data.update(status='scanning', activity='')
                        task.save()
                        announced.discard('reservation_unconfirmed')
                        emit('reservation_succeeded')
                    else:
                        failures = 0
                        health(last_query_at=clock(), failure_count=0)
                        emit('reservation_unconfirmed')
                        ask('预约结果待确认；核实后按回车重新查询（Ctrl+C 停止）：')
                        continue
                current = progress(mine, clock())
                if current.done:
                    task.data.update(status='done', activity='')
                    task.save()
                    final_phase = 'done'
                    health(phase='done', last_query_at=clock(), failure_count=0)
                    emit('goal_completed')
                    return 0
                if not any(current.needs(category) for category in settings.categories):
                    failures = 0
                    health(phase='waiting_for_attendance', last_query_at=clock(), failure_count=0,
                           next_check_at=clock() + 10)
                    sleep(10)
                    continue
                conflicts = [interval(row) for row in mine if reserved(row)]
                candidates = []
                health(phase='activities')
                rows = client.activities()
                for row in rows:
                    lecture = read_lecture(row)
                    if lecture is None:
                        continue
                    if (lecture.wid in booked or lecture.category not in settings.categories
                            or not current.needs(lecture.category)
                            or (settings.titles and lecture.title not in settings.titles)
                            or (lecture.online and not settings.online)
                            or (not lecture.online and (not settings.offline or lecture.campus not in settings.campuses))
                            or any(lecture.start <= end and lecture.end >= start for start, end in conflicts)
                            or task.data['cooldowns'].get(lecture.wid, 0) > clock()):
                        continue
                    candidates.append(lecture)
                candidates.sort(key=lambda row: (row.category not in current.missing,
                                settings.categories.index(row.category), not row.online, row.start, row.wid))
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
                        if not code or not code.isalnum() or len(code) > 16:
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
                        if has_code(reply, 0) or not (captcha_wrong or unavailable):
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
                    sleep(10)
                else:
                    health(phase='confirming', failure_count=0)
            except AuthenticationRejected:
                raise
            except AuthenticationExpired:
                authenticated = False
                failures += 1
                health(last_error='authentication_expired', last_error_at=clock(), failure_count=failures)
                if failures == 3:
                    emit('query_failure')
                defer(min(300, 10 * 2 ** min(failures, 5)))
            except (RemoteError, ValueError) as exc:
                failures += 1
                reason = str(exc)
                safe_reason = reason if isinstance(exc, RemoteError) or reason in (
                    'attendance_result_pending', 'attendance_category_unknown',
                    'attendance_status_unknown', 'attendance_record_conflict') else 'invalid_activity_data'
                record(safe_reason)
                health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                if failures == 3:
                    emit('query_failure')
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
