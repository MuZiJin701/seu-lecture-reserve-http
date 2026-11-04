"""Application entry point: one task, with replaceable external dependencies."""

import functools
import json
import os
import random
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
from .model import (COFFEE_CATEGORY, GOAL_CATEGORIES, AttendanceCategoryUnknown, Lecture, Progress,
                    REQUIRED_CATEGORIES, SHANGHAI, activity_id, category_name,
                    confirmed_reservation, conflicting_records, interval, moment, progress,
                    reservation_display, reserved, test_notice)
from .mail import MailWorker, Mailer, mail_failure_reason, mail_history
from .poller import ActivityPoller, WaitGate
from .runtime import CaptchaWorker, LocalLogs, PreparedCaptcha, WorkSignal
from .state import FINGERPRINT_FILE, Task, open_nofollow

DEFAULT_CATEGORIES = GOAL_CATEGORIES + (COFFEE_CATEGORY,)
QUERY_FAILURE_SECONDS = 5 * 60
STEP_FAILURE_SECONDS = 5 * 60
MAJOR_MAIL_EVENTS = {'authentication_required', 'authentication_rejected', 'query_failure', 'task_failed',
                     'unexpected_failure', 'reservation_step_failure'}
SCAN_INTERVAL = 5
FAST_INTERVAL = 0.10
FAST_RETRY_INTERVAL = 0.25
# Transient opening failures retry quickly; explicit server waits still take precedence.
FAST_RETRY_REASONS = {'network_timeout', 'connect_timeout', 'network_failure', 'connection_failure',
                      'proxy_failure', 'invalid_json', 'invalid_response', 'http_failure',
                      'business_response_invalid', 'business_rows_invalid', 'business_rows_incomplete'}
SELECTION_REASONS = {'candidate', 'reservation_not_started', 'reservation_ended', 'capacity_full',
    'not_reservable', 'already_reserved', 'category_not_allowed', 'category_not_needed',
    'title_not_selected', 'online_disabled', 'offline_disabled', 'campus_not_selected',
    'time_conflict', 'cooldown', 'invalid_activity_time', 'notice_test_only', 'retry_limit'}
# A pending submission is verified repeatedly by activity ID before any automatic release: the
# submission is non-idempotent, so absence only becomes actionable after a short series of valid
# checks. Automatic release keeps unattended operation possible; the local log keeps the evidence.
PENDING_MISS_PAUSE = 2
PENDING_AUTO_RELEASE_CHECKS = 3
# The align callback runs in the poller thread and writes a diagnostic line; serialize the local
# log appends so a background line cannot interleave with a main-loop line.
_LOG_LOCK = threading.Lock()
# After the first release, keep the original activity's time interval as a local hold so a
# different lecture at the same time cannot be booked while the first submission is unresolved.
PENDING_HOLD_SECONDS = 600
PENDING_RETRY_LIMIT = 2


class QueryInFlight(Exception):
    """A required reader snapshot is not ready; wait without recording a query failure."""


def pending_hold_until(start: float, now: float, *, offset_known: bool,
                       server_offset: float) -> float:
    """Local-clock hold deadline for an activity start expressed in school time.

    ``start`` comes from the school rows (absolute school-time epoch) while ``now`` and the
    stored deadline are host-clock based. Before the school clock is calibrated there is no
    safe conversion, so keep the full local hold instead of expiring at a skewed activity start.
    """
    deadline = now + PENDING_HOLD_SECONDS
    if offset_known:
        deadline = min(deadline, float(start) - server_offset)
    return max(deadline, now)


def tighten_hold_deadlines(holds: Mapping[str, Any], *, offset_known: bool,
                           server_offset: float) -> bool:
    """Tighten stored local-clock hold deadlines once the school clock is known.

    Hold ``start`` values are school-time epochs while ``until`` is host-clock. Before calibration
    the hold keeps its full local fallback; afterwards it must not outlive the activity start by
    more than the clock skew. Returns True when a deadline changed.
    """
    if not offset_known:
        return False
    changed = False
    for entry in holds.values():
        if not isinstance(entry, dict) or not isinstance(entry.get('start'), (int, float)):
            continue
        start_local = float(entry['start']) - server_offset
        if start_local < float(entry.get('until', start_local)):
            entry['until'] = start_local
            changed = True
    return changed
# After the candidate appears, a slow successful submission must not be aborted by the fast list
# timeout; captcha and submit keep a longer budget while the list stays fast.
SUBMIT_TIMEOUT = 8
# Main-session setup budget; query owners use their independent read policy below.
OPENING_QUERY_TIMEOUT = 6
# Slow reads stay off the main thread without abandoning every response over half a second.
OPENING_BACKGROUND_TIMEOUT = 2.0
# Readers retain their sessions across transient failures and own their retry schedule.
OPENING_POLLER_RETRY = FAST_INTERVAL
# Legacy CLI without periodic history refresh retains the older opening cache lifetime.
OPENING_HISTORY_TTL = 60.0
NORMAL_HISTORY_TTL = 30.0
# Both opening phases allow the last success for 2 s; an error still invalidates its rows.
POLLER_SNAPSHOT_MAX_AGE = 2.0
# After history resolution, the list rows
# fetched before it may already be stale. Re-consume a newer poller snapshot above this age
# difference before selecting candidates.
HISTORY_LIST_REFRESH_AFTER = 0.5
# An independent history owner serves periodic and on-demand refreshes in every phase.
HISTORY_REFRESH_INTERVAL = 5.0
HISTORY_BACKGROUND_FRESH = 10.0
# Minimum spacing between on-demand history refreshes; keeps a broken history response from
# turning the proactive refresh into a tight request loop.
HISTORY_REFRESH_MIN_INTERVAL = 2.0
# When the school-clock estimate is this uncertain, prewarm/prefetch are skipped: timing them from
# an early/deadline guess is not safe, and the normal in-scan captcha fetch still works.
PREPARATION_MAX_UNCERTAINTY = 2.0
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
                                  'manual_required', 'failed', 'done', 'waiting_for_attendance'})
