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
from .model import Lecture, activity_id, interval, reserved
from .mail import Mailer
from .state import Task

DEFAULT_CATEGORIES = ('人文与科学素养系列讲座_心理健康', '人文与科学素养系列讲座_法律',
                      '人文与科学素养系列讲座-艺术类', '人文与科学素养系列讲座_其他', '“SEU咖啡间"系列沙龙活动')


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
    def record(event: str) -> None:
        print('event=' + event, flush=True)
        if check_only:
            return
        try:
            fd = os.open(settings.state_dir / 'events.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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
            try:
                notify(event)
            except Exception:
                record('notification_failed')
    client = Client(session or requests.Session(), environ, prompt, emit)
    task = None
    try:
        if check_only:
            client.authenticate()
            client.mine()
            client.activities()
            client.captcha()
            print('event=read_only_complete', flush=True)
            return 0
        task = Task(settings.state_dir, environ.get('SEU_USERNAME', ''))
        if new_task:
            if task.data['status'] != 'done':
                raise ValueError('active_task_cannot_be_reset')
            task.data.update(status='scanning', activity='', cooldowns={})
            task.save()
        if task.data['status'] == 'done':
            return 0
        if notify is None:
            try:
                notify = Mailer(environ)
            except ValueError:
                print('event=mail_configuration_invalid', flush=True)
                print('邮件配置不完整或格式错误。请检查 .env 中的 SMTP_HOST、SMTP_PORT、MAIL_FROM、MAIL_TO；'
                      'SMTP_USERNAME 与 SMTP_PASSWORD 需同时填写。', flush=True)
                return 1
        checkpoint = task
        def defer(seconds: float) -> None:
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.save()
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
                    client.authenticate()
                    authenticated = True
                    announced.discard('authentication_required')
                mine = client.mine()
                booked = {activity_id(row) for row in mine if reserved(row)}
                if task.data['status'] == 'pending':
                    if task.data['activity'] in booked:
                        task.data['status'] = 'done'
                        task.save()
                        emit('reservation_succeeded')
                        return 0
                    emit('reservation_unconfirmed')
                    prompt('预约结果待确认；核实后按回车重新查询（Ctrl+C 停止）：')
                    continue
                conflicts = [interval(row) for row in mine if reserved(row)]
                candidates = []
                rows = client.activities()
                for row in rows:
                    try:
                        lecture = Lecture.read(row)
                    except ValueError as exc:
                        if str(exc) in ('lecture_schema_unknown', 'activity_id_missing', 'location_missing'):
                            raise RemoteError('activity_schema_unknown') from None
                        continue
                    if (lecture.wid in booked or lecture.category not in settings.categories
                            or (settings.titles and lecture.title not in settings.titles)
                            or (lecture.online and not settings.online)
                            or (not lecture.online and (not settings.offline or lecture.campus not in settings.campuses))
                            or any(lecture.start <= end and lecture.end >= start for start, end in conflicts)
                            or task.data['cooldowns'].get(lecture.wid, 0) > clock()):
                        continue
                    candidates.append(lecture)
                candidates.sort(key=lambda row: (settings.categories.index(row.category), not row.online, row.start, row.wid))
                for lecture in candidates:
                    if not client.preflight(lecture.wid):
                        continue
                    for attempt in range(4):
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
                        try:
                            reply = client.submit(lecture.wid, code)
                        except RemoteError as exc:
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
                    sleep(10)
            except AuthenticationRejected:
                raise
            except AuthenticationExpired:
                authenticated = False
                failures += 1
                if failures == 3:
                    emit('query_failure')
                defer(min(300, 10 * 2 ** min(failures, 5)))
            except (RemoteError, ValueError) as exc:
                failures += 1
                record(str(exc) if isinstance(exc, RemoteError) else 'invalid_activity_data')
                if failures == 3:
                    emit('query_failure')
                retry = exc.retry_after if isinstance(exc, RemoteError) else 0
                defer(max(retry, min(300, 10 * 2 ** min(failures, 5))))
    except (KeyboardInterrupt, EOFError):
        return 130
    except (AuthenticationRejected, RemoteError, ValueError, OSError):
        print('event=task_failed', flush=True)
        return 1
    except Exception:
        print('event=unexpected_failure', flush=True)
        return 1
    finally:
        if task:
            task.close()
        if session is None:
            client.session.close()
