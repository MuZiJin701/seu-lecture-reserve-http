"""Application entry point: one task, with replaceable external dependencies."""

import functools
import json
import os
import random
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import requests

from .client import (AuthenticationExpired, AuthenticationRejected, Client, RemoteError,
                     has_code, server_backoff_error)
from .model import (COFFEE_CATEGORY, GOAL_CATEGORIES, Lecture, Progress,
                    REQUIRED_CATEGORIES, SHANGHAI, activity_id, category_name,
                    confirmed_reservation, conflicting_records, interval, moment, progress,
                    reservation_display, reserved, test_notice)
from .mail import MailWorker, Mailer, mail_failure_reason, mail_history
from .poller import ActivityPoller, WaitGate, server_directed_error
from .runtime import CaptchaWorker, LocalLogs, PreparedCaptcha, WorkSignal
from .state import FINGERPRINT_FILE, Task, open_nofollow

DEFAULT_CATEGORIES = GOAL_CATEGORIES + (COFFEE_CATEGORY,)
QUERY_FAILURE_SECONDS = 5 * 60
STEP_FAILURE_SECONDS = 5 * 60
MAJOR_MAIL_EVENTS = {'authentication_required', 'authentication_rejected', 'query_failure', 'task_failed',
                     'unexpected_failure', 'reservation_step_failure'}
SCAN_INTERVAL = 5
FAST_INTERVAL = 0.10
SELECTION_REASONS = {'candidate', 'reservation_not_started', 'reservation_ended', 'capacity_full',
    'not_reservable', 'already_reserved', 'category_not_allowed',
    'title_not_selected', 'online_disabled', 'offline_disabled', 'campus_not_selected',
    'time_conflict', 'cooldown', 'invalid_activity_time', 'notice_test_only', 'retry_limit'}
# A pending submission is verified repeatedly by activity ID before any automatic release: the
# submission is non-idempotent, so absence only becomes actionable after a short series of valid
# checks. Automatic release keeps unattended operation possible; the local log keeps the evidence.
PENDING_MISS_PAUSE = 2
PENDING_AUTO_RELEASE_CHECKS = 3
# After the first release, keep the original activity's time interval as a local hold so a
# different lecture at the same time cannot be booked while the first submission is unresolved.
PENDING_HOLD_SECONDS = 600
PENDING_RETRY_LIMIT = 2


class QueryInFlight(Exception):
    """A required reader snapshot is not ready; wait without recording a query failure."""


class ReaderFailure(RemoteError):
    """A failed snapshot whose retry is owned by its reader."""


def pending_hold_until(start: float, now: float) -> float:
    return max(now, min(now + PENDING_HOLD_SECONDS, start))


def normalize_hold_deadlines(holds: Mapping[str, Any]) -> bool:
    """Recalculate older checkpoint deadlines from the stored start and creation time."""
    changed = False
    for entry in holds.values():
        until = pending_hold_until(entry['start'], entry['since'])
        if entry['until'] != until:
            entry['until'] = until
            changed = True
    return changed


# After the candidate appears, a slow successful submission must not be aborted by the fast list
# timeout; captcha and submit keep a longer budget while the list stays fast.
SUBMIT_TIMEOUT = 8
# Main-session setup budget; query owners use their independent read policy below.
OPENING_QUERY_TIMEOUT = 6
# Slow reads stay off the main thread without abandoning every response over half a second.
OPENING_BACKGROUND_TIMEOUT = 2.0
HISTORY_TIMEOUT = 8.0
HISTORY_LIST_REFRESH_AFTER = 0.5
# The opening fast path retries a failed reader quickly; throttle its local diagnostic so a
# persistent network fault cannot append one line every 0.1 s.
BACKGROUND_READER_DIAG_INTERVAL = 5.0
# Treat 502/503/504 as temporary service failures; they do not establish overload or rate limiting.
# Explicit Retry-After still wins.
OVERLOAD_RETRY_CAP = 5.0
# A concurrent local reader can briefly block the Windows state replace; report such a transient
# health-save failure at most once a minute instead of on every scan.
HEALTH_WARNING_INTERVAL = 60.0
# Health snapshots are not recovery-critical: coalesce the many in-scan updates and flush once at
# each wait boundary (and at shutdown) instead of fsyncing several times per second. --status can
# lag the in-memory state by up to this interval; errors are still written immediately.
HEALTH_WRITE_INTERVAL = 1.0
# Phase changes that an operator must see immediately (long blocking requests happen in these
# phases, so the coalesced wait-boundary flush alone would hide them for seconds).
HEALTH_URGENT_PHASES = frozenset({'authentication', 'captcha', 'submitting', 'confirming',
                                  'manual_required', 'failed'})
CAPTCHA_PREFETCH_LEAD_SECONDS = 2.7
CAPTCHA_PREFETCH_TIMEOUT = 8.0
# One retry keeps the prepared captcha and the warm main-session connection within reach when the
# first attempt fails on a slow handshake. A concurrent second captcha is still impossible.
CAPTCHA_PREFETCH_ATTEMPTS = 2
# Leave a little lead time before starting a speculative fetch on the submission session.
CAPTCHA_PREFETCH_MIN_REMAINING = 0.45
# Open the submission session's connection once when fast opening starts: the T-2.7 captcha
# prefetch and the submit after it then reuse a pooled TLS connection instead of paying a cold
# handshake in the congested opening seconds. The request itself stays a cheap list read.
MAIN_SESSION_WARM_TIMEOUT = 2.0
MAIN_SESSION_WARM_MARGIN = 0.2
# Local diagnostics are append-only and never deleted: an oversized log is renamed once at startup
# so old evidence stays on disk while the active file restarts empty.
DIAGNOSTICS_MAX_BYTES = 8 * 1024 * 1024
# A background reader that has produced no reliable snapshot for this long gets one local
# diagnostic per outage. This changes no request ownership or wait behavior: the documented
# "keep waiting, never replace an in-flight reader" rule still applies.
READER_STALL_SECONDS = 60.0
# The same request failure can repeat every 0.1 s during an opening outage; one local line per
# (reader, endpoint, reason, stage) per interval is enough for diagnosis. Suppressed repeats are
# counted in the next emitted line.
REQUEST_FAILED_DIAG_INTERVAL = 5.0


def short_backoff(failures: int) -> float:
    """Bounded short retry schedule: 1, 2, 4, 5, 5 ... seconds.

    The old 20-40-80 s generic schedule could park the scanner for the whole opening after one
    transient failure. Opening windows last seconds, so retries stay frequent; a server-directed
    Retry-After is handled separately and still honored exactly.
    """
    return min(OVERLOAD_RETRY_CAP, 2.0 ** min(max(failures - 1, 0), 3))


def new_history_poller(query: Callable[[], list[dict[str, Any]]], *,
                       interval: float | Callable[[], float | None] | None, clock: Callable[[], float],
                       gate: WaitGate | None = None,
                       on_exit: Callable[[], None] | None = None,
                       on_update: Callable[[], None] | None = None,
                       retry_delay: Callable[[int, Any], float] | None = None,
                       retry_interval: Callable[[], float] | None = None) -> ActivityPoller:
    """Build the event-driven "my activities" owner (injectable for offline tests).

    There is no periodic schedule: each refresh event retries until a reliable response arrives.
    """
    return ActivityPoller(query, interval=interval, clock=clock, gate=gate, on_exit=on_exit,
                          on_update=on_update, retry_delay=retry_delay,
                          retry_interval=retry_interval, name='history-reader')


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
    # Prepare the captcha off the candidate-to-submit path.
    captcha_prefetch: bool = True
    prewarm: bool = True
    # Opt-in trusted device for the SMS second factor: off by default so the login payload keeps
    # its previous (untrusted) shape unless the user explicitly enables it.
    trust_device: bool = False

def server_wait_error(exc: BaseException) -> bool:
    """True only for server replies permitted to introduce an error backoff."""
    return (isinstance(exc, RemoteError) and not isinstance(exc, ReaderFailure)
            and server_backoff_error(exc))


def progress_rows(history: list[dict[str, Any]], holds: Mapping[str, Any] | None,
                  now: float) -> list[dict[str, Any]]:
    """Merge confirmed local holds that the school history has not shown yet."""
    rows = list(history)
    known: set[str] = set()
    for row in history:
        try:
            known.add(activity_id(row))
        except ValueError:
            continue
    for wid, entry in (holds or {}).items():
        if not isinstance(entry, dict) or not entry.get('confirmed'):
            continue
        if wid in known:
            continue
        category = entry.get('category')
        if not isinstance(category, str):
            # The reference target cannot count an unknown category, but the confirmed hold must
            # still participate in conflict checks, so keep the row with an empty category.
            category = ''
        try:
            start = datetime.fromtimestamp(float(entry['start']), SHANGHAI)
            end = datetime.fromtimestamp(float(entry['end']), SHANGHAI)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
        if end.timestamp() <= now:
            continue
        rows.append({'HD_WID': wid, 'YYZT': '已预约', 'JZXL_DISPLAY': category,
                     'KSSJ': start.strftime('%Y-%m-%d %H:%M:%S'),
                     'JSSJ': end.strftime('%Y-%m-%d %H:%M:%S'),
                     'SFDK': '0', 'SFWY': '0'})
    return rows


# Structural errors that make a background "my activities" snapshot unusable. Unresolved
# attendance results no longer block: an ended lecture without a readable result is skipped until
# the school updates it; format-level errors require a new history snapshot.
HISTORY_ROW_FORMAT_ERRORS = frozenset({
    'activity_id_missing', 'attendance_record_conflict', 'publication_status_unknown', 'reservation_status_unknown',
    'time_missing', 'time_invalid', 'interval_invalid'})


def background_history_usable(rows: list[dict[str, Any]], now: float) -> str | None:
    """Return the structural error reason when the background snapshot must be retried.

    A malformed background snapshot used to be accepted and only fail later in ``progress`` or
    in the conflict check, which turned one bad row into a scan-level backoff. Validate it the
    same way the scan will use it before accepting it as reliable history.
    """
    # Conflict protection needs a parseable interval for every existing reservation, including
    # salons and notices that progress() skips when counting goal progress.
    for row in rows:
        try:
            activity_id(row)
            if reserved(row):
                interval(row)
        except ValueError as exc:
            return str(exc)
    try:
        progress(rows, now)
    except ValueError as exc:
        reason = str(exc)
        if reason in HISTORY_ROW_FORMAT_ERRORS:
            return reason
    return None


def select_candidates(rows: list[dict[str, Any]], settings: Settings, current: Progress,
                      mine: list[dict[str, Any]], cooldowns: Mapping[str, float],
                      now: float, *,
                      holds: list[Mapping[str, Any]] | None = None
                      ) -> tuple[list[Lecture], list[dict[str, Any]]]:
    booked = {activity_id(row) for row in mine if confirmed_reservation(row)}
    conflicts = [interval(row) for row in mine if confirmed_reservation(row)]
    candidates: list[Lecture] = []
    details: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    categories = tuple(category_name(value) for value in settings.categories)
    for row in rows:
        try:
            identity = {'wid': activity_id(row)}
        except ValueError:
            identity = {}
        category = category_name(row.get('JZXL_DISPLAY', row.get('HDLX_DISPLAY')))
        if test_notice(row):
            title = row.get('JZMC', row.get('HDMC'))
            details.append(dict(title=title if isinstance(title, str) else '',
                                reason='notice_test_only',
                                reservation_start=None, reservation_end=None, **identity))
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
                                reason='category_not_allowed', **window, **identity))
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
                                reason='invalid_activity_time', **window, **identity))
            continue
        held = next((item for item in holds or [] if item.get('activity') == lecture.wid), None)
        hold_conflict = any(item.get('activity') != lecture.wid
                            and lecture.start <= item['end'] and lecture.end >= item['start']
                            for item in holds or [])
        hold_retry = held is not None and (held.get('confirmed') is True
                                           or int(held.get('attempts', 0)) >= PENDING_RETRY_LIMIT)
        if lecture.wid in booked or lecture.availability == 'already_reserved':
            reason = 'already_reserved'
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
        elif hold_conflict:
            reason = 'time_conflict'
        elif hold_retry:
            reason = 'retry_limit'
        elif lecture.wid in cooldowns and cooldowns[lecture.wid] > now:
            reason = 'cooldown'
        elif lecture.availability != 'candidate':
            reason = lecture.availability
        else:
            reason = 'candidate'
            candidates.append(lecture)
        details.append(dict(wid=lecture.wid, title=lecture.title, reason=reason,
            reservation_start=lecture.reservation_start.isoformat(timespec='seconds') if lecture.reservation_start else None,
            reservation_end=lecture.reservation_end.isoformat(timespec='seconds') if lecture.reservation_end else None))
    candidates.sort(key=lambda row: (categories.index(row.category),
                    not row.online, row.start, row.wid))
    return candidates, details