# Ask the history owner for a fresh result shortly before the opening, without main-thread I/O.
PREWARM_LEAD_SECONDS = 2.0
# Fetch and recognise a captcha just before the opening so the candidate can be submitted without
# the captcha round-trip on the critical path. The lead is also the timeout budget below: a slow
# prefetch must finish by the opening instant or fall back to a fresh fetch.
CAPTCHA_PREFETCH_LEAD_SECONDS = 0.6
# A prefetched captcha is only reused while this young; otherwise the submit path fetches fresh.
CAPTCHA_PREFETCH_MAX_AGE = 5.0
# A slow captcha prefetch must not consume the opening.
CAPTCHA_PREFETCH_TIMEOUT = 0.6
# When less time than this is left before the opening, preparation is skipped instead of being
# forced a minimum request budget that would run past the opening instant.
PREWARM_MIN_REMAINING = 0.4
CAPTCHA_PREFETCH_MIN_REMAINING = 0.45
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
                       retry_delay: Callable[[int, Any], float] | None = None) -> ActivityPoller:
    """Build the background "my activities" refresher (injectable for offline tests)."""
    return ActivityPoller(query, interval=interval, clock=clock, gate=gate, on_exit=on_exit,
                          on_update=on_update, retry_delay=retry_delay, name='history-reader')


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
    # Opening-window latency experiments; both keep the same request ordering and only shift
    # read-only work off the candidate-to-submit path.
    captcha_prefetch: bool = True
    prewarm: bool = True
    # Aligning a poller poll to the exact opening instant depends on the school-clock offset, which
    # carries up to a second of truncation/request-latency error; off by default until measured.
    poller_align: bool = False
    # Disable periodic history polling for legacy CLI; on-demand reads still use its owner.
    history_background: bool = True
    # Opt-in trusted device for the SMS second factor: off by default so the login payload keeps
    # its previous (untrusted) shape unless the user explicitly enables it.
    trust_device: bool = False

def server_wait_error(exc: BaseException) -> bool:
    """True when the school explicitly asked us to wait before another request."""
    return (isinstance(exc, RemoteError)
            and (exc.retry_after > 0 or str(exc) == 'server_backoff'))


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
        if not isinstance(category, str) or not category:
            raise AttendanceCategoryUnknown(wid)
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
    'activity_id_missing', 'publication_status_unknown', 'reservation_status_unknown',
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
                      now: float, *, cooldown_clock_known: bool = True,
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
        elif hold_conflict:
            reason = 'time_conflict'
        elif hold_retry:
            reason = 'retry_limit'
        elif lecture.wid in cooldowns and (not cooldown_clock_known or cooldowns[lecture.wid] > now):
            reason = 'cooldown'
        elif lecture.availability != 'candidate':
            reason = lecture.availability
        else:
            reason = 'candidate'
            candidates.append(lecture)
        details.append(dict(wid=lecture.wid, title=lecture.title, reason=reason,
            reservation_start=lecture.reservation_start.isoformat(timespec='seconds') if lecture.reservation_start else None,
            reservation_end=lecture.reservation_end.isoformat(timespec='seconds') if lecture.reservation_end else None))
    candidates.sort(key=lambda row: (row.category not in current.missing,
                    categories.index(row.category), not row.online, row.start, row.wid))
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


def opening_intervals(openings: list[tuple[float, float]], now: float) -> tuple[float, float] | None:
    active = [(start, end) for start, end in openings
              if start - 30 <= now < min(end, start + 300)]
    if not active:
        return None
    return (0.05, 0.10) if any(start - 5 <= now < start + 9 for start, _ in active) else (0.5, 1.5)


@dataclass(frozen=True)
class ReadPolicy:
    timeout: float
    list_max_age: float
    history_interval: float
    history_max_age: float


def read_policy(openings: list[tuple[float, float]], now: float) -> ReadPolicy:
    bounds = opening_intervals(openings, now)
    if bounds is None:
        return ReadPolicy(10.0, 10.0, 25.0, NORMAL_HISTORY_TTL)
    return ReadPolicy(OPENING_BACKGROUND_TIMEOUT, POLLER_SNAPSHOT_MAX_AGE,
                      HISTORY_REFRESH_INTERVAL, HISTORY_BACKGROUND_FRESH)


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


def school_clock_uncertainty(snapshot_age: float, request_seconds: float = 0.0) -> float:
    """Conservative lateness of a school-clock based local opening estimate.

    The school timestamp is second-truncated (up to one second), the snapshot is consumed later
    than it was produced, and a slow list request delays when the reading takes effect, so the
    naive ``start - offset`` can be later than the true opening by the truncation fraction plus
    the snapshot age plus the request duration. There is deliberately no small hard cap here: the
    caller skips preparation when the uncertainty is too large to time it safely.
    """
    return 1.1 + max(0.0, snapshot_age) + max(0.0, request_seconds)


def earliest_opening_local(start: float, offset: float, margin: float,
                           previous: float | None = None) -> float:
    """Earliest plausible local opening time; recomputation may only move it earlier."""
    candidate = start - offset - margin
    return candidate if previous is None else min(previous, candidate)


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
    unresolved_reported: frozenset[str] = frozenset()
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
    history_refresh_at = 0.0
    # Reader instance that produced the current refresh throttle window: a recycled reader must
    # be allowed to refresh immediately instead of inheriting the old reader's throttle.
    history_refresh_poller: ActivityPoller | None = None
    history_request: tuple[ActivityPoller, int] | None = None
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
            until = pending_hold_until(start, now, offset_known=offset_known,
                                       server_offset=server_offset)
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
        school_now = now + server_offset
        deadlines = [boundary - school_now for start, end in known_openings
                     for boundary in (start - 30, start - 5, min(start + 9, end), end)
                     if boundary > school_now]
        deadlines += [at - now for at in (poller_retry_at, history_retry_at, next_preparation_at)
                      if at is not None and at > now]
        opening = next_opening()
        if opening is not None and task is not None and task.data['status'] != 'pending':
            start = opening[0]
            local = opening_local_estimate(start)
            for enabled, lead in ((settings.prewarm and start not in prewarmed, PREWARM_LEAD_SECONDS),
                                  (settings.captcha_prefetch and start != prefetched_for,
                                   CAPTCHA_PREFETCH_LEAD_SECONDS)):
                if enabled and local - lead > now:
                    deadlines.append(local - lead - now)
        if pending_check_due > timer():
            deadlines.append(pending_check_due - timer())
        return min([SCAN_INTERVAL, *deadlines])

    def query_mine() -> list[dict[str, Any]]:
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
        if history_request is None or history_request[0] is not poller:
            history_request = (poller, poller.request_refresh())
        snapshot, version = poller.snapshot_with_version()
        if snapshot is None or version < history_request[1]:
            raise QueryInFlight
        raise_reader_error(snapshot)
        if not 0 <= clock() - snapshot.at <= history_max_age():
            history_request = (poller, poller.request_refresh())
            raise QueryInFlight
        after = (max(float(task.data.get('pending_since', 0)), pending_read_after or 0)
                 if task and task.data['status'] == 'pending' else 0)
        if (getattr(snapshot, 'started', 0) or snapshot.at) < after:
            history_request = (poller, poller.request_refresh())
            raise QueryInFlight
        if not accept_background_history(snapshot):
            history_request = None
            raise RemoteError('business_rows_invalid')
        history_request = None
        last_mine_started = getattr(snapshot, 'started', 0) or snapshot.at
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
        raise RemoteError(reason, snapshot.retry_after,
                          http_status=429 if snapshot.server_directed else None)

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
        checkpoint = task
        def defer(seconds: float, *, server_directed: bool = False) -> None:
            if server_directed:
                wait_gate.record(seconds)
            checkpoint.data['retry_at'] = clock() + seconds
            checkpoint.data['retry_server'] = server_directed
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

        remaining = task.data.get('retry_at', 0) - clock()
        if remaining > 0:
            # Older snapshots could hold a generic 20-300 s wait; only a server-directed
            # (Retry-After) wait may survive a restart at its full length.
            server_directed = task.data.get('retry_server') is True
            if not server_directed:
                remaining = min(remaining, OVERLOAD_RETRY_CAP)
            defer(remaining, server_directed=server_directed)
        authenticated = False
        failures = 0
        unconfirmed_checks = 0
        # The unconfirmed event is per pending episode. The marker lives in task.data: a failed
        # check resets pending_checks and a process restart reloads the state,
        # so a process-local flag would announce the same episode again in both cases.
        # It is cleared whenever a new pending episode starts and whenever one is resolved.
        last_timing_at = 0.0
        server_offset = 0.0
        offset_known = False
        # Conservative error of the school-clock estimate: the school timestamp is truncated to a
        # whole second and is computed after the response arrives, so the true opening can be up
        # to this much earlier than ``start - server_offset``. Preparation uses the earliest
        # plausible time, never a later one.
        school_clock_margin = 0.0
        history: list[dict[str, Any]] | None = None
        mine_at = 0.0
        mine_seconds = 0.0
        refresh_mine = False
        progress_probe_attempted = False

        # Preparation state is keyed by the school opening timestamp (stable across scans), not by
        # the locally adjusted time: the school-clock offset is re-estimated from a second-
        # truncated field and jitters, so a local key would repeat preparation for one opening.
        scheduled_openings: dict[float, float] = {}
        scheduled_margins: dict[float, float] = {}
        calibrated_openings: set[float] = set()
        preparation_skipped: set[float] = set()

        def next_opening() -> tuple[float, float] | None:
            """Earliest opening that has not started yet (school clock basis).

            Preparation must target the next not-yet-open lecture, not an already-open window
            that is still running: selecting a past start makes ``prepare_opening`` bail out and
            skips prewarm/prefetch for a later lecture in the same fast-polling period.
            """
            school_now = clock() + server_offset
            future = [window for window in known_openings if window[0] > school_now - 1.0]
            return min(future) if future else None

        def opening_local_estimate(start: float) -> float:
            """Earliest plausible local time of one opening, fixed on first sight.

            The school timestamp is second-truncated and read after the response arrives, so
            ``start - server_offset`` can be later than the true opening; subtracting a margin
            turns that systematic error into an earlier (safe) preparation deadline. The
            estimate may be recomputed only when the school clock first becomes known or when
            the measured margin grows, never later than a prior estimate.
            """
            margin = school_clock_margin if offset_known else 0.0
            previous = scheduled_openings.get(start)
            previous_margin = scheduled_margins.get(start, -1.0)
            if previous is None or (offset_known and (
                    start not in calibrated_openings or margin > previous_margin + 0.05)):
                previous = earliest_opening_local(start, server_offset, margin, previous)
                scheduled_openings[start] = previous
                scheduled_margins[start] = margin
            if offset_known:
                calibrated_openings.add(start)
            return previous

        def align_poller() -> None:
            """Ask the poller to place one poll right on the next opening instant.

            Off by default: after the school clock is calibrated the target is the earliest
            plausible opening (start - offset - margin), so it errs earlier; before calibration
            the raw school timestamp may still be off. Enable with SEU_POLLER_ALIGN only after
            real-run measurement.
            """
            if not settings.poller_align:
                return
            if offset_known and school_clock_margin > PREPARATION_MAX_UNCERTAINTY:
                return
            poller = activity_poller
            align = getattr(poller, 'align_at', None)
            if poller is None or align is None:
                return
            opening = next_opening()
            align(None if opening is None else opening_local_estimate(opening[0]))

        def background_client(reader: str) -> Client:
            clone = client.clone_readonly()
            clone.request_role = reader
            return clone

        def background_read(reader: str, clone: Client) -> list[dict[str, Any]]:
            clone.timeout = read_policy(known_openings, clock() + server_offset).timeout
            return clone.activities() if reader == 'activities' else clone.mine()

        def reader_retry(failures: int, snapshot: Any) -> float:
            if (opening_intervals(known_openings, clock() + server_offset) is not None
                    and snapshot is not None and snapshot.reason in FAST_RETRY_REASONS):
                return OPENING_POLLER_RETRY
            return short_backoff(failures)

        def history_interval() -> float | None:
            if not settings.history_background:
                return None
            return read_policy(known_openings, clock() + server_offset).history_interval

        def history_max_age() -> float:
            policy = read_policy(known_openings, clock() + server_offset)
            return (OPENING_HISTORY_TTL if not settings.history_background and opening_scan
                    else policy.history_max_age)

        def trigger_history_refresh() -> bool:
            """Ask the history thread to refresh now, throttled; True when a request was sent."""
            nonlocal history_refresh_at, history_refresh_poller
            poller = history_poller
            refresh = getattr(poller, 'request_refresh', None)
            if poller is None or not poller.running or not callable(refresh):
                return False
            if poller is not history_refresh_poller:
                # A recreated reader must not inherit the old reader's throttle window.
                history_refresh_poller = poller
                history_refresh_at = 0.0
            now = clock()
            if now - history_refresh_at < HISTORY_REFRESH_MIN_INTERVAL:
                return False
            history_refresh_at = now
            refresh()
            return True

        def accept_background_history(snapshot: Any) -> bool:
            """Consume one fresh, structurally valid history snapshot; True when accepted."""
            nonlocal history, mine_at, mine_seconds, refresh_mine, history_request
            if snapshot is None or snapshot.error is not None:
                return False
            if not (0 <= clock() - snapshot.at <= history_max_age()):
                return False
            rows = list(snapshot.rows)
            invalid = background_history_usable(rows, clock() + server_offset)
            if invalid is not None:
                diagnostic('history_background_invalid', reason=invalid)
                return False
            reader_recovered('history')
            history = rows
            history_request = None
            mine_at = snapshot.at
            remember_reservations(rows, mine_at)
            mine_seconds = getattr(snapshot, 'seconds', 0.0) or 0.0
            refresh_mine = True
            diagnostic('history_background_used', age_seconds=round(clock() - mine_at, 3))
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
                align_poller()
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
                                             name='activities-reader',
                                             on_align=lambda target, actual, delta: diagnostic(
                                                 'poller_aligned', target=round(target, 3),
                                                 actual=round(actual, 3),
                                                 delta_seconds=round(delta, 3)))
            activity_poller.start()
            update_reader_policy('activities', activity_poller)
            align_poller()

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
                update_reader_policy('history', history_poller)
                absorb_history_wait()
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
                                                interval=history_interval,
                                                clock=clock, gate=wait_gate,
                                                on_exit=functools.partial(close_session, session),
                                                on_update=functools.partial(work_updated.set, 'history'), retry_delay=reader_retry)
            history_poller.start()
            update_reader_policy('history', history_poller)

        def update_reader_policy(reader: str, poller: ActivityPoller) -> None:
            policy = read_policy(known_openings, clock() + server_offset)
            old = reader_policies.get(reader)
            reader_policies[reader] = policy
            reschedule = getattr(poller, 'reschedule', None)
            if (old is not None and old != policy and reschedule is not None
                    and (reader == 'activities' or settings.history_background)):
                reschedule()

        def query_interval() -> float:
            return scan_delay([], clock() + server_offset, 0, SCAN_INTERVAL, known_openings)[0]

        def list_snapshot_max_age() -> float:
            return read_policy(known_openings, clock() + server_offset).list_max_age

        def list_rows(*, after: float = 0.0) -> tuple[list[dict[str, Any]], float, str, float, float, float]:
            """Consume a fresh owner snapshot; direct reads are an offline test adapter.

            Returns ``(rows, query_seconds, source, snapshot_age, fetch_seconds, data_age)``:
            snapshot age counts from the poller response, fetch seconds is the query duration and
            data age counts from the request start, which is what a candidate's staleness means.
            """
            nonlocal consumed_polls, last_rows_started, rows_snapshot_at
            poller = activity_poller

            def current_pair() -> tuple[Any, Any]:
                versioned = getattr(poller, 'snapshot_with_version', None)
                if poller is not None and versioned is not None:
                    # Snapshot and version must come from the same lock: otherwise a poll landing
                    # between the two reads is marked consumed although its rows were never seen.
                    return versioned()
                return (poller.snapshot() if poller is not None else None,
                        getattr(poller, 'polls', 0))

            snapshot, version = current_pair()
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
                raise_reader_error(snapshot)
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
            poller = activity_poller
            if poller is None:
                return None
            versioned = getattr(poller, 'snapshot_with_version', None)
            if versioned is not None:
                snapshot, version = versioned()
            else:
                snapshot, version = poller.snapshot(), getattr(poller, 'polls', 0)
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

        # Opening preparation: one warmed history refresh and one prefetched captcha per opening.
        prewarmed: set[float] = set()
        prefetched_code: str | None = None
        prefetched_at = 0.0
        prefetched_seconds = 0.0
        prefetched_ocr_seconds = 0.0
        prefetched_for = 0.0

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
                client.timeout = min(float(saved_timeout), available)
                try:
                    started = clock()
                    binary = client.captcha()
                    fetched_at = clock()
                    seconds = max(0.0, fetched_at - started)
                    ocr_started = clock()
                    code = recognizer(binary).strip()
                    return PreparedCaptcha(code, fetched_at, seconds,
                        max(0.0, clock() - ocr_started), client.last_connect_seconds,
                        timer() > expires)
                finally:
                    client.timeout = saved_timeout
            return fetch

        def dispatch_captcha(fetch: Callable[[], PreparedCaptcha], *, source: str) -> None:
            nonlocal captcha_worker, preparation, preparation_source, prefetched_code
            poller_wait_error()
            if captcha_worker is None:
                captcha_worker = CaptchaWorker(functools.partial(work_updated.set, 'captcha'))
            captcha_worker.submit(fetch, generation=authentication_generation)
            prefetched_code = None
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
            if result.expired or not 0 <= clock() - result.at <= CAPTCHA_PREFETCH_MAX_AGE:
                raise RemoteError('network_timeout')
            return result

        def consume_captcha_updates() -> None:
            poller_wait_error()
            for reader in (activity_poller, history_poller):
                reader_snapshot = reader.snapshot() if reader is not None else None
                raise_reader_error(reader_snapshot)
            history_snapshot = history_poller.snapshot() if history_poller is not None else None
            if history_snapshot is not None and history_snapshot.at > mine_at:
                if not accept_background_history(history_snapshot):
                    raise ValueError('invalid_activity_data')

        def accept_preparation(result: PreparedCaptcha, opening_at: float) -> None:
            nonlocal prefetched_code, prefetched_at, prefetched_seconds, prefetched_ocr_seconds
            if result.expired:
                diagnostic('captcha_prefetch_failed', reason='deadline_expired')
            elif not result.code or not result.code.isascii() or not result.code.isalnum() or len(result.code) > 16:
                diagnostic('captcha_prefetch_invalid')
            elif not 0 <= clock() - result.at <= CAPTCHA_PREFETCH_MAX_AGE:
                diagnostic('captcha_prefetch_stale', age_seconds=round(clock() - result.at, 3))
            else:
                prefetched_code, prefetched_at = result.code, result.at
                prefetched_seconds, prefetched_ocr_seconds = result.seconds, result.ocr_seconds
                diagnostic('captcha_prefetched', seconds=round(result.seconds, 3),
                    ocr_seconds=round(result.ocr_seconds, 3), lead_seconds=round(opening_at-result.at, 3),
                    connect_seconds=round(result.connect_seconds, 3) if result.connect_seconds is not None else None)

        def collect_preparation(*, discard: bool = False) -> None:
            nonlocal preparation
            if preparation is None or not preparation.ready:
                return
            completed, preparation = preparation, None
            discard = (discard or completed.generation != authentication_generation
                       or preparation_source != 'prefetch')
            failed_event = ('captcha_prefetch_failed' if preparation_source == 'prefetch'
                            else 'captcha_job_discarded')
            try:
                result = completed.result()
                if not discard:
                    accept_preparation(result, opening_local_estimate(prefetched_for))
                else:
                    diagnostic('captcha_job_discarded', source=preparation_source,
                               job=completed.number, generation=completed.generation)
            except (RemoteError, ValueError) as exc:
                if not discard and (isinstance(exc, AuthenticationExpired) or server_wait_error(exc)):
                    raise
                diagnostic(failed_event, reason=type(exc).__name__)
            except Exception as exc:
                diagnostic(failed_event, reason=type(exc).__name__)

        def prepare_opening(opening: tuple[float, float] | None) -> float | None:
            """Do read-only work that must not sit on the candidate-to-submit path.

            Returns the local-clock time of the next pending piece of work so the scan loop can
            wake exactly then; None when nothing is pending. History only enqueues a refresh;
            captcha I/O lends the main session to one bounded preparation task.
            """
            nonlocal prefetched_code, prefetched_at, prefetched_seconds, prefetched_ocr_seconds
            nonlocal prefetched_for, preparation
            if opening is None:
                return None
            start = opening[0]
            if offset_known and school_clock_margin > PREPARATION_MAX_UNCERTAINTY:
                if start not in preparation_skipped:
                    preparation_skipped.add(start)
                    diagnostic('preparation_skipped', reason='clock_uncertainty',
                               uncertainty=round(school_clock_margin, 3))
                return None
            open_local = opening_local_estimate(start)
            if open_local < clock() - 1.0:
                return None
            deadline: float | None = None
            if settings.prewarm and start not in prewarmed:
                now = clock()
                remaining = open_local - now
                if now < open_local - PREWARM_LEAD_SECONDS:
                    if remaining > PREWARM_MIN_REMAINING:
                        deadline = open_local - PREWARM_LEAD_SECONDS
                elif remaining > PREWARM_MIN_REMAINING:
                    if trigger_history_refresh():
                        prewarmed.add(start)
                        diagnostic('prewarm_history_requested')
            if (settings.captcha_prefetch and recognize is not None
                    and prefetched_for != start and preparation is None):
                # Recompute before captcha I/O; the history refresh above only enqueues work.
                now = clock()
                remaining = open_local - now
                target = open_local - CAPTCHA_PREFETCH_LEAD_SECONDS
                if now < target:
                    if remaining > CAPTCHA_PREFETCH_MIN_REMAINING:
                        deadline = target if deadline is None else min(deadline, target)
                elif remaining > CAPTCHA_PREFETCH_MIN_REMAINING:
                    # Only prefetch before the opening instant; once it has started a candidate
                    # may already be waiting, so fetching off the path would only add requests.
                    prefetched_for = start
                    poller_wait_error()
                    # The whole prefetch must finish by the opening instant; otherwise the submit
                    # path fetches fresh and this attempt has only cost a bounded delay.
                    budget = max(0.15, min(CAPTCHA_PREFETCH_TIMEOUT, remaining - 0.03))
                    fetch = captcha_fetch(budget)
                    if background_poller:
                        dispatch_captcha(fetch, source='prefetch')
                        diagnostic('captcha_prefetch_started')
                    else:
                        # The direct adapter keeps deterministic clocks and fake sessions single-threaded.
                        try:
                            accept_preparation(fetch(), open_local)
                        except (RemoteError, ValueError) as exc:
                            if isinstance(exc, AuthenticationExpired) or server_wait_error(exc):
                                raise
                            diagnostic('captcha_prefetch_failed', reason=type(exc).__name__)
                        except Exception as exc:
                            diagnostic('captcha_prefetch_failed', reason=type(exc).__name__)
            return deadline

        previous_selection = task.data.get('selection')
        details: list[dict[str, Any]] = (previous_selection.get('activities', [])
                                         if isinstance(previous_selection, dict) else [])
        known_targets = merge_openings({}, details, clock() + server_offset)
        known_openings = sorted(set(opening_windows(list(known_targets.values()))))[:128]
        last_opening_mode = False
        while True:
            try:
                work_updated.clear()
                check_stopped()
                flush_mail()
                collect_preparation(discard=not authenticated)
                scan_started = clock()
                school_now = clock() + server_offset
                known_openings = [window for window in known_openings if window[1] >= school_now - 1]
                opening_scan = opening_intervals(known_openings, school_now) is not None
                if opening_scan and not last_opening_mode:
                    trigger_history_refresh()
                last_opening_mode = opening_scan
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
                    sync_history_poller()
                if (opening_scan and settings.history_background and background_poller
                        and history_poller is not None and history_poller.running):
                    # Ask for a replacement before candidates need the expired history.
                    background_snapshot = history_poller.snapshot()
                    background_fresh = (background_snapshot is not None
                                        and background_snapshot.error is None
                                        and 0 <= clock() - background_snapshot.at
                                        <= HISTORY_BACKGROUND_FRESH)
                    cache_fresh = (history is not None
                                   and 0 <= clock() - mine_at < HISTORY_BACKGROUND_FRESH)
                    if not background_fresh and not cache_fresh:
                        trigger_history_refresh()
                # Holds store local-clock deadlines. Once the school clock is known, tighten any
                # hold created before calibration to the activity start converted into the host
                # clock. This also runs while a submission is pending: a hold can outlive the
                # pending check, and waiting for it to resolve would keep the full 600 s fallback.
                holds_now = hold_entries()
                adjusted = tighten_hold_deadlines(holds_now, offset_known=offset_known,
                                                  server_offset=server_offset)
                if task.data['status'] != 'pending':
                    now = clock()
                    expired = [wid for wid, entry in holds_now.items()
                               if isinstance(entry, dict) and now >= float(entry.get('until', 0))]
                    for wid in expired:
                        holds_now.pop(wid, None)
                        diagnostic('reservation_hold_expired', wid=wid)
                        record('reservation_hold_expired')
                    adjusted = adjusted or bool(expired)
                if adjusted:
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
                            and poller_snapshot.rows):
                        # Contradictory duplicate rows cannot confirm the submission, nor can
                        # they prove absence: pause instead of trusting either row.
                        if conflicting_records(poller_snapshot.rows):
                            abandon_check(RemoteError('activity_schema_unknown'))
                        for row in poller_snapshot.rows:
                            try:
                                row_wid = activity_id(row)
                            except ValueError:
                                continue
                            if row_wid != pending_wid:
                                continue
                            try:
                                judged = Lecture.read(row, now=clock() + server_offset)
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
                                and 0 <= clock() - positive.at <= history_max_age()
                                and background_history_usable(positive.rows, clock() + server_offset) is None
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

                    # Probe the list before requesting history: an unavailable list cannot prove
                    # absence, so repeated history queries would add no evidence. A failed list is
                    # different: history may still carry positive evidence, so fall through and let
                    # the normal error handling abort the check.
                    probed_rows: list[dict[str, Any]] | None = None
                    probed_error: BaseException | None = None
                    probed_source = 'none'
                    probe_none = False
                    if not confirmed and background_poller:
                        try:
                            probed_rows, _, activities_source, _, _, _ = list_rows(after=pending_read_after)
                            probed_source = activities_source
                        except (RemoteError, ValueError) as exc:
                            probed_rows = None
                            probed_error = exc
                            if server_wait_error(exc):
                                # 429/Retry-After applies to every request: honour it before the
                                # history query instead of sending one inside the requested wait.
                                abandon_check(exc)
                        else:
                            probe_none = activities_source == 'none'
                    if probe_none:
                        # Waiting adds no absence evidence and preserves earlier valid checks.
                        delay = reader_wait_seconds()
                        health(phase='waiting', next_check_at=clock() + delay)
                        sync_poller()
                        pause_for_update(delay)
                        continue

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
                        if probed_rows is not None or probed_error is not None:
                            # Reuse the probe: neither reading the list twice nor falling back to
                            # the legacy 'none' branch is needed here.
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
                                    judged = Lecture.read(row, now=clock() + server_offset)
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
                # Query the opening-critical list before the slower history refresh: a slow or
                # failing my-activities endpoint must not hide the next reservation opening.
                health(phase='activities')
                rows, activities_seconds, activities_source, rows_age, rows_fetch_seconds, \
                    rows_data_age = list_rows()
                if activities_source == 'none':
                    # The owner publishes a wakeup; wait for that result or a real deadline.
                    delay = reader_wait_seconds()
                    health(phase='waiting', next_check_at=clock() + delay)
                    sync_poller()
                    sync_history_poller()
                    pause_for_update(delay)
                    continue
                rows_fetched_at = clock()
                # Start the list reader before the history step: if resolving history takes
                # seconds, the scan can then consume a newer list snapshot instead of submitting
                # a candidate from the pre-wait rows.
                if opening_scan:
                    sync_poller()
                if school_clock_known(rows, clock()):
                    server_offset = school_clock_offset(rows, clock())
                    offset_known = True
                    # The truncation fraction plus the query round trip can make the estimate
                    # late by up to ~1s + request time; treat that much as uncertainty.
                    school_clock_margin = school_clock_uncertainty(rows_age, rows_fetch_seconds)
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
                                judged = Lecture.read(row, now=clock() + server_offset)
                            except ValueError:
                                break
                            if (judged.availability == 'already_reserved'
                                    or held_entry.get('confirmed') and not held_entry.get('category')):
                                ensure_hold(held_wid, judged.start.timestamp(),
                                            judged.end.timestamp(), confirmed=True,
                                            source='activity_list', category=judged.category)
                            break
                background_history = (history_poller.snapshot()
                                      if history_poller is not None else None)
                history_ttl = (HISTORY_BACKGROUND_FRESH
                               if settings.history_background and background_poller
                               else OPENING_HISTORY_TTL)
                if not opening_scan:
                    history_ttl = NORMAL_HISTORY_TTL
                history_still_fresh = (
                    history is not None
                    and 0 <= clock() - mine_at < history_ttl)
                candidate_refresh = False
                if history is not None and history_still_fresh and not opening_scan and not refresh_mine:
                    cached_progress = progress(progress_rows(history, hold_entries(), clock() + server_offset),
                                               clock() + server_offset)
                    possible, _ = select_candidates(rows, settings, cached_progress, history,
                        task.data['cooldowns'], clock() + server_offset,
                        cooldown_clock_known=offset_known, holds=hold_views())
                    if possible:
                        history_still_fresh = False
                        candidate_refresh = True
                used_background_history = False
                if (background_poller and not candidate_refresh and not refresh_mine and background_history is not None
                        and (background_history.at > mine_at or not history_still_fresh
                             or background_history.error is not None)):
                    # A newer background result must replace the cached history immediately.
                    used_background_history = accept_background_history(background_history)
                    if not used_background_history:
                        history_still_fresh = False
                if history_still_fresh or used_background_history:
                    reader_recovered('history')
                if (background_poller and not refresh_mine and not history_still_fresh):
                    reader_missing('history')
                if not used_background_history and not refresh_mine and not history_still_fresh:
                    poller_wait_error()
                    health(phase='my_activities', next_check_at=0,
                           activity_count=None, candidate_count=None)
                    mine_started = clock()
                    history = query_mine()
                    if not background_poller:
                        mine_at = clock()
                        mine_seconds = max(0, clock() - mine_started)
                    refresh_mine = True
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
                current = progress(progress_rows(history, hold_entries(), clock() + server_offset),
                                   clock() + server_offset)
                if current.unresolved != unresolved_reported:
                    unresolved_reported = current.unresolved
                    diagnostic('attendance_unresolved', count=len(current.unresolved))
                latest_progress, progress_at = current, mine_at
                snapshot['progress'] = dict(completed=dict(current.completed), planned=dict(current.planned),
                                            missing=sorted(current.missing), unresolved=len(current.unresolved),
                                            at=mine_at)
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
                    known_targets.clear()
                    known_openings.clear()
                    opening_scan = False
                    sync_poller()
                    sync_history_poller()
                    flush_mail()
                    failures = 0
                    delay = random.uniform(SCAN_INTERVAL - 1, SCAN_INTERVAL + 1)
                    health(phase='waiting_for_attendance', last_query_at=clock(), failure_count=0,
                           next_check_at=clock() + delay)
                    query_recovered()
                    clear_alert('task_failed')
                    pause_for_update(delay)
                    continue
                if clock() - rows_fetched_at > HISTORY_LIST_REFRESH_AFTER:
                    # Resolving history may have taken seconds; do not submit a candidate that the
                    # newer background list snapshot has already shown as full or superseded.
                    fresher = newer_poller_rows()
                    if fresher is not None:
                        (rows, activities_source, rows_age, rows_fetch_seconds, rows_data_age,
                         consumed_polls, rows_snapshot_at, last_rows_started) = fresher
                        activities_seconds = 0.0
                        if school_clock_known(rows, clock()):
                            server_offset = school_clock_offset(rows, clock())
                            offset_known = True
                            school_clock_margin = school_clock_uncertainty(
                                rows_age, rows_fetch_seconds)
                candidates, details = select_candidates(rows, settings, current, history,
                    task.data['cooldowns'], clock() + server_offset,
                    cooldown_clock_known=offset_known, holds=hold_views())
                query_recovered()
                if not candidates:
                    # No submission step can be attempted in this scan; do not keep an old
                    # captcha/submit failure timer alive while only list queries run.
                    step_recovered()
                known_targets = merge_openings(known_targets, details, clock() + server_offset)
                known_openings = sorted(set(opening_windows(list(known_targets.values()))))[:128]
                opening_scan = opening_intervals(known_openings, clock() + server_offset) is not None
                if candidates and preparation is not None:
                    raise QueryInFlight
                if preparation is None:
                    client.timeout = SUBMIT_TIMEOUT if opening_scan else 10
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
                    wake_at = prepare_opening(next_opening())
                next_preparation_at = wake_at
                prefetch_ready = False
                prefetch_age: float | None = None
                if prefetched_code is not None:
                    age = clock() - prefetched_at
                    if 0.0 <= age <= CAPTCHA_PREFETCH_MAX_AGE:
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
                            prefetched_code = None
                            prefetch_ready = False
                            attempt_prefetch_age = prefetch_age
                            captcha_seconds = 0.0
                            ocr_seconds = 0.0
                            captcha_source = 'prefetch'
                        else:
                            try:
                                result = fresh_captcha(float(client.timeout))
                            except RemoteError as exc:
                                step_failed(clock())
                                diagnostic('captcha_error', wid=lecture.wid, reason=str(exc),
                                           attempt=attempt + 1)
                                raise
                            code = result.code
                            captcha_seconds, ocr_seconds = result.seconds, result.ocr_seconds
                            captcha_connect = result.connect_seconds
                        if not code or not code.isascii() or not code.isalnum() or len(code) > 16:
                            diagnostic('ocr_invalid', wid=lecture.wid, attempt=attempt + 1)
                            if attempt == 3:
                                task.data['cooldowns'][lecture.wid] = clock() + server_offset + 300
                                task.save()
                                diagnostic('captcha_cooldown', wid=lecture.wid, seconds=300)
                            continue
                        # Recheck already available snapshots, without another school query.
                        if background_poller:
                            consume_captcha_updates()
                            (rows, activities_seconds, activities_source, rows_age,
                             rows_fetch_seconds, rows_data_age) = list_rows()
                            if activities_source == 'none' or not 0 <= clock() - mine_at <= history_max_age():
                                diagnostic('captcha_candidate_changed', wid=lecture.wid,
                                           reason='snapshot_unavailable')
                                restart_scan = True
                                break
                            current = progress(progress_rows(history, hold_entries(), clock() + server_offset),
                                               clock() + server_offset)
                            refreshed, _ = select_candidates(rows, settings, current, history,
                                task.data['cooldowns'], clock() + server_offset,
                                cooldown_clock_known=offset_known, holds=hold_views())
                            updated = next((item for item in refreshed if item.wid == lecture.wid), None)
                            if updated is None:
                                diagnostic('captcha_candidate_changed', wid=lecture.wid,
                                           reason='candidate_unavailable')
                                restart_scan = True
                                break
                            lecture = updated
                        # A wait learned during the round-trip must stop the submission.
                        poller_wait_error()
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
                            wait = exc.retry_after if exc.retry_after > 0 else (
                                FAST_RETRY_INTERVAL if opening_scan else short_backoff(1))
                            if not authenticated:
                                stop_poller()
                                stop_history_poller()
                            defer(wait, server_directed=exc.retry_after > 0)
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
                        if captcha_wrong and attempt == 3:
                            task.data['cooldowns'][lecture.wid] = clock() + server_offset + 300
                            diagnostic('captcha_cooldown', wid=lecture.wid, seconds=300)
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
                    delay, fast = scan_delay(details, clock() + server_offset, elapsed, SCAN_INTERVAL,
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
                        # Wake exactly when the prewarm/history refresh or the captcha prefetch
                        # is due instead of drifting with the fast interval.
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
                pending_check_due = 0.0
                pending_read_after = None
                history = None
                authenticated = False
                unconfirmed_checks = 0
                reset_pending_checks()
                failures += 1
                query_failed(scan_started)
                health(last_error='authentication_expired', last_error_at=clock(), failure_count=failures)
                stop_poller()
                stop_history_poller()
                defer(short_backoff(failures))
            except (RemoteError, ValueError) as exc:
                pending_read_after = None
                if isinstance(exc, ValueError):
                    # A structurally invalid history is not a cache we can keep using.
                    history = None
                unconfirmed_checks = 0
                reset_pending_checks()
                reason = str(exc)
                if isinstance(exc, AttendanceCategoryUnknown):
                    # Local-only detail: the history row had no usable category. The public error
                    # code stays unchanged; the activity ID helps identify the lagging record.
                    diagnostic('attendance_category_unknown', wid=exc.wid or None)
                safe_reason = reason if isinstance(exc, RemoteError) or reason in (
                    'attendance_category_unknown', 'attendance_record_conflict') else 'invalid_activity_data'
                record(safe_reason)
                query_failed(scan_started)
                # Never issue another request before a rate-limit wait: the probe exists only to
                # fill progress for a later alert, not to bypass the server's Retry-After.
                server_directed_wait = (isinstance(exc, RemoteError)
                                        and (exc.retry_after > 0 or str(exc) == 'server_backoff'))
                if (authenticated and not progress_probe_attempted and latest_progress is None
                        and not server_directed_wait and not shared_wait_pending()):
                    # One best-effort history read so a sustained query-failure alert can still
                    # report confirmed progress; never submit from this probe.
                    progress_probe_attempted = True
                    try:
                        history = query_mine()
                        if not background_poller:
                            mine_at = clock()
                        latest_progress = progress(history, clock() + server_offset)
                        progress_at = mine_at
                    except (RemoteError, ValueError, QueryInFlight):
                        history = None
                opening_scan = opening_intervals(known_openings, clock() + server_offset) is not None
                quick = (opening_scan and authenticated and isinstance(exc, RemoteError)
                         and reason in FAST_RETRY_REASONS and not exc.retry_after)
                retry = exc.retry_after if isinstance(exc, RemoteError) else 0
                if reason == 'service_unavailable':
                    # Temporary overload/gateway failure: recover within seconds. An explicit
                    # Retry-After still takes precedence.
                    failures += 1
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    delay = retry if retry > 0 else short_backoff(failures)
                elif quick:
                    # Opening seconds are scarce: retry transient request failures at the fast cadence.
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    delay = FAST_RETRY_INTERVAL
                else:
                    failures += 1
                    health(last_error=safe_reason, last_error_at=clock(), failure_count=failures)
                    delay = retry if retry > 0 else short_backoff(failures)
                defer(delay, server_directed=retry > 0)
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