def opening_windows(details: list[dict[str, Any]]) -> list[tuple[float, float]]:
    openings: list[tuple[float, float]] = []
    for item in details if isinstance(details, list) else []:
        if not isinstance(item, dict) or item.get('reason') not in (
                'reservation_not_started', 'candidate', 'capacity_full'):
            continue
        try:
            start = datetime.fromisoformat(item['reservation_start'])
            end = (datetime.fromisoformat(item['reservation_end']) if item.get('reservation_end')
                   else start + timedelta(minutes=5))
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                continue
            openings.append((start.timestamp(), min(end.timestamp(), start.timestamp() + 300)))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return openings


def merge_openings(known: dict[str | int, dict[str, Any]], details: list[dict[str, Any]],
                   now: float) -> dict[str | int, dict[str, Any]]:
    """Keep missing targets, but retire targets with an explicit reservation/filter result."""
    merged: dict[str | int, dict[str, Any]] = {}
    for key, item in known.items():
        windows = opening_windows([{**item, 'reason': 'candidate'}])
        if windows and windows[0][1] >= now - 1:
            merged[key] = item
    for index, item in enumerate(details if isinstance(details, list) else []):
        if not isinstance(item, dict) or item.get('reason') == 'invalid_activity_time':
            continue
        if not opening_windows([{**item, 'reason': 'candidate'}]):
            continue
        wid = item.get('wid')
        if known and not isinstance(wid, str):
            continue
        key = wid if isinstance(wid, str) and wid else index
        if isinstance(key, str) and key not in merged:
            # Older selection snapshots lack IDs; bind one matching old row, including duplicates.
            legacy = next((old for old, value in merged.items() if isinstance(old, int)
                           and value.get('title') == item.get('title')
                           and value.get('reservation_start') == item.get('reservation_start')
                           and value.get('reservation_end') == item.get('reservation_end')), None)
            if legacy is not None:
                merged.pop(legacy)
        merged[key] = item
    return merged


def in_fast_opening(start: float, end: float, now: float) -> bool:
    return start - 5 <= now < min(end, start + 9)


def opening_intervals(openings: list[tuple[float, float]], now: float) -> tuple[float, float] | None:
    if any(in_fast_opening(start, end, now) for start, end in openings):
        return 0.05, 0.10
    if any(start - 30 <= now < min(end, start + 300) for start, end in openings):
        return 0.5, 1.5
    return None


@dataclass(frozen=True)
class ReadPolicy:
    timeout: float
    list_max_age: float


def read_policy(openings: list[tuple[float, float]], now: float) -> ReadPolicy:
    bounds = opening_intervals(openings, now)
    if bounds is None:
        return ReadPolicy(10.0, 10.0)
    return ReadPolicy(OPENING_BACKGROUND_TIMEOUT, OPENING_BACKGROUND_TIMEOUT)


def scan_delay(details: list[dict[str, Any]], now: float, elapsed: float,
               interval: float, known: list[tuple[float, float]] | None = None) -> tuple[float, bool]:
    openings = opening_windows(details) if known is None else known
    bounds = opening_intervals(openings, now)
    target = random.uniform(*(bounds or (interval - 1, interval + 1)))
    delay = max(0, target - elapsed) if bounds is not None else target
    boundaries = [boundary - now for start, end in openings
                  for boundary in (start - 30, start - 5, min(start + 9, end), min(end, start + 300))
                  if boundary > now]
    return min([delay, *boundaries]), bounds is not None


def _state_event(settings: Settings, event: str) -> None:
    print('event=' + event, flush=True)
    try:
        fd = open_nofollow(settings.state_dir / 'events.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, 'a') as target:
            target.write(f'{time.time():.0f} event={event}\n')
    except OSError:
        pass


def rotate_diagnostics(directory: Path, limit: int = DIAGNOSTICS_MAX_BYTES) -> None:
    """Rename an oversized local diagnostics log once at startup; old files are never deleted.

    Diagnostics are append-only, so a long-running process can grow the file without bound. The
    operator's cleanup becomes a per-start guarantee instead: the oversized file is renamed to
    ``diagnostics-<stamp>.jsonl`` (numeric suffix on collision) and the next scan starts a new,
    empty active file. Rotation only happens before the scan starts, so nothing appends to a file
    while it moves, and a failed rename must never stop the scan.
    """
    active = directory / 'diagnostics.jsonl'
    try:
        # lstat keeps a symlink from being followed into a file outside the state directory.
        if active.lstat().st_size <= limit:
            return
    except OSError:
        return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    archived = directory / f'diagnostics-{stamp}.jsonl'
    suffix = 1
    while archived.exists():
        archived = directory / f'diagnostics-{stamp}-{suffix}.jsonl'
        suffix += 1
    try:
        active.rename(archived)
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
        client = Client(session or requests.Session(), environ, prompt, lambda _: None,
                        trust_device=settings.trust_device,
                        fingerprint_dir=settings.state_dir)
        if task.data['status'] != 'pending':
            _state_event(settings, 'pending_not_found')
            return 1
        pending_wid = task.data['activity']
        try:
            client.authenticate()
            history = client.mine()
            if conflicting_records(history):
                # Contradictory records cannot confirm the submission; keep the pending state
                # and make the operator inspect the school record again.
                _state_event(settings, 'pending_check_failed')
                return 1
            booked = {activity_id(row) for row in history if confirmed_reservation(row)}
        except (RemoteError, ValueError):
            _state_event(settings, 'pending_check_failed')
            return 1
        if pending_wid in booked:
            task.data.update(status='scanning', activity='')
            for field in ('pending_checks', 'pending_since', 'pending_start', 'pending_end',
                          'pending_category', 'pending_unconfirmed_notified'):
                task.data.pop(field, None)
            holds = task.data.get('holds')
            if isinstance(holds, dict):
                holds.pop(pending_wid, None)
            legacy = task.data.get('hold')
            if isinstance(legacy, dict) and legacy.get('activity') == pending_wid:
                task.data.pop('hold', None)
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
        for field in ('pending_checks', 'pending_since', 'pending_start', 'pending_end',
                      'pending_category'):
            task.data.pop(field, None)
        holds = task.data.get('holds')
        if isinstance(holds, dict):
            holds.pop(pending_wid, None)
        legacy = task.data.get('hold')
        if isinstance(legacy, dict) and legacy.get('activity') == pending_wid:
            task.data.pop('hold', None)
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
        check_only: bool = False, new_task: bool = False,
        background_poller: bool = True,
        stop: Callable[[], bool] | None = None,
        checkpoint: Task | None = None,
        wait_for_update: Callable[[WorkSignal, float], bool] | None = None,
        monotonic: Callable[[], float] | None = None) -> int:
    environ = os.environ if environ is None else environ
    announced: set[str] = set()
    # Alerts this process already recorded (even when a persisted alert suppressed the mail); the
    # authentication-rejected fallback uses it to avoid writing a second event line when the
    # client already announced (and recorded) it.
    announced_here: set[str] = set()
    alerts: dict[str, float] = {}
    mail_worker: MailWorker | None = None
    mailer: Mailer | None = None
    work_updated = WorkSignal()
    timer = monotonic or (time.monotonic if clock is time.time else clock)
    local_logs = LocalLogs(settings.state_dir)
    captcha_worker: CaptchaWorker | None = None
    preparation: CaptchaWorker | None = None
    preparation_source = 'prefetch'
    authentication_generation = 0
    pending_check_due = 0.0
    next_preparation_at: float | None = None
    latest_progress: Progress | None = None
    progress_at = 0.0
    goal_reported = False
    last_recorded_error: tuple[str, float] | None = None
    task: Task | None = None
    client = None
    activity_poller: ActivityPoller | None = None
    poller_client: Client | None = None
    poller_retry_at = 0.0
    # Completed-poll version consumed by the last list read: the opening wait only wakes for
    # polls newer than this.
    consumed_polls = 0
    # Last time each background reader failure was written to diagnostics (throttled in opening).
    reader_diag_logged_at: dict[str, float] = {}
    # Local time the last consumed list request started, for candidate data-age metrics.
    last_rows_started = 0.0
    # When the consumed list rows were published/fetched, for detecting a newer poller snapshot.
    rows_snapshot_at = 0.0
    # An absolute deadline copied from a server-directed poller error. The scan loop checks it
    # before processing candidates, so every request shares one server-requested pause.
    poller_wait_until = 0.0
    # Shared server-directed wait deadline for both read-only query threads and the scan loop.
    # A thread that gets 429/Retry-After updates it immediately, so the other thread stops before
    # its next request even when the main thread is blocked in captcha/submission.
    wait_gate = WaitGate(clock)
    # The history owner has its own session; the main loop only consumes its results.
    history_poller: ActivityPoller | None = None
    history_client: Client | None = None
    history_retry_at = 0.0
    history_request: tuple[ActivityPoller, int] | None = None
    prewarmed_openings: set[float] = set()
    main_warmed_openings: set[float] = set()
    missing_readers: set[str] = set()
    # Start of the current "no reliable snapshot" outage per reader, whether its missing line was
    # already written, and whether the stall line was written; all cleared on recovery.
    missing_since: dict[str, float] = {}
    missing_diagnosed: set[str] = set()
    stalled_readers: set[str] = set()
    # request_failed repeats are throttled per safe key: last write time and suppressed count.
    request_failed_written: dict[tuple[Any, ...], float] = {}
    request_failed_suppressed: dict[tuple[Any, ...], int] = {}
    pending_read_key: tuple[str, float] | None = None
    pending_read_after: float | None = None
    # Set after the atomic submission checkpoint is persisted; positive pending evidence must come
    # from a snapshot taken no earlier than this, not merely no earlier than ``pending_since``.
    pending_saved_at = 0.0
    last_mine_started = 0.0
    draining_readers: dict[str, list[ActivityPoller]] = {'activities': [], 'history': []}
    reader_policies: dict[str, ReadPolicy] = {}

    def close_session(session: Any) -> None:
        close = getattr(session, 'close', None)
        if callable(close):
            close()

    def check_stopped() -> None:
        if stop is not None and stop():
            raise KeyboardInterrupt

    def stop_poller() -> None:
        nonlocal activity_poller, poller_client
        poller = activity_poller
        activity_poller = None
        if poller is not None:
            # Do not wait for an in-flight request: the worker closes its own session on exit.
            poller.stop(join=False)
            if getattr(poller, 'in_flight_since', None) is not None:
                draining_readers['activities'].append(poller)
        elif poller_client is not None:
            close_session(getattr(poller_client, 'session', None))
        poller_client = None

    def stop_history_poller() -> None:
        """Stop the background history refresher; its worker closes the session on exit."""
        nonlocal history_poller, history_client
        poller = history_poller
        history_poller = None
        if poller is not None:
            poller.stop(join=False)
            if getattr(poller, 'in_flight_since', None) is not None:
                draining_readers['history'].append(poller)
        elif history_client is not None:
            close_session(getattr(history_client, 'session', None))
        history_client = None

    def reader_draining(reader: str) -> bool:
        draining_readers[reader] = [p for p in draining_readers[reader]
                                    if p.in_flight_since is not None]
        return bool(draining_readers[reader])

    def absorb_poller_wait() -> None:
        """Copy a server-directed wait from a failed poller into the scan loop's deadline."""
        nonlocal poller_wait_until, poller_retry_at
        poller = activity_poller
        snapshot = poller.snapshot() if poller is not None else None
        if snapshot is None or snapshot.error is None or not snapshot.server_directed:
            return
        wait = snapshot.retry_after if snapshot.retry_after > 0 else short_backoff(1)
        based_at = snapshot.failed_at if snapshot.failed_at > 0 else clock()
        poller_wait_until = max(poller_wait_until, based_at + wait)
        poller_retry_at = max(poller_retry_at, poller_wait_until)
        # Mirror the wait into the thread-shared gate as well (real pollers already record it
        # from their own error path; this also covers injected/test pollers and older snapshots).
        wait_gate.record_until(poller_wait_until)

    def absorb_history_wait() -> None:
        """A server-directed wait from the history refresher constrains all requests too."""
        nonlocal poller_wait_until, history_retry_at
        poller = history_poller
        snapshot = poller.snapshot() if poller is not None else None
        if snapshot is None or snapshot.error is None or not snapshot.server_directed:
            return
        wait = snapshot.retry_after if snapshot.retry_after > 0 else short_backoff(1)
        based_at = snapshot.failed_at if snapshot.failed_at > 0 else clock()
        poller_wait_until = max(poller_wait_until, based_at + wait)
        history_retry_at = max(history_retry_at, poller_wait_until)
        wait_gate.record_until(poller_wait_until)

    def shared_wait_pending() -> bool:
        """Absorb both readers' waits and report whether a server-directed pause is active."""
        absorb_poller_wait()
        absorb_history_wait()
        return wait_gate.remaining() > 0

    def poller_wait_error() -> None:
        """Abort before a new request while any background reader's server wait is pending.

        Both read-only threads share one server-requested pause: a 429/Retry-After seen by the
        history refresher must stop captcha/submission just like one seen by the list poller.
        Readers park at the same gate before their next request without losing their sessions.
        """
        check_stopped()
        if not shared_wait_pending():
            return
        remaining = max(wait_gate.remaining(), poller_wait_until - clock())
        if remaining > 0:
            raise RemoteError('server_backoff', remaining, http_status=429)

    mail_state_ready = False
    health_warning_at = 0.0
    final_phase = 'stopped'
    stage = 'configuration'
    snapshot: dict[str, Any] = dict(phase='starting', last_query_at=0, last_error='none',
        last_error_at=0, last_error_active=False, failure_count=0, next_check_at=0,
        mail_status='disabled',
        last_mail_at=0, last_mail_status='never', last_mail_error='none', last_mail_event='none',
        last_mail_duration_seconds=None, activity_count=None, candidate_count=None)
    health_written_at = 0.0
    health_written_phase: str | None = None
    health_dirty = False
    def write_health() -> None:
        nonlocal health_warning_at, health_written_at, health_written_phase, health_dirty
        if task is None:
            return
        task.data['health'] = dict(snapshot)
        try:
            task.save()
        except (OSError, ValueError):
            now = clock()
            if now - health_warning_at >= HEALTH_WARNING_INTERVAL:
                health_warning_at = now
                print('event=health_write_failed', flush=True)
            return
        health_dirty = False
        health_written_at = clock()
        health_written_phase = snapshot['phase']
    def health(**changes: Any) -> None:
        nonlocal health_dirty
        if 'last_error' in changes:
            changes['last_error_active'] = changes['last_error'] != 'none'
        elif changes.get('last_query_at'):
            # A completed query clears the current-error flag while the old code/time remains.
            changes['last_error_active'] = False
        snapshot.update(changes, updated_at=clock())
        if task is None:
            return
        health_dirty = True
        urgent = ('last_error' in changes or 'last_error_at' in changes
                  or 'mail_status' in changes or 'last_mail_status' in changes
                  # A phase is urgent when first entered, not on every repeated call in it:
                  # the candidate path calls it several times before submitting.
                  or (snapshot['phase'] in HEALTH_URGENT_PHASES
                      and snapshot['phase'] != health_written_phase))
        if urgent or clock() - health_written_at >= HEALTH_WRITE_INTERVAL:
            write_health()
    def flush_health(force: bool = False) -> None:
        """Write the pending snapshot when the phase changed, the interval elapsed, or forced."""
        if not health_dirty:
            return
        if (force or snapshot['phase'] != health_written_phase
                or clock() - health_written_at >= HEALTH_WRITE_INTERVAL):
            write_health()
    def set_health(**changes: Any) -> None:
        """Update the in-memory snapshot without a write; the next explicit save persists it."""
        nonlocal health_dirty
        snapshot.update(changes, updated_at=clock())
        if task is not None:
            task.data['health'] = dict(snapshot)
            health_dirty = True
    def record(event: str) -> None:
        print(('' if check_only else f'{clock():.0f} ') + 'event=' + event, flush=True)
        if check_only:
            return
        try:
            local_logs.append('events.log', f'{clock():.0f} event={event}')
        except OSError:
            print('event=local_log_failed', flush=True)
    def diagnostic(event: str, **fields: Any) -> None:
        # Detailed history stays in the private state directory, never SMTP or stdout.
        if check_only:
            return
        if event == 'request_failed':
            # A persistent reader fault repeats the same safe fields every retry; keep one local
            # line per interval and report how many repeats were folded into the next line.
            key = (fields.get('reader'), fields.get('endpoint'),
                   fields.get('reason'), fields.get('stage'))
            now = clock()
            last = request_failed_written.get(key)
            if last is not None and 0 <= now - last < REQUEST_FAILED_DIAG_INTERVAL:
                request_failed_suppressed[key] = request_failed_suppressed.get(key, 0) + 1
                return
            suppressed = request_failed_suppressed.pop(key, 0)
            if suppressed:
                fields['suppressed'] = suppressed
            request_failed_written[key] = now
        try:
            line = json.dumps(dict(at=clock(), event=event, **fields),
                              ensure_ascii=False, allow_nan=False)
            local_logs.append('diagnostics.jsonl', line)
        except (OSError, ValueError):
            record('diagnostic_write_failed')
    def load_recognizer() -> Callable[[bytes], str]:
        # Load the OCR model once before an opening: first use inside a submission path costs
        # roughly 1-2 s and is paid exactly at the worst moment.
        started = clock()
        import ddddocr
        engine = ddddocr.DdddOcr(show_ad=False).classification
        diagnostic('ocr_ready', duration_seconds=max(0, clock() - started))
        return engine
    def flush_mail() -> None:
        if mail_worker is None:
            return
        for result in mail_worker.results():
            if result.track:
                health(mail_status=result.status, last_mail_status=result.status,
                       last_mail_at=clock(), last_mail_event=result.event,
                       last_mail_error=result.reason, last_mail_duration_seconds=result.duration)
            if result.status == 'failed':
                record('notification_failed reason=' + result.reason)
            if result.status != 'sending':
                record(f'notification_finished mail_event={result.event} duration_seconds={result.duration}')

    def start_mail_worker() -> None:
        nonlocal mail_worker
        if notify is not None and mail_worker is None:
            sender = notify
            smtp = mailer
            # The enqueue time is frozen before the job enters the queue so the body (including
            # its timestamp) is immutable while the single sender thread only performs SMTP I/O.
            mail_worker = MailWorker(
                (lambda ev, details, at: smtp(ev, details, at=at)) if smtp is not None
                else (lambda ev, details, at: sender(ev)), functools.partial(work_updated.set, 'mail'))
    def send_mail(event: str, *, track: bool = True) -> None:
        if notify is None:
            return
        start_mail_worker()
        assert mail_worker is not None
        details = notification_details() if mailer is not None else ''
        if track:
            health(mail_status='sending', last_mail_status='sending', last_mail_error='none',
                   last_mail_at=clock(), last_mail_event=event, last_mail_duration_seconds=None)
        mail_worker.submit(event, details, track=track, at=clock())

    def emit(event: str) -> None:
        record(event)
        if check_only:
            return
        if event == 'reservation_succeeded':
            # Success is not a deduplicated major alert: each confirmed reservation may notify.
            # It is sent from a worker thread; scanning must not wait for SMTP.
            if settings.mail_success and notify is not None:
                send_mail(event, track=False)
            return
        if event not in MAJOR_MAIL_EVENTS:
            return
        alert = ('task_failed' if event in ('unexpected_failure', 'authentication_rejected')
                 else event)
        # Record that this process already handled the alert before the persisted dedupe can return
        # early: the authentication-rejected fallback must not write a second event line after a
        # restart that reloaded the alert from task.json.
        announced_here.add(alert)
        if alert in announced:
            return
        announced.add(alert)
        if notify and task is not None and mail_state_ready:
            alerts[alert] = clock()
            task.save()
        if notify:
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

    def hold_entries() -> dict[str, dict[str, Any]]:
        if task is None:
            return {}
        holds = task.data.get('holds')
        return holds if isinstance(holds, dict) else {}

    def emit_success(wid: str, source: str) -> None:
        # The single reservation_confirmed diagnostic for a confirmation lives here; callers
        # must not log the same confirmation again.
        diagnostic('reservation_confirmed', wid=wid, source=source)
        record('reservation_hold_confirmed')
        step_recovered()
        clear_alert('reservation_unconfirmed')
        emit('reservation_succeeded')
        # A confirmed reservation is a history refresh event; the scan fires it after the
        # current submission round so progress/conflicts are updated for the next scan.
        trigger_history_refresh()

    def report_goal_once() -> None:
        """Record the reference target once per task; never stop or pause reservations."""
        nonlocal goal_reported
        if goal_reported:
            return
        goal_reported = True
        if task is not None:
            task.data['goal_reported'] = True
            task.save()
        emit('goal_completed')

    def ensure_hold(wid: str, start: float, end: float, *, confirmed: bool, source: str,
                    category: str | None = None, save: bool = True) -> bool:
        """Add/confirm one activity's local interval; return True on a new confirmation."""
        if task is None:
            return False
        holds = task.data.get('holds')
        if holds is None:
            holds = task.data['holds'] = {}
        if not isinstance(holds, dict):
            raise ValueError('state_invalid')
        entry = holds.get(wid)
        if not isinstance(entry, dict):
            now = clock()
            until = pending_hold_until(start, now)
            entry = dict(start=float(start), end=float(end), since=now,
                         until=until, attempts=0, confirmed=False)
            holds[wid] = entry
        if category and not entry.get('category'):
            entry['category'] = category
        newly_confirmed = False
        if confirmed and not entry.get('confirmed'):
            entry['confirmed'] = True
            newly_confirmed = True
        if save:
            task.save()
            if newly_confirmed:
                emit_success(wid, source)
        return newly_confirmed

    def remove_hold(wid: str) -> None:
        if task is None:
            return
        holds = task.data.get('holds')
        if isinstance(holds, dict) and holds.pop(wid, None) is not None:
            task.save()

    def hold_views() -> list[Mapping[str, Any]]:
        views: list[Mapping[str, Any]] = []
        for wid, entry in hold_entries().items():
            if not isinstance(entry, dict):
                continue
            try:
                start = datetime.fromtimestamp(float(entry['start']), SHANGHAI)
                end = datetime.fromtimestamp(float(entry['end']), SHANGHAI)
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                continue
            views.append({'activity': wid, 'start': start, 'end': end,
                          'attempts': int(entry.get('attempts', 0)),
                          'confirmed': bool(entry.get('confirmed'))})
        return views

    def notification_details() -> str:
        counts = '已预约：未知\n参考目标：未知\n必需类别缺口：未知'
        if latest_progress is not None:
            missing = '、'.join(label for category, label in zip(REQUIRED_CATEGORIES, ('心理健康', '法律', '艺术'))
                               if category in latest_progress.missing) or '无'
            counts = (f'已预约：{latest_progress.total} / 8（参考目标）\n'
                      f'参考达标：{"是" if latest_progress.complete else "否"}\n必需类别缺口：{missing}')
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
                   '进度为最近一次事件刷新得到的已预约记录；未知或较旧时请检查本地运行状态。')

    def pause(seconds: float, wake: Callable[[float], bool] | None = None) -> None:
        deadline = timer() + max(0.0, seconds)
        while True:
            check_stopped()
            if wake is None:
                work_updated.clear()
            flush_mail()
            duration = max(0.0, deadline - timer())
            if health_dirty and background_poller and wait_for_update is None:
                until_health = health_written_at + HEALTH_WRITE_INTERVAL - clock()
                if until_health > 0:
                    duration = min(duration, until_health)
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
            if duration <= 0:
                return
            if stop is not None:
                duration = min(duration, 0.2)
            # Persist the latest coalesced snapshot at each wait boundary so --status and an
            # outside observer always see the state the scan is about to wait in.
            flush_health()
            duration = min(duration, max(0.0, deadline - timer()))
            if duration <= 0:
                continue
            # An opening-window scan may cut the wait short when the background poller
            # publishes a new list snapshot. ``wake`` blocks for the duration itself, so a
            # False return already consumed the wait and must not be followed by a sleep.
            if wake is not None:
                if wake(duration):
                    return
            elif background_poller and sleep is time.sleep:
                work_updated.wait(duration)
            else:
                sleep(duration)
            if clock is not time.time and monotonic is None:
                # Legacy offline sleepers may only record waits, without advancing their clock.
                seconds -= duration
                deadline = min(deadline, timer() + max(0.0, seconds))
    def ask(label: str) -> str:
        health(phase='manual_required', next_check_at=0)
        return prompt(label)

    def remember_reservations(rows: list[dict[str, Any]], at: float) -> None:
        nonlocal health_dirty
        if task is None:
            return
        try:
            items = reservation_display(rows, at)
        except ValueError:
            return
        task.data['reservations'] = dict(at=at, items=items)
        health_dirty = True

    def pause_for_update(seconds: float) -> None:
        def wait(seconds: float) -> bool:
            return (wait_for_update(work_updated, seconds) if wait_for_update is not None
                    else work_updated.wait(seconds))
        pause(seconds, wait if background_poller else None)

    def reader_wait_seconds() -> float:
        if not background_poller:
            return query_interval()
        now = clock()
        deadlines = [boundary - now for start, end in known_openings
                     for boundary in (start - 30, start - 5, min(start + 9, end), end)
                     if boundary > now]
        deadlines += [at - now for at in (poller_retry_at, history_retry_at, next_preparation_at)
                      if at is not None and at > now]
        start = next_opening(now)
        if (settings.captcha_prefetch and start is not None and start != prefetched_for
                and task is not None and task.data['status'] != 'pending'):
            until_prefetch = start - CAPTCHA_PREFETCH_LEAD_SECONDS - now
            if until_prefetch > 0:
                deadlines.append(until_prefetch)
        if pending_check_due > timer():
            deadlines.append(pending_check_due - timer())
        return min([SCAN_INTERVAL, *deadlines])

    def query_mine() -> list[dict[str, Any]]:
        """Event-driven "my activities" refresh through its owner; returns the newest snapshot.

        The history reader has no periodic schedule: this is the query path used by
        authentication and pending confirmation. A failed refresh
        never invalidates the previous snapshot: there is no time-based validity window, and the
        caller decides whether the last known history is still usable.
        """
        nonlocal history_request, last_mine_started
        assert client is not None
        if not background_poller:
            last_mine_started = clock()
            rows = client.mine()
            remember_reservations(rows, clock())
            return rows
        poller_wait_error()
        poller = history_poller
        if poller is None:
            raise QueryInFlight
        if not poller.running:
            failed = poller.snapshot()
            if failed is not None and failed.error is not None:
                reader_stopped('history', failed)
            raise_reader_error(failed)
            raise QueryInFlight
        after = (max(float(task.data.get('pending_since', 0)), pending_read_after or 0)
                 if task and task.data['status'] == 'pending' else 0)
        snapshot, version = poller.snapshot_with_version()
        if after > 0 and history is not None and snapshot is not None and snapshot.error is None:
            started = getattr(snapshot, 'started', 0) or snapshot.at
            if started == last_mine_started and started >= after and snapshot.at == mine_at:
                return history
        if history_request is None or history_request[0] is not poller:
            history_request = (poller, poller.request_refresh())
            snapshot, version = poller.snapshot_with_version()
        if snapshot is None or (history_request is not None and version < history_request[1]):
            raise QueryInFlight
        if snapshot.error is not None:
            # Let the next check track the reader's retry instead of consuming the same failure.
            history_request = None
            raise_reader_error(snapshot)
        started = getattr(snapshot, 'started', 0) or snapshot.at
        if started < after:
            history_request = (poller, poller.request_refresh())
            raise QueryInFlight
        if not accept_background_history(snapshot):
            history_request = None
            raise RemoteError('business_rows_invalid')
        history_request = None
        last_mine_started = started
        return list(snapshot.rows)

    def raise_reader_error(snapshot: Any) -> None:
        if snapshot is None or snapshot.error is None:
            return
        if snapshot.server_directed:
            poller_wait_error()
            # The recorded deadline has expired; do not re-arm its old Retry-After.
            raise QueryInFlight
        reason = getattr(snapshot, 'reason', None) or 'network_failure'
        if snapshot.error == 'AuthenticationExpired':
            raise AuthenticationExpired(reason)
        raise ReaderFailure(reason)

    def reader_recovered(reader: str) -> None:
        missing_readers.discard(reader)
        missing_since.pop(reader, None)
        missing_diagnosed.discard(reader)
        stalled_readers.discard(reader)

    def reader_missing(reader: str) -> None:
        """Track one "no reliable snapshot" outage and its local diagnostics."""
        now = clock()
        if reader not in missing_readers:
            missing_readers.add(reader)
            missing_since[reader] = now
            missing_diagnosed.discard(reader)
            stalled_readers.discard(reader)
        poller = activity_poller if reader == 'activities' else history_poller
        started = getattr(poller, 'in_flight_since', None)
        # A reader that has not completed its first query is starting up, not lost; the initial
        # wait is ordinary and must not look like an outage in the local log.
        snapshot = poller.snapshot() if poller is not None else None
        if reader not in missing_diagnosed and not (poller is not None and snapshot is None):
            diagnostic('reader_snapshot_missing', reader=reader,
                       reader_in_flight=started is not None, reader_request_started_at=started)
            missing_diagnosed.add(reader)
        if (reader not in stalled_readers
                and now - missing_since.get(reader, now) >= READER_STALL_SECONDS):
            diagnostic('reader_stalled', reader=reader,
                       missing_seconds=round(now - missing_since.get(reader, now), 3),
                       in_flight_seconds=(round(now - started, 3)
                                          if started is not None else None))
            stalled_readers.add(reader)

    try:
        if not check_only:
            if notify is None and Mailer.configured(environ):
                try:
                    mailer = Mailer(environ)
                    notify = mailer
                except ValueError:
                    print('event=mail_configuration_invalid', flush=True)
                    print('邮件配置不完整或格式错误。QQ、163、126、Gmail 只需在 .env 中填写 SMTP_USERNAME（完整邮箱地址）'
                          '和 SMTP_PASSWORD（授权码或应用专用密码）；其他邮箱需指定 SMTP_HOST。'
                          '高级配置请检查 SMTP_PORT、MAIL_FROM、MAIL_TO。清空邮件配置可关闭通知。', flush=True)
                    return 1
            snapshot['mail_status'] = 'ready' if notify else 'disabled'
        client = Client(session or requests.Session(), environ, ask, emit,
                        trust_device=settings.trust_device,
                        fingerprint_dir=settings.state_dir,
                        diagnostic=diagnostic)
        if check_only:
            stage = 'authentication'
            client.authenticate()
            stage = 'my_activities'
            mine = client.mine()
            current = progress(mine, clock())
            stage = 'activities'
            _, selection_details = select_candidates(client.activities(), settings, current, mine, {}, clock())
            public_details = [{key: value for key, value in item.items() if key != 'wid'}
                              for item in selection_details]
            print('activity_details=' + json.dumps(public_details, ensure_ascii=False), flush=True)
            stage = 'captcha'
            client.captcha()
            print('event=read_only_complete', flush=True)
            return 0
        task = checkpoint if checkpoint is not None else Task(settings.state_dir, environ.get('SEU_USERNAME', ''))
        goal_reported = bool(task.data.get('goal_reported'))
        # One startup rotation, before the scan writes any diagnostic: never while running.
        rotate_diagnostics(settings.state_dir)
        if settings.trust_device:
            # Opt-in trusted device: the fingerprint itself is created lazily at login. Report
            # only whether one already exists; its value must never reach any log.
            diagnostic('device_trust_enabled',
                       fingerprint_present=(settings.state_dir / FINGERPRINT_FILE).exists())
        alerts = task.data.setdefault('alerts', {})
        if notify:
            announced.update(alerts)
        previous = task.data.get('health', {})
        if isinstance(previous, dict):
            snapshot.update(mail_history(previous))
            if notify and snapshot['last_mail_status'] in ('accepted', 'failed', 'sending'):
                snapshot['mail_status'] = snapshot['last_mail_status']
        mail_state_ready = True
        start_mail_worker()
        health(phase='starting')
        if new_task and task.data['status'] != 'done':
            raise ValueError('active_task_cannot_be_reset')
        record('task_started')
        legacy_hold = task.data.get('hold')
        if isinstance(legacy_hold, dict):
            wid = legacy_hold.get('activity')
            if isinstance(wid, str) and wid:
                holds = task.data.get('holds') or {}
                task.data['holds'] = holds
                holds.setdefault(wid, dict(
                    start=legacy_hold.get('start', 0), end=legacy_hold.get('end', 0),
                    since=legacy_hold.get('since', 0), until=legacy_hold.get('until', 0),
                    attempts=legacy_hold.get('attempts', 0), confirmed=False))
                task.data.pop('hold', None)
                task.save()
        if normalize_hold_deadlines(hold_entries()):
            task.save()
        checkpoint = task
        def defer(seconds: float, *, server_directed: bool = False) -> None:
            if server_directed:
                wait_gate.record(seconds)
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.data['retry_server'] = True
            checkpoint.save()
            health(phase='backoff', next_check_at=checkpoint.data['retry_at'])
            pause(seconds)
            checkpoint.data['retry_at'] = 0
            checkpoint.data.pop('retry_server', None)
            checkpoint.save()

        def reset_pending_checks() -> None:
            """A scan-level abort restarts the consecutive-valid pending-check count."""
            if (task is not None and task.data.get('status') == 'pending'
                    and task.data.pop('pending_checks', None) is not None):
                task.save()

        if task.data['cooldowns']:
            task.data['cooldowns'] = {}
            task.save()
        remaining = task.data.get('retry_at', 0) - clock()
        if remaining > 0 and task.data.get('retry_server') is True:
            defer(remaining, server_directed=True)
        elif task.data.get('retry_at') or 'retry_server' in task.data:
            task.data['retry_at'] = 0
            task.data.pop('retry_server', None)
            task.save()
        authenticated = False
        failures = 0
        unconfirmed_checks = 0
        # The unconfirmed event is per pending episode. The marker lives in task.data: a failed
        # check resets pending_checks and a process restart reloads the state,
        # so a process-local flag would announce the same episode again in both cases.
        # It is cleared whenever a new pending episode starts and whenever one is resolved.
        last_timing_at = 0.0
        history: list[dict[str, Any]] | None = None
        mine_at = 0.0
        mine_seconds = 0.0
        refresh_mine = False

        def next_opening(now: float) -> float | None:
            return min((start for start, _ in known_openings if start > now), default=None)

        def background_client(reader: str) -> Client:
            clone = client.clone_readonly()
            clone.request_role = reader
            return clone

        def background_read(reader: str, clone: Client) -> list[dict[str, Any]]:
            if reader == 'activities':
                clone.timeout = read_policy(known_openings, clock()).timeout
                return clone.activities()
            clone.timeout = HISTORY_TIMEOUT
            rows = clone.mine()
            invalid = background_history_usable(rows, clock())
            if invalid is not None:
                raise RemoteError(invalid)
            return rows

        def reader_retry(failures: int, snapshot: Any) -> float:
            if snapshot is not None and (snapshot.server_directed or snapshot.reason == 'service_unavailable'):
                return short_backoff(failures)
            return 0.0

        def captcha_max_age() -> float:
            return (SUBMIT_TIMEOUT if opening_intervals(
                known_openings, clock()) is not None else 10.0)

        def trigger_history_refresh() -> None:
            """Notify the history owner after a confirmed reservation, without waiting."""
            poller = history_poller
            if poller is not None and poller.running:
                poller.request_refresh()

        def accept_background_history(snapshot: Any) -> bool:
            """Consume one structurally valid history snapshot; True when accepted.

            There is no time-based validity: an accepted snapshot stays authoritative until the
            next refresh event (or a process restart). Manual school-side changes are therefore
            observed only after an event or a restart.
            """
            nonlocal history, mine_at, mine_seconds, refresh_mine, history_request
            if snapshot is None or snapshot.error is not None:
                return False
            rows = list(snapshot.rows)
            invalid = background_history_usable(rows, clock())
            if invalid is not None:
                diagnostic('history_invalid', reason=invalid)
                return False
            reader_recovered('history')
            history = rows
            history_request = None
            mine_at = snapshot.at
            remember_reservations(rows, mine_at)
            mine_seconds = getattr(snapshot, 'seconds', 0.0) or 0.0
            refresh_mine = True
            diagnostic('history_refreshed', seconds=round(mine_seconds, 3),
                       rows=len(rows), source='event')
            return True

        def reader_stopped(reader: str, snapshot: Any) -> None:
            """Diagnose one failed background reader just before it is recycled.

            Local-only: a recycled reader is not an event or mail-worthy condition, but without
            this it is invisible (``poller_polls`` resets and the scan falls back silently). The
            fixed reason code keeps server text, titles and IDs out of the log. The opening fast
            path recreates a failed reader every 0.1 s, so this diagnostic is throttled locally.
            """
            now = clock()
            if now - reader_diag_logged_at.get(reader, float('-inf')) < BACKGROUND_READER_DIAG_INTERVAL:
                return
            reader_diag_logged_at[reader] = now
            diagnostic('background_reader_stopped', reader=reader,
                       reason=getattr(snapshot, 'reason', None) or snapshot.error,
                       server_directed=snapshot.server_directed,
                       retry_seconds=round(snapshot.retry_after, 3))

        def sync_poller() -> None:
            """Keep one activity-list owner alive while authenticated."""
            nonlocal activity_poller, poller_client, poller_retry_at
            if shared_wait_pending():
                return
            if (not background_poller or check_only or not authenticated):
                absorb_poller_wait()
                stop_poller()
                return
            if reader_draining('activities'):
                return
            if activity_poller is not None and activity_poller.running:
                update_reader_policy('activities', activity_poller)
                return
            if activity_poller is not None:
                snapshot = activity_poller.snapshot()
                if snapshot is not None and snapshot.error is not None:
                    reader_stopped('activities', snapshot)
                    poller_retry_at = max(
                        poller_retry_at,
                        clock() + max(reader_retry(1, snapshot), snapshot.retry_after))
                    absorb_poller_wait()
                stop_poller()
            if clock() < poller_retry_at:
                return
            if poller_client is None:
                if preparation is not None:
                    return
                poller_client = background_client('activities')
            session = getattr(poller_client, 'session', None)
            activity_poller = ActivityPoller(functools.partial(background_read, 'activities', poller_client),
                                             interval=query_interval, clock=clock,
                                             gate=wait_gate,
                                             on_exit=functools.partial(close_session, session),
                                             on_update=functools.partial(work_updated.set, 'activities'), retry_delay=reader_retry,
                                             name='activities-reader')
            activity_poller.start()
            update_reader_policy('activities', activity_poller)

        def sync_history_poller() -> None:
            """Keep one history owner alive while authenticated."""
            nonlocal history_poller, history_client, history_retry_at
            if shared_wait_pending():
                return
            if (not background_poller or check_only or not authenticated):
                absorb_history_wait()
                stop_history_poller()
                return
            if reader_draining('history'):
                return
            if history_poller is not None and history_poller.running:
                absorb_history_wait()
                if settings.prewarm:
                    now = clock()
                    entering = {start for start, end in known_openings
                                if in_fast_opening(start, end, now)} - prewarmed_openings
                    if entering and history_poller.request_refresh():
                        prewarmed_openings.update(entering)
                        diagnostic('prewarm_history_requested')
                return
            if history_poller is not None:
                snapshot = history_poller.snapshot()
                if snapshot is not None and snapshot.error is not None:
                    reader_stopped('history', snapshot)
                    history_retry_at = max(
                        history_retry_at,
                        clock() + max(reader_retry(1, snapshot), snapshot.retry_after))
                    absorb_history_wait()
                stop_history_poller()
            if shared_wait_pending():
                return
            if clock() < history_retry_at:
                return
            if history_client is None:
                if preparation is not None:
                    return
                history_client = background_client('history')
            query = getattr(history_client, 'mine', None)
            if query is None:
                stop_history_poller()
                return
            session = getattr(history_client, 'session', None)
            history_poller = new_history_poller(functools.partial(background_read, 'history', history_client),
                                                interval=None,
                                                clock=clock, gate=wait_gate,
                                                on_exit=functools.partial(close_session, session),
                                                on_update=functools.partial(work_updated.set, 'history'),
                                                retry_delay=reader_retry, retry_interval=query_interval)
            history_poller.start()

        def update_reader_policy(reader: str, poller: ActivityPoller) -> None:
            policy = read_policy(known_openings, clock())
            old = reader_policies.get(reader)
            reader_policies[reader] = policy
            reschedule = getattr(poller, 'reschedule', None)
            if reader == 'activities' and old is not None and old != policy and reschedule is not None:
                reschedule()

        def query_interval() -> float:
            return scan_delay([], clock(), 0, SCAN_INTERVAL, known_openings)[0]

        def list_snapshot_max_age() -> float:
            return read_policy(known_openings, clock()).list_max_age

        def list_snapshot(*, for_scan: bool) -> tuple[Any, int]:
            poller = activity_poller
            if poller is None:
                return None, 0
            if for_scan:
                cached = getattr(poller, 'snapshot_for_scan', None)
                if cached is not None:
                    return cached()
            versioned = getattr(poller, 'snapshot_with_version', None)
            if versioned is not None:
                return versioned()
            return poller.snapshot(), getattr(poller, 'polls', 0)

        def list_rows(*, after: float = 0.0) -> tuple[list[dict[str, Any]], float, str, float, float, float]:
            """Consume a fresh owner snapshot; direct reads are an offline test adapter.

            Returns ``(rows, query_seconds, source, snapshot_age, fetch_seconds, data_age)``:
            snapshot age counts from the poller response, fetch seconds is the query duration and
            data age counts from the request start, which is what a candidate's staleness means.
            """
            nonlocal consumed_polls, last_rows_started, rows_snapshot_at
            poller = activity_poller

            snapshot, version = list_snapshot(for_scan=not after)
            if (poller is not None and snapshot is not None and snapshot.error is None
                    and 0 <= clock() - snapshot.at <= list_snapshot_max_age()
                    and (getattr(snapshot, 'started', 0) or snapshot.at) >= after):
                reader_recovered('activities')
                consumed_polls = version
                started = getattr(snapshot, 'started', 0.0) or snapshot.at
                last_rows_started = started
                rows_snapshot_at = snapshot.at
                return (snapshot.rows, 0.0, 'poller', max(0.0, clock() - snapshot.at),
                        getattr(snapshot, 'seconds', 0.0) or 0.0, max(0.0, clock() - started))
            if background_poller:
                reader_missing('activities')
                poller_wait_error()
                raise_reader_error(poller.snapshot() if poller is not None else None)
                if after and poller is not None and poller.running:
                    poller.request_refresh()
                return [], 0.0, 'none', 0.0, 0.0, 0.0
            started = clock()
            rows = client.activities()  # synchronous adapter for isolated/offline callers
            duration = max(0, clock() - started)
            last_rows_started = started
            rows_snapshot_at = clock()
            return rows, duration, 'query', 0.0, duration, duration

        def newer_poller_rows() -> tuple[
                list[dict[str, Any]], str, float, float, float, int, float, float] | None:
            """Newest fresh poller rows if they are newer than the rows already in hand."""
            snapshot, version = list_snapshot(for_scan=True)
            if snapshot is None or snapshot.error is not None:
                return None
            age = clock() - snapshot.at
            if not (0 <= age <= list_snapshot_max_age()):
                return None
            if version <= consumed_polls and snapshot.at <= rows_snapshot_at:
                return None
            started = getattr(snapshot, 'started', 0.0) or snapshot.at
            return (snapshot.rows, 'poller', max(0.0, age),
                    getattr(snapshot, 'seconds', 0.0) or 0.0,
                    max(0.0, clock() - started), version, snapshot.at, started)

        # Up to two captcha prefetch attempts per opening also warm the main session connection.
        prefetched_code: str | None = None
        unsubmitted_captcha: tuple[PreparedCaptcha, float] | None = None
        prefetched_at = 0.0
        prefetched_seconds = 0.0
        prefetched_ocr_seconds = 0.0
        prefetched_for = 0.0
        prefetch_attempts: dict[float, int] = {}
        prefetch_skipped: set[float] = set()

        def captcha_fetch(budget: float) -> Callable[[], PreparedCaptcha]:
            nonlocal recognize
            if recognize is None:
                recognize = load_recognizer()
            recognizer = recognize
            expires = timer() + budget
            def fetch() -> PreparedCaptcha:
                available = expires - timer()
                if available <= 0:
                    return PreparedCaptcha('', clock(), 0.0, 0.0, None, True)
                server_wait = wait_gate.remaining()
                if server_wait > 0:
                    raise RemoteError('server_backoff', server_wait, http_status=429)
                saved_timeout = client.timeout
                client.timeout = available
                try:
                    started = clock()
                    binary = client.captcha()
                    fetched_at = clock()
                    seconds = max(0.0, fetched_at - started)
                    ocr_started = clock()
                    try:
                        code = recognizer(binary).strip()
                    except Exception:
                        raise RemoteError('ocr_failed') from None
                    return PreparedCaptcha(code, fetched_at, seconds,
                        max(0.0, clock() - ocr_started), client.last_connect_seconds,
                        timer() > expires)
                finally:
                    client.timeout = saved_timeout
            return fetch

        def dispatch_captcha(fetch: Callable[[], PreparedCaptcha], *, source: str) -> None:
            nonlocal captcha_worker, preparation, preparation_source, prefetched_code, unsubmitted_captcha
            poller_wait_error()
            if captcha_worker is None:
                captcha_worker = CaptchaWorker(functools.partial(work_updated.set, 'captcha'))
            captcha_worker.submit(fetch, generation=authentication_generation)
            prefetched_code = None
            unsubmitted_captcha = None
            preparation, preparation_source = captcha_worker, source
            diagnostic('captcha_job_started', source=source, job=captcha_worker.number,
                       generation=authentication_generation)

        def fresh_captcha(budget: float) -> PreparedCaptcha:
            nonlocal preparation
            fetch = captcha_fetch(budget)
            if not background_poller:
                return fetch()  # Deterministic direct-query adapter for offline callers.
            dispatch_captcha(fetch, source='fresh')
            assert preparation is not None
            while True:
                # Acknowledge before inspecting results, so completion cannot lose its wakeup.
                work_updated.clear()
                consume_captcha_updates()
                sync_poller()
                sync_history_poller()
                if preparation.ready:
                    break
                pause_for_update(reader_wait_seconds())
            completed, preparation = preparation, None
            result = completed.result()
            if completed.generation != authentication_generation:
                raise AuthenticationExpired('business_authentication_expired')
            if result.expired or not 0 <= clock() - result.at <= captcha_max_age():
                raise RemoteError('network_timeout')
            return result

        def consume_captcha_updates() -> None:
            poller_wait_error()
            # Reader failures do not invalidate a captcha. The main thread checks the list again
            # before submitting, and only an authentication change invalidates the session.
            activities_snapshot = activity_poller.snapshot() if activity_poller is not None else None
            history_snapshot = history_poller.snapshot() if history_poller is not None else None
            for result in (activities_snapshot, history_snapshot):
                if result is not None and result.error == 'AuthenticationExpired':
                    raise_reader_error(result)
            if (history_snapshot is not None and history_snapshot.error is None
                    and history_snapshot.at > mine_at):
                accept_background_history(history_snapshot)

        def accept_preparation(result: PreparedCaptcha, opening_at: float) -> bool:
            nonlocal prefetched_code, prefetched_at, prefetched_seconds, prefetched_ocr_seconds
            if result.expired:
                diagnostic('captcha_prefetch_failed', reason='deadline_expired')
            elif not result.code or not result.code.isascii() or not result.code.isalnum() or len(result.code) > 16:
                diagnostic('captcha_prefetch_invalid')
            elif not 0 <= clock() - result.at <= CAPTCHA_PREFETCH_TIMEOUT:
                diagnostic('captcha_prefetch_stale', age_seconds=round(clock() - result.at, 3))
            else:
                prefetched_code, prefetched_at = result.code, result.at
                prefetched_seconds, prefetched_ocr_seconds = result.seconds, result.ocr_seconds
                diagnostic('captcha_prefetched', seconds=round(result.seconds, 3),
                    ocr_seconds=round(result.ocr_seconds, 3), lead_seconds=round(opening_at-result.at, 3),
                    connect_seconds=round(result.connect_seconds, 3) if result.connect_seconds is not None else None)
                return True
            return False

        def collect_preparation(*, discard: bool = False) -> None:
            nonlocal preparation, prefetched_for
            if preparation is None or not preparation.ready:
                return
            completed, preparation = preparation, None
            source = preparation_source
            attempt = prefetch_attempts.get(prefetched_for, 0) or None
            discard = (discard or completed.generation != authentication_generation
                       or source != 'prefetch')
            failed_event = 'captcha_prefetch_failed' if source == 'prefetch' else 'captcha_job_discarded'
            try:
                result = completed.result()
                if discard:
                    diagnostic('captcha_job_discarded', source=source,
                               job=completed.number, generation=completed.generation)
                elif not accept_preparation(result, prefetched_for):
                    # A failed prefetch attempt may be retried once per opening.
                    prefetched_for = 0.0
            except (RemoteError, ValueError) as exc:
                if not discard and (isinstance(exc, AuthenticationExpired) or server_directed_error(exc)):
                    raise
                if not discard:
                    prefetched_for = 0.0
                diagnostic(failed_event, reason=type(exc).__name__, attempt=attempt)
            except Exception as exc:
                if not discard:
                    prefetched_for = 0.0
                diagnostic(failed_event, reason=type(exc).__name__, attempt=attempt)

        def prepare_opening(start: float | None) -> float | None:
            """Start prefetch or return its scheduled wake time."""
            nonlocal prefetched_for
            if start is None:
                return None
            if (settings.captcha_prefetch and recognize is not None and preparation is None
                    and prefetch_attempts.get(start, 0) < CAPTCHA_PREFETCH_ATTEMPTS
                    and prefetched_for != start):
                now = clock()
                remaining = start - now
                target = start - CAPTCHA_PREFETCH_LEAD_SECONDS
                if now < target:
                    return target
                if remaining > CAPTCHA_PREFETCH_MIN_REMAINING:
                    consume_captcha_updates()
                    # Only prefetch before the opening instant; once it has started a candidate
                    # may already be waiting, so fetching off the path would only add requests.
                    prefetched_for = start
                    prefetch_attempts[start] = prefetch_attempts.get(start, 0) + 1
                    attempt = prefetch_attempts[start]
                    # The remaining lead sets when the job starts, not its TLS and read budget.
                    fetch = captcha_fetch(CAPTCHA_PREFETCH_TIMEOUT)
                    if background_poller:
                        dispatch_captcha(fetch, source='prefetch')
                        diagnostic('captcha_prefetch_started', attempt=attempt,
                                   lead_seconds=round(remaining, 3))
                    else:
                        # The direct adapter keeps deterministic clocks and fake sessions single-threaded.
                        try:
                            if not accept_preparation(fetch(), start):
                                prefetched_for = 0.0
                        except (RemoteError, ValueError) as exc:
                            if isinstance(exc, AuthenticationExpired) or server_directed_error(exc):
                                raise
                            prefetched_for = 0.0
                            diagnostic('captcha_prefetch_failed', reason=type(exc).__name__,
                                       attempt=attempt)
                        except Exception as exc:
                            prefetched_for = 0.0
                            diagnostic('captcha_prefetch_failed', reason=type(exc).__name__,
                                       attempt=attempt)
                elif start not in prefetch_skipped:
                    prefetch_skipped.add(start)
                    diagnostic('captcha_prefetch_skipped', reason='insufficient_remaining',
                               remaining=round(remaining, 3))
            elif settings.captcha_prefetch and recognize is None and start not in prefetch_skipped:
                prefetch_skipped.add(start)
                diagnostic('captcha_prefetch_skipped', reason='no_recognizer')
            return None

        def warm_main_session(start: float) -> None:
            """Leave a pooled connection on the session that captcha prefetch and submit share.

            The request is a normal list read whose body is consumed normally, so the keep-alive
            connection returns to the pool. A failure is only diagnosed: the prefetch still runs
            on its own schedule, and a server-directed wait is left to the normal scan backoff.
            """
            now = clock()
            budget = min(MAIN_SESSION_WARM_TIMEOUT,
                         start - CAPTCHA_PREFETCH_LEAD_SECONDS - now - MAIN_SESSION_WARM_MARGIN)
            if budget < CAPTCHA_PREFETCH_MIN_REMAINING:
                return
            saved_timeout = client.timeout
            client.timeout = budget
            started = clock()
            try:
                client.activities()
            except (AuthenticationExpired, AuthenticationRejected):
                raise
            except RemoteError as exc:
                diagnostic('main_session_warm_failed', reason=str(exc),
                           seconds=round(max(0.0, clock() - started), 3))
                if server_backoff_error(exc):
                    raise
            except Exception as exc:
                diagnostic('main_session_warm_failed', reason=type(exc).__name__,
                           seconds=round(max(0.0, clock() - started), 3))
            else:
                diagnostic('main_session_warmed',
                           seconds=round(max(0.0, clock() - started), 3),
                           connect_seconds=(round(client.last_connect_seconds, 3)
                                            if client.last_connect_seconds is not None else None))
            finally:
                client.timeout = saved_timeout

        previous_selection = task.data.get('selection')
        details: list[dict[str, Any]] = (previous_selection.get('activities', [])
                                         if isinstance(previous_selection, dict) else [])
        known_targets = merge_openings({}, details, clock())
        known_openings = sorted(set(opening_windows(list(known_targets.values()))))[:128]
        while True:
            try:
                work_updated.clear()
                check_stopped()
                flush_mail()
                collect_preparation(discard=not authenticated)
                scan_started = clock()
                now = clock()
                known_openings = [window for window in known_openings if window[1] >= now - 1]
                opening_scan = opening_intervals(known_openings, now) is not None
                if preparation is None:
                    client.timeout = OPENING_QUERY_TIMEOUT if opening_scan else 10
                activities_source = 'query'
                # Establish any shared server-directed wait before either read-only thread is
                # (re)started: a thread must never issue a request inside another thread's wait.
                absorb_poller_wait()
                absorb_history_wait()
                poller_wait_error()
                sync_poller()
                sync_history_poller()
                if not authenticated:
                    if preparation is not None:
                        raise QueryInFlight
                    health(phase='authentication', next_check_at=0)
                    client.authenticate()
                    authenticated = True
                    authentication_generation += 1
                    clear_alert('authentication_required')
                    record('authentication_succeeded')
                    if recognize is None:
                        try:
                            recognize = load_recognizer()
                        except Exception as exc:
                            # Degrade to the lazy load at the first candidate; the run fails
                            # there only if OCR is genuinely unusable.
                            diagnostic('ocr_unavailable', reason=type(exc).__name__)
                    stop_poller()
                    sync_poller()
                    stop_history_poller()
                    # Starting the new owner is the login refresh; don't request it twice.
                    sync_history_poller()
                if task.data['status'] != 'pending':
                    holds_now = hold_entries()
                    now = clock()
                    expired = [wid for wid, entry in holds_now.items()
                               if isinstance(entry, dict) and now >= float(entry.get('until', 0))]
                    for wid in expired:
                        holds_now.pop(wid, None)
                        diagnostic('reservation_hold_expired', wid=wid)
                        record('reservation_hold_expired')
                    if expired:
                        task.save()
                confirmed = False
                mine_seconds = 0.0
                refresh_mine = False
                if task.data['status'] == 'pending':
                    # Positive evidence from either read-only source confirms. The poller snapshot
                    # is already in memory, so consult it before paying for a history query; it
                    # only counts when taken after the submission was persisted. Absence is never
                    # evidence here.
                    pending_wid = task.data['activity']
                    rows = None
                    activities_source = 'none'
                    pending_history: list[dict[str, Any]] | None = None
                    pending_error: BaseException | None = None
                    confirmed_newly = False
                    confirmed_override: str | None = None
                    # Absence only becomes evidence after a complete two-source check. Any
                    # failed or unusable source keeps pending and must not advance the
                    # unfilled counter toward automatic release.
                    source_complete = True
                    pending_since = float(task.data.get('pending_since', 0) or 0)
                    if pending_read_key != (pending_wid, pending_since):
                        pending_read_key = (pending_wid, pending_since)
                        pending_read_after = None
                        pending_check_due = 0.0

                    def abandon_check(error: BaseException) -> None:
                        # A failed, aborted or unusable check is not valid absence evidence:
                        # persist the restart of the consecutive-valid count before the
                        # scan-level backoff (or server-directed wait) retries it.
                        task.data.pop('pending_checks', None)
                        task.save()
                        raise error

                    poller_snapshot = (activity_poller.snapshot()
                                       if activity_poller is not None else None)
                    # A recovered pending checkpoint has no in-process save timestamp; its reader
                    # snapshots are all created after the restart, i.e. after the persisted
                    # checkpoint, so the older pre-save timestamp is a safe floor there too.
                    pending_evidence_after = (pending_saved_at if pending_saved_at > 0
                                              else pending_since)
                    if (poller_snapshot is not None and poller_snapshot.error is None
                            and poller_snapshot.at >= pending_evidence_after
                            and poller_snapshot.rows and not conflicting_records(poller_snapshot.rows)):
                        for row in poller_snapshot.rows:
                            try:
                                row_wid = activity_id(row)
                            except ValueError:
                                continue
                            if row_wid != pending_wid:
                                continue
                            try:
                                judged = Lecture.read(row, now=clock())
                            except ValueError:
                                break
                            if judged.availability == 'already_reserved':
                                confirmed_newly = ensure_hold(
                                    pending_wid, judged.start.timestamp(),
                                    judged.end.timestamp(), confirmed=True,
                                    source='pending_snapshot',
                                    category=judged.category, save=False)
                                confirmed = True
                                confirmed_override = 'pending_snapshot'
                                diagnostic('pending_snapshot_confirmed',
                                           age_seconds=round(clock() - poller_snapshot.at, 3))
                            break

                    if not confirmed:
                        positive = history_poller.snapshot() if history_poller is not None else None
                        if (positive is not None and positive.error is None
                                and positive.at >= pending_evidence_after
                                and background_history_usable(positive.rows, clock()) is None
                                and not conflicting_records(positive.rows)
                                and any(activity_id(row) == pending_wid and confirmed_reservation(row)
                                        for row in positive.rows)
                                and accept_background_history(positive)):
                            pending_history = list(positive.rows)
                            confirmed = True
                    if not confirmed and pending_check_due > timer():
                        remaining = pending_check_due - timer()
                        health(phase='confirming', next_check_at=clock() + remaining)
                        pause_for_update(remaining)
                        continue
                    if pending_read_after is None:
                        pending_read_after = max(pending_since, clock())

                    # Request both sources for this check. Either can confirm a reservation;
                    # only a complete pair can count toward releasing an unconfirmed submission.
                    probed_rows: list[dict[str, Any]] | None = None
                    probed_error: BaseException | None = None
                    probed_source = 'none'
                    if not confirmed and background_poller:
                        try:
                            probed_rows, _, activities_source, _, _, _ = list_rows(after=pending_read_after)
                            if conflicting_records(probed_rows):
                                raise RemoteError('activity_schema_unknown')
                            probed_source = activities_source
                        except (RemoteError, ValueError) as exc:
                            probed_rows = None
                            probed_error = exc
                            if server_wait_error(exc):
                                # 429/Retry-After applies to every request: honour it before the
                                # history query instead of sending one inside the requested wait.
                                abandon_check(exc)
                            reset_pending_checks()

                    if not confirmed:
                        health(phase='my_activities', next_check_at=0)
                        mine_started = clock()
                        try:
                            # The wait may have been learned after the scan-start check; abort
                            # before the history request instead of querying inside it.
                            poller_wait_error()
                            pending_history = query_mine()
                            history = pending_history
                            if not background_poller:
                                mine_at = clock()
                                mine_seconds = max(0, clock() - mine_started)
                            refresh_mine = True
                        except (RemoteError, ValueError) as exc:
                            if server_wait_error(exc):
                                abandon_check(exc)
                            reset_pending_checks()
                            pending_error = exc
                    if pending_history is not None and conflicting_records(pending_history):
                        # A conflicting "my activities" record is neither success nor absence
                        # evidence, and it must not advance the automatic-release count.
                        abandon_check(ValueError('attendance_record_conflict'))
                    booked = set()
                    if pending_history is not None:
                        try:
                            booked = {activity_id(row) for row in pending_history
                                      if confirmed_reservation(row)}
                        except ValueError as exc:
                            abandon_check(exc)
                    if pending_wid not in booked and not confirmed:
                        health(phase='activities', next_check_at=0)
                        list_error: BaseException | None = None
                        if background_poller:
                            rows = probed_rows
                            activities_source = probed_source if probed_rows is not None else 'none'
                            list_error = probed_error
                            if list_error is not None:
                                if server_wait_error(list_error):
                                    abandon_check(list_error)
                                pending_error = pending_error or list_error
                                source_complete = False
                        else:
                            try:
                                rows, _, activities_source, _, _, _ = list_rows(after=pending_read_after)
                            except (RemoteError, ValueError) as exc:
                                rows = None
                                list_error = exc
                                if server_wait_error(exc):
                                    abandon_check(exc)
                                pending_error = pending_error or exc
                                source_complete = False
                        if activities_source == 'none' and list_error is None:
                            # Defensive path for injected readers: nothing failed but the source is
                            # unavailable, so the check cannot prove absence. Keep the original
                            # no-fabricated-error behaviour.
                            delay = reader_wait_seconds()
                            health(phase='waiting', next_check_at=clock() + delay)
                            sync_poller()
                            pause_for_update(delay)
                            continue
                        if rows is not None and conflicting_records(rows):
                            # The activity list cannot rule the pending activity in or out.
                            abandon_check(RemoteError('activity_schema_unknown'))
                        if rows is not None:
                            for row in rows:
                                try:
                                    row_wid = activity_id(row)
                                except ValueError:
                                    # Without an activity ID this row cannot be ruled out as
                                    # the pending activity: the activity list cannot provide
                                    # absence evidence, so the check must not advance.
                                    source_complete = False
                                    break
                                if row_wid != pending_wid:
                                    continue
                                try:
                                    judged = Lecture.read(row, now=clock())
                                except ValueError:
                                    source_complete = False
                                    break
                                if judged.availability == 'already_reserved':
                                    confirmed_newly = ensure_hold(
                                        pending_wid, judged.start.timestamp(),
                                        judged.end.timestamp(), confirmed=True,
                                        source='pending_activity_list',
                                        category=judged.category, save=False)
                                    confirmed = True
                                break
                    if pending_wid in booked:
                        confirmed = True
                    if confirmed:
                        pending_check_due = 0.0
                        need_emit = False
                        if confirmed_override is not None:
                            source = confirmed_override
                            need_emit = confirmed_newly
                        elif pending_history is not None and pending_wid in booked:
                            confirmed_entry = hold_entries().get(pending_wid)
                            already = bool(isinstance(confirmed_entry, dict)
                                           and confirmed_entry.get('confirmed'))
                            holds = task.data.get('holds')
                            if isinstance(holds, dict):
                                holds.pop(pending_wid, None)
                            source = 'pending_history'
                            need_emit = not already
                        else:
                            source = 'pending_activity_list'
                            need_emit = confirmed_newly
                        task.data.update(status='scanning', activity='')
                        for field in ('pending_checks', 'pending_since', 'pending_start',
                                      'pending_end', 'pending_category',
                                      'pending_unconfirmed_notified'):
                            task.data.pop(field, None)
                        task.save()
                        clear_alert('reservation_unconfirmed')
                        health(last_query_at=clock(), failure_count=0)
                        step_recovered()
                        if need_emit:
                            emit_success(pending_wid, source)
                        unconfirmed_checks = 0
                    else:
                        if pending_history is None:
                            abandon_check(pending_error if pending_error is not None
                                          else RemoteError('network_timeout'))
                        if not source_complete:
                            # A failed or unreadable activity-list source is not absence
                            # evidence: keep pending and let the scan-level backoff retry the
                            # check instead of counting it toward automatic release.
                            abandon_check(pending_error if pending_error is not None
                                          else ValueError('pending_activity_unreadable'))
                        failures = 0
                        health(last_query_at=clock(), failure_count=0)
                        query_recovered()
                        if task.data.get('pending_since') is None:
                            task.data['pending_since'] = clock()
                        unconfirmed_checks = int(task.data.get('pending_checks', 0)) + 1
                        task.data['pending_checks'] = unconfirmed_checks
                        announce_unconfirmed = (unconfirmed_checks == 1
                                                and not task.data.get('pending_unconfirmed_notified'))
                        if announce_unconfirmed:
                            # Persist the marker in the same save as the count so a restart or a
                            # failed check cannot announce the same episode twice; starting a
                            # new pending episode clears it.
                            task.data['pending_unconfirmed_notified'] = True
                        task.save()
                        diagnostic('reservation_checked', wid=pending_wid, checks=unconfirmed_checks,
                                   check_started_at=pending_read_after,
                                   activities_started_at=last_rows_started, history_started_at=last_mine_started)
                        if announce_unconfirmed:
                            emit('reservation_unconfirmed')
                        if unconfirmed_checks >= PENDING_AUTO_RELEASE_CHECKS:
                            started = task.data.get('pending_start')
                            ended = task.data.get('pending_end')
                            category = task.data.get('pending_category')
                            if (isinstance(started, (int, float))
                                    and isinstance(ended, (int, float)) and ended > started):
                                ensure_hold(pending_wid, float(started), float(ended),
                                            confirmed=False, source='pending_release',
                                            category=category if isinstance(category, str) else None,
                                            save=False)
                                task.data.update(status='scanning', activity='')
                                for field in ('pending_checks', 'pending_since', 'pending_start',
                                              'pending_end', 'pending_category',
                                              'pending_unconfirmed_notified'):
                                    task.data.pop(field, None)
                                task.save()
                                clear_alert('reservation_unconfirmed')
                                diagnostic('reservation_unconfirmed_released', wid=pending_wid,
                                           checks=unconfirmed_checks,
                                           hold=pending_wid in hold_entries())
                                record('reservation_unconfirmed_released')
                                unconfirmed_checks = 0
                                continue
                        pause_value = PENDING_MISS_PAUSE
                        pending_check_due = timer() + pause_value if background_poller else 0.0
                        pending_read_after = None
                        health(phase='confirming', next_check_at=clock() + pause_value)
                        pause_for_update(pause_value)
                        continue
                # Consume the last published result without asking the history owner to query.
                background_history = (history_poller.snapshot()
                                      if history_poller is not None else None)
                if (background_poller and not refresh_mine and background_history is not None
                        and background_history.at > mine_at):
                    accept_background_history(background_history)
                health(phase='activities')
                try:
                    rows, activities_seconds, activities_source, rows_age, rows_fetch_seconds, \
                        rows_data_age = list_rows()
                    if activities_source == 'none':
                        raise QueryInFlight
                except (QueryInFlight, RemoteError) as exc:
                    if (background_poller and history is not None
                            and not isinstance(exc, (AuthenticationExpired, AuthenticationRejected))
                            and not server_directed_error(exc)):
                        next_preparation_at = prepare_opening(next_opening(clock()))
                    raise
                rows_fetched_at = clock()
                # Start the list reader before the history step: if resolving history takes
                # seconds, the scan can then consume a newer list snapshot instead of submitting
                # a candidate from the pre-wait rows.
                if opening_scan:
                    sync_poller()
                if task.data['status'] != 'pending' and hold_entries():
                    if conflicting_records(rows):
                        # A contradictory list cannot confirm a hold before the normal
                        # candidate validation runs; abort instead of trusting either row.
                        raise RemoteError('activity_schema_unknown')
                    for held_wid, held_entry in list(hold_entries().items()):
                        for row in rows:
                            try:
                                row_wid = activity_id(row)
                            except ValueError:
                                continue
                            if row_wid != held_wid:
                                continue
                            try:
                                judged = Lecture.read(row, now=clock())
                            except ValueError:
                                break
                            if (judged.availability == 'already_reserved'
                                    or held_entry.get('confirmed') and not held_entry.get('category')):
                                ensure_hold(held_wid, judged.start.timestamp(),
                                            judged.end.timestamp(), confirmed=True,
                                            source='activity_list', category=judged.category)
                            break
                if history is None:
                    poller_wait_error()
                    health(phase='my_activities', next_check_at=0,
                           activity_count=None, candidate_count=None)
                    mine_started = clock()
                    if background_poller:
                        raise_reader_error(background_history)
                        raise QueryInFlight
                    history = query_mine()
                    if not background_poller:
                        mine_at = clock()
                        mine_seconds = max(0, clock() - mine_started)
                    reader_recovered('history')
                assert history is not None
                if conflicting_records(history):
                    # Contradictory duplicates must not confirm an unconfirmed hold before the
                    # normal progress validation runs; abort instead of acting on either row.
                    raise ValueError('attendance_record_conflict')
                if hold_entries():
                    booked_wids: set[str] = set()
                    for row in history:
                        try:
                            if confirmed_reservation(row):
                                booked_wids.add(activity_id(row))
                        except ValueError:
                            continue
                    for held_wid, held_entry in list(hold_entries().items()):
                        if held_wid not in booked_wids:
                            continue
                        already_confirmed = bool(isinstance(held_entry, dict)
                                                 and held_entry.get('confirmed'))
                        remove_hold(held_wid)
                        diagnostic('reservation_hold_synced', wid=held_wid)
                        if not already_confirmed:
                            emit_success(held_wid, 'history')
                current = progress(progress_rows(history, hold_entries(), clock()),
                                   clock())
                latest_progress, progress_at = current, mine_at
                snapshot['progress'] = dict(reserved=dict(current.reserved), total=current.total,
                                            missing=sorted(current.missing), complete=current.complete,
                                            at=mine_at)
                if current.complete:
                    # Reference only: reaching the target never stops new reservations.
                    report_goal_once()
                if task.data['status'] == 'done':
                    # Old state files may still carry the removed terminal status; keep running.
                    task.data.update(status='scanning', activity='')
                    task.save()
                if clock() - rows_fetched_at > HISTORY_LIST_REFRESH_AFTER:
                    # Resolving history may have taken seconds; do not submit a candidate that the
                    # newer background list snapshot has already shown as full or superseded.
                    fresher = newer_poller_rows()
                    if fresher is not None:
                        (rows, activities_source, rows_age, rows_fetch_seconds, rows_data_age,
                         consumed_polls, rows_snapshot_at, last_rows_started) = fresher
                        activities_seconds = 0.0
                candidates, details = select_candidates(rows, settings, current, history,
                    task.data['cooldowns'], clock(),
                    holds=hold_views())
                query_recovered()
                if not candidates:
                    # No submission step can be attempted in this scan; do not keep an old
                    # captcha/submit failure timer alive while only list queries run.
                    step_recovered()
                known_targets = merge_openings(known_targets, details, clock())
                known_openings = sorted(set(opening_windows(list(known_targets.values()))))[:128]
                opening_scan = opening_intervals(known_openings, clock()) is not None
                if candidates and preparation is not None:
                    raise QueryInFlight
                if preparation is None:
                    client.timeout = captcha_max_age()
                sync_poller()
                sync_history_poller()
                previous_selection = task.data.get('selection')
                if (not isinstance(previous_selection, dict)
                        or previous_selection.get('activities') != details):
                    diagnostic('selection_changed', activities=details)
                task.data['selection'] = dict(updated_at=clock(), activities=details)
                health(last_query_at=clock(), failure_count=failures, activity_count=len(rows),
                       candidate_count=len(candidates), phase='captcha' if candidates else 'waiting')
                wake_at: float | None = None
                if not candidates and task.data['status'] != 'pending':
                    if (settings.prewarm and background_poller and authenticated
                            and preparation is None):
                        now = clock()
                        for start in ({start for start, end in known_openings
                                       if in_fast_opening(start, end, now)}
                                      - main_warmed_openings):
                            main_warmed_openings.add(start)
                            warm_main_session(start)
                    wake_at = prepare_opening(next_opening(clock()))
                next_preparation_at = wake_at
                prefetch_ready = False
                prefetch_age: float | None = None
                if prefetched_code is not None:
                    age = clock() - prefetched_at
                    if 0.0 <= age <= CAPTCHA_PREFETCH_TIMEOUT:
                        prefetch_ready = True
                        prefetch_age = age
                    else:
                        diagnostic('captcha_prefetch_stale', age_seconds=round(age, 3))
                        prefetched_code = None
                restart_scan = False
                for lecture in candidates:
                    for attempt in range(4):
                        # The poller may have failed after the scan start; do not fetch a
                        # captcha for a submission the server asked us to postpone.
                        poller_wait_error()
                        captcha_source = 'fresh'
                        captcha_connect: float | None = None
                        attempt_prefetch_age: float | None = None
                        if attempt == 0 and prefetch_ready and prefetched_code is not None:
                            # The captcha was fetched (and recognised) off the critical path;
                            # submit the candidate immediately.
                            code = prefetched_code
                            attempt_prefetch_age = prefetch_age
                            captcha_seconds = 0.0
                            ocr_seconds = 0.0
                            captcha_source = 'prefetch'
                        else:
                            if (unsubmitted_captcha is not None
                                    and unsubmitted_captcha[0].at <= clock() <= unsubmitted_captcha[1]):
                                result, _ = unsubmitted_captcha
                            else:
                                unsubmitted_captcha = None
                                budget = float(client.timeout)
                                try:
                                    result = fresh_captcha(budget)
                                except (AuthenticationExpired, AuthenticationRejected):
                                    raise
                                except RemoteError as exc:
                                    if server_directed_error(exc):
                                        raise
                                    step_failed(clock())
                                    diagnostic('captcha_error', wid=lecture.wid, reason=str(exc),
                                               attempt=attempt + 1)
                                    continue
                                unsubmitted_captcha = (result, result.at + budget)
                            code = result.code
                            captcha_seconds, ocr_seconds = result.seconds, result.ocr_seconds
                            captcha_connect = result.connect_seconds
                        if not code or not code.isascii() or not code.isalnum() or len(code) > 16:
                            unsubmitted_captcha = None
                            diagnostic('ocr_invalid', wid=lecture.wid, attempt=attempt + 1)
                            continue
                        # Recheck already available snapshots, without another school query.
                        if background_poller:
                            consume_captcha_updates()
                            (rows, activities_seconds, activities_source, rows_age,
                             rows_fetch_seconds, rows_data_age) = list_rows()
                            if activities_source == 'none' or history is None:
                                diagnostic('captcha_candidate_changed', wid=lecture.wid,
                                           reason='snapshot_unavailable')
                                restart_scan = True
                                break
                            current = progress(progress_rows(history, hold_entries(), clock()),
                                               clock())
                            refreshed, _ = select_candidates(rows, settings, current, history,
                                task.data['cooldowns'], clock(),
                                holds=hold_views())
                            updated = next((item for item in refreshed if item.wid == lecture.wid), None)
                            if updated is None:
                                unsubmitted_captcha = None
                                diagnostic('captcha_candidate_changed', wid=lecture.wid,
                                           reason='candidate_unavailable')
                                restart_scan = True
                                break
                            lecture = updated
                        # A wait learned during the round-trip must stop the submission.
                        poller_wait_error()
                        unsubmitted_captcha = None
                        if captcha_source == 'prefetch':
                            prefetched_code = None
                            prefetch_ready = False
                        # Persist before sending: a crash must recover by querying, never resubmitting.
                        retry_hold = hold_entries().get(lecture.wid)
                        if isinstance(retry_hold, dict):
                            retry_hold['attempts'] = min(PENDING_RETRY_LIMIT + 1,
                                                         int(retry_hold.get('attempts', 0)) + 1)
                        task.data.update(status='pending', activity=lecture.wid,
                                         pending_checks=0, pending_since=clock(),
                                         pending_start=lecture.start.timestamp(),
                                         pending_end=lecture.end.timestamp(),
                                         pending_category=lecture.category)
                        # A new pending episode starts with a clean unconfirmed-event marker.
                        task.data.pop('pending_unconfirmed_notified', None)
                        # One atomic save persists both the crash-recovery checkpoint and the
                        # 'submitting' health phase; a separate health write would fsync again.
                        set_health(phase='submitting')
                        persist_started = clock()
                        task.save()
                        # Positive pending evidence must come from a snapshot taken after the
                        # checkpoint is durable, not merely after its pre-save timestamp.
                        pending_saved_at = clock()
                        persist_seconds = max(0, clock() - persist_started)
                        unconfirmed_checks = 0
                        submission_started = clock()
                        diagnostic('submission_started', wid=lecture.wid, attempt=attempt + 1,
                                   captcha_seconds=round(captcha_seconds, 3),
                                   ocr_seconds=round(ocr_seconds, 3),
                                   captcha_source=captcha_source,
                                   prefetch_age=(round(attempt_prefetch_age, 3)
                                                 if attempt_prefetch_age is not None else None),
                                   connect_seconds=(round(captcha_connect, 3)
                                                    if captcha_connect is not None else None),
                                   rows_age=round(rows_age, 3),
                                   rows_data_age=(round(clock() - last_rows_started, 3)
                                                  if last_rows_started else None),
                                   yyrs=lecture.taken,
                                   hdzrs=lecture.capacity,
                                   remaining=lecture.remaining,
                                   remaining_after=(None if lecture.remaining is None
                                                    else max(0, lecture.remaining - 1)),
                                   precommit_save_seconds=round(persist_seconds, 3))
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
                                if isinstance(retry_hold, dict):
                                    # No request left, so it must not consume a retry of the
                                    # original activity either.
                                    retry_hold['attempts'] = max(0, int(retry_hold.get('attempts', 0)) - 1)
                                task.data.update(status='scanning', activity='')
                                task.data.pop('pending_checks', None)
                                task.data.pop('pending_since', None)
                                task.data.pop('pending_start', None)
                                task.data.pop('pending_end', None)
                                task.data.pop('pending_category', None)
                                task.data.pop('pending_unconfirmed_notified', None)
                                task.save()
                                diagnostic('submission_connect_failed', wid=lecture.wid)
                                break
                            if isinstance(exc, AuthenticationExpired):
                                authenticated = False
                            if not authenticated:
                                stop_poller()
                                stop_history_poller()
                            if server_backoff_error(exc):
                                wait = exc.retry_after if exc.retry_after > 0 else short_backoff(1)
                                defer(wait, server_directed=server_directed_error(exc))
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
                        already_reserved = isinstance(message, str) and any(
                            word in message for word in ('您已预约', '你已预约', '已预约该',
                                                         '已有预约', '已经预约', '重复预约', '已预约过'))
                        diagnostic('submission_reply', wid=lecture.wid, outcome=outcome,
                                   captcha_source=captcha_source,
                                   feedback=reply.get('feedback') if reply.get('feedback') in (
                                       'unrecognized_message', 'no_message') else None,
                                   http_status=reply.get('http_status') if type(reply.get('http_status')) is int
                                       and 100 <= reply['http_status'] <= 599 else None,
                                   connect_seconds=(round(client.last_connect_seconds, 3)
                                                    if client.last_connect_seconds is not None
                                                    else None),
                                   duration_seconds=max(0, clock() - submission_started))
                        if already_reserved and not has_code(reply, 0):
                            # Explicit evidence that the earlier submission is already registered.
                            # Keep pending until its replacement exists: Ctrl+C cleanup also saves.
                            newly_confirmed = ensure_hold(
                                lecture.wid, lecture.start.timestamp(), lecture.end.timestamp(),
                                confirmed=True, source='submit_reply',
                                category=lecture.category, save=False)
                            task.data.update(status='scanning', activity='')
                            for field in ('pending_checks', 'pending_since', 'pending_start',
                                          'pending_end', 'pending_category',
                                          'pending_unconfirmed_notified'):
                                task.data.pop(field, None)
                            task.save()
                            if newly_confirmed:
                                emit_success(lecture.wid, 'submit_reply')
                            restart_scan = True
                            break
                        if (has_code(reply, 0) or type(reply.get('code')) is not int
                                or not (captcha_wrong or unavailable)):
                            break
                        task.data.update(status='scanning', activity='')
                        if captcha_wrong and captcha_source == 'prefetch':
                            # A prefetched captcha was rejected; the next attempt fetches fresh.
                            diagnostic('captcha_prefetch_rejected', attempt=attempt + 1)
                        task.save()
                        if unavailable:
                            break
                    if restart_scan:
                        break
                    if task.data['status'] == 'pending':
                        break
                if restart_scan:
                    continue
                failures = 0
                clear_alert('task_failed')
                if task.data['status'] != 'pending':
                    elapsed = max(0, clock() - scan_started)
                    delay, fast = scan_delay(details, clock(), elapsed, SCAN_INTERVAL,
                                             known_openings)
                    if (fast != opening_scan or fast or not last_timing_at
                            or clock() - last_timing_at >= 300):
                        poller = activity_poller
                        diagnostic('scan_timing', mode='opening' if fast else 'normal',
                                   duration_seconds=elapsed, wait_seconds=delay,
                                   history_reused=not refresh_mine,
                                   mine_seconds=mine_seconds, activities_seconds=activities_seconds,
                                   rows_source=activities_source, rows_age=round(rows_age, 3),
                                   rows_fetch_seconds=round(rows_fetch_seconds, 3),
                                   rows_data_age=round(rows_data_age, 3),
                                   main_requests=client.requests_made,
                                   poller_polls=getattr(poller, 'polls', 0))
                        last_timing_at = clock()
                    opening_scan = fast
                    if not fast:
                        flush_mail()
                    health(phase='waiting', next_check_at=clock() + delay, failure_count=0)
                    query_recovered()
                    if wake_at is not None:
                        # Wake when captcha prefetch is due instead of drifting with the interval.
                        delay = min(delay, max(0.0, wake_at - clock()))
                    pause_for_update(delay)
                else:
                    health(phase='confirming', failure_count=0)
            except AuthenticationRejected:
                raise
            except QueryInFlight:
                delay = reader_wait_seconds()
                health(phase='confirming' if task.data['status'] == 'pending' else 'waiting',
                       next_check_at=clock() + delay)
                pause_for_update(delay)
            except AuthenticationExpired:
                prefetched_code = None
                unsubmitted_captcha = None
                pending_check_due = 0.0
                pending_read_after = None
                history = None
                authenticated = False
                unconfirmed_checks = 0
                reset_pending_checks()
                failures = 0
                query_failed(scan_started)
                health(last_error='authentication_expired', last_error_at=clock(), failure_count=failures)
                stop_poller()
                stop_history_poller()
                delay = reader_wait_seconds()
                health(phase='authentication', next_check_at=clock() + delay)
                pause_for_update(delay)
            except (RemoteError, ValueError) as exc:
                pending_read_after = None
                if not background_poller and isinstance(exc, ValueError):
                    # The direct adapter has no reader to validate history before use.
                    history = None
                unconfirmed_checks = 0
                reset_pending_checks()
                reason = str(exc)
                safe_reason = reason if isinstance(exc, RemoteError) or reason in (
                    'attendance_record_conflict',) else 'invalid_activity_data'
                # A repeated reader error while the opening fast path retries every 0.25 s must not
                # write hundreds of identical event lines: record the first one and any change.
                now = clock()
                if (last_recorded_error is None or last_recorded_error[0] != safe_reason
                        or now - last_recorded_error[1] >= 1.0):
                    last_recorded_error = (safe_reason, now)
                    record(safe_reason)
                query_failed(scan_started)
                opening_scan = opening_intervals(known_openings, clock()) is not None
                if not server_wait_error(exc):
                    failures = 0
                    delay = reader_wait_seconds()
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=0,
                           phase='confirming' if task.data['status'] == 'pending' else 'waiting',
                           next_check_at=clock() + delay)
                    pause_for_update(delay)
                    continue
                failures += 1
                health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                retry = exc.retry_after if isinstance(exc, RemoteError) else 0
                defer(retry if retry > 0 else short_backoff(failures),
                      server_directed=server_directed_error(exc))
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
            # The client normally emitted authentication_rejected before raising. Emit again only
            # when this process has not announced it yet (for example the credentials-missing
            # path), so one rejection does not write a duplicate event line. A persisted alert
            # still dedupes the mail: emit records the event but returns before notifying.
            if 'task_failed' not in announced_here:
                emit('authentication_rejected')
        else:
            emit('task_failed')
        if not check_only:
            flush_mail()
        return 1
    except Exception:
        final_phase = 'failed'
        health(last_error='unexpected_failure', last_error_at=clock())
        if check_only:
            print('event=unexpected_failure', flush=True)
        else:
            emit('unexpected_failure')
            flush_mail()
        return 1
    finally:
        stop_poller()
        stop_history_poller()
        if captcha_worker is not None:
            captcha_worker.close()
        if mail_worker is not None:
            if not mail_worker.close():
                record('notification_pending_on_exit')
            flush_mail()
        if task:
            health(phase='manual_required' if snapshot['phase'] == 'manual_required' and final_phase == 'stopped'
                   else final_phase, next_check_at=0)
            flush_health(force=True)
        try:
            local_logs.close()
        except OSError:
            print('event=local_log_failed', flush=True)
        finally:
            # Keep deletion/another writer excluded until persistent Windows log handles close.
            if task:
                task.close()
        if session is None and client is not None:
            client.session.close()
