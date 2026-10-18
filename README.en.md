# SEU Lecture Reservation · HTTPS Edition

[中文](README.md)

A lecture reservation tool for Linux servers, with Windows compatibility. No browser is needed: configure your credentials and filters, then let it look for lectures, recognize CAPTCHAs and attempt reservations.

The default goal is 8 school-recorded attendances, including at least one each in law, art and mental health. Booking one lecture does not stop the client. It pauses new bookings when existing reservations cover the remaining goal, and finishes when recorded attendance meets it. Salons are neither booked nor counted.

When existing records overlap, the client automatically selects mutually nonconflicting lectures for local counts. It does not cancel school reservations; the university determines which attendances are valid. See the [selection rules](docs/usage.md#运行和恢复) (Chinese).

## Screenshot

Background startup and local task status on a Linux server:

![Linux terminal starting the client in the background with nohup and querying the local task status, status=scanning](docs/images/使用截图.png)

`status=scanning` is a local task state; it does not confirm process health or a successful reservation. See the [usage guide](docs/usage.md#服务器后台运行) (Chinese) for process and log checks. Actual reservations and long-running operation still need acceptance testing.

Joint project poster: [high-resolution PNG](docs/promo/seu-lecture-poster.png) · [Python generation guide](docs/promo/usage.md) (Chinese), featuring the Windows browser edition and Linux HTTPS edition.

## Quick start

Ubuntu 24.04 LTS example: use an existing sudo or root account over SSH, and use the same account for installation and running. Omit `sudo` when already root.

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
uv python install 3.12
```

See the official [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) and [Python guide](https://docs.astral.sh/uv/guides/install-python/). On Windows, install uv using the official Windows instructions, then use Git Bash for the project commands below and run in the foreground. Skip Ubuntu system installation and background-management commands.

**1. Download and install dependencies**

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

**2. Fill in the configuration**

Copy the example on first use. Keep your existing `.env` if you already have one.

```bash
cp -n .env.example .env
chmod 600 .env
```

Open `.env` in a text editor and enter your credentials and allowed campus names:

```dotenv
SEU_CARD_NUMBER="your-card-number"
SEU_PASSWORD='your-password'
SEU_PREFERRED_CAMPUS='["无锡校区"]'
```

Campus names must match the university system; change the example to suit your needs. The default campus list is empty, allowing only online lectures. Other settings can keep their example values. The client automatically reads `.env` in the current directory; no `source` command is needed. Process environment variables take precedence.

Other common settings follow; restart after editing `.env`. Lists use JSON arrays, such as `'["无锡校区"]'`.

| Setting | Purpose |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | Allowed offline campuses; `'[]'` excludes offline lectures |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | Allow online / offline lectures; use `true` or `false` |
| `SEU_PREFERRED_LECTURES` | Match full titles; `'[]'` leaves titles unrestricted |
| `SEU_REQUIRED_CATEGORIES` | Candidate categories and their order; usually keep the four example categories |
| `SEU_VPN_IP` | Backup gateway IP for initial login; normal DNS is always tried first. Empty disables fallback |

Exact titles still obey other filters and time-conflict checks. The example backup IP is `121.249.14.2`. Fallback happens once after gateway DNS failure, connection closure/reset or unexpected TLS disconnection; ordinary timeouts and certificate errors do not trigger it. Once switched, the IP is used until the process exits. The next launch tries DNS again. See [gateway DNS fallback](docs/usage.md#仅在项目内绕过网关-dns-故障) (Chinese) for full limits and IP updates.

Email is optional; reservations also work without it. Each user supplies their own sender account. Messages go to the same address by default; set `MAIL_TO` for a different recipient. For Gmail:

1. Sign into your Google account and enable [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification).
2. Open [App passwords](https://myaccount.google.com/apppasswords) and create a password for this tool.
3. Copy the generated 16-character password, remove grouping spaces and put it in `.env`. Do not use your regular Google login password:

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

The client defaults to `smtp.gmail.com:465` and sending to yourself. Nonempty `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM` and `MAIL_TO` overrides take precedence; clear old overrides to use automatic defaults. See the [usage guide](docs/usage.md#gmail-示例) (Chinese) and [Google documentation](https://support.google.com/accounts/answer/185833) for unavailable app passwords, revocation and other providers.

With mail enabled, important events have Chinese explanations. A daily report summarizes the latest progress and pending issues at **20:00 Asia/Shanghai**. Starting after 20:00 attempts that day's report at the first safe wait. Each state directory attempts it at most once per day, without resending failures. Reports stop when the process exits or the goal is complete.

**3. Check login and optional email**

```bash
uv run main.py --check
```

`event=read_only_complete` means login, current query data and the CAPTCHA image passed validation; it does not verify an actual reservation. Lecture fields are checked when the list is nonempty; an empty list cannot validate them. This does not submit reservations or send mail. Follow the terminal prompts if SMS verification is needed. On failure, check the `event=task_failed` line against the [troubleshooting guide](docs/usage.md#先做只读检查) (Chinese).

If mail is configured, send one test message without university login or starting a reservation task:

```bash
uv run main.py --test-mail
```

`event=test_mail_accepted` only means SMTP accepted it; check your inbox separately. Failures show a fixed error category without passwords or raw server responses. See [mail troubleshooting](docs/usage.md#邮件通知与每日汇报) (Chinese).

**4. Start reservations**

```bash
uv run main.py
```

Every launch first reads the latest university progress and exits if the goal is already complete. Otherwise, this command submits real reservations. The client quietly waits when no suitable lecture is available, checking every 10 seconds by default. Keep your SSH terminal open for foreground operation. Press `Ctrl+C` to stop; running the same command resumes the task without cancelling existing reservations.

## Run in the background on a server

First run `uv run main.py --status` and confirm `process_running=false`. Ensure the same account has no other instance on another machine or in the browser edition, then run these commands from the project directory:

```bash
mkdir -p -m 700 state
nohup uv run main.py >> state/runner.log 2>&1 < /dev/null &
echo $! > state/runner.pid
tail -n 20 state/runner.log
```

Confirm `process_running=true` and an updated `last_query_at` before disconnecting SSH. After reconnecting, return to the project directory to check status and logs. The process can survive an SSH disconnect, but it will not restart automatically after exiting or a server reboot. If manual authentication is needed, it exits and preserves the task. Confirm the background process has exited, then run `uv run main.py` in the foreground to handle it. Adjust the log paths if you changed `SEU_STATE_DIR`. See [background operation](docs/usage.md#服务器后台运行) (Chinese) for process checks and recovery.

## Check task status

Read the local task record from the project directory without starting another reservation process:

```bash
uv run main.py --status
```

| Status | Meaning |
| --- | --- |
| `not_started` | No local task yet |
| `scanning` | The task is incomplete and still needs scanning or progress checks |
| `pending` | A reservation submission result is unconfirmed |
| `done` | The attendance goal is complete |

The first line remains `status=...`, followed by runtime information:

| Field | Meaning |
| --- | --- |
| `process_running` | Whether a process holds this task's lock; `false` means no reservation instance is running |
| `phase` | `authentication`: logging in; `waiting`: waiting after a scan; `waiting_for_attendance`: waiting for attendance; `backoff`: retry delay; `manual_required`: user action needed |
| `last_query_at` / `last_query_age_seconds` | Most recent successful query and its age; `never` / `unknown` means none has been recorded |
| `updated_at` / `snapshot_age_seconds` | Last runtime update and its age; investigate old snapshots together with the phase and `next_check_at` |
| `failure_count` / `last_error` / `last_error_at` | Consecutive failures, latest error and its time; recovery resets the count but retains the error for diagnosis |
| `activity_count` / `candidate_count` | Last activity-list size and matching candidate count; `unknown` means the list was not queried in that cycle |
| `mail_status` | `disabled`, `ready`, `sending`, `accepted` by SMTP, or `failed`; this does not prove inbox delivery |

`activity_details` shows each lecture's title, first selection reason and booking window from the latest scan. Check `selection_updated_at` for freshness. For example, `reservation_not_started` means booking has not opened, `campus_not_selected` excludes an offline campus, and `time_conflict` indicates an existing reservation overlaps. See [runtime diagnostics](docs/usage.md#运行诊断) (Chinese). These private details are excluded from event logs and emails; do not publish state files or terminal output containing them.

`last_mail_status`, `last_mail_error`, `last_mail_event` and `last_mail_duration_seconds` retain the latest task notification's outcome, error category, event and measured duration across restarts. Unfinished attempts and old records show unknown duration.

Check `process_running=true`, a recent successful query and the current phase together. Zero candidates with an advancing success timestamp means no candidate was selectable in that cycle: booking may not have opened, or filters may exclude the lectures. It does not verify reservation submission. Normal polling stays quiet. `phase=unknown` means an old task has no new runtime information, or its information is invalid; it is not evidence of health.

**`scanning` alone does not prove the process is running.** For the background startup method above, you can also check the process recorded in the PID file:

```bash
ps -p "$(cat state/runner.pid)" -o pid,ppid,args
```

No matching process row means that PID has exited. A running process does not prove its latest query succeeded. Read recent output and events:

```bash
tail -n 20 state/runner.log
tail -n 20 state/events.log
```

Logs may contain old errors; check their timestamps. Missing log files are possible before anything has been logged. Adjust these paths and those below if you changed `SEU_STATE_DIR`.

## Stop and resume

For foreground operation, press `Ctrl+C` in the original terminal. For background operation, read the PID and check its command and working directory first:

```bash
reserve_pid=$(cat state/runner.pid)
ps -p "$reserve_pid" -o pid,ppid,args
readlink "/proc/$reserve_pid/cwd"
```

Confirm the command is `uv run main.py` and the working directory is this project, then run:

```bash
kill -TERM "$reserve_pid"
```

`uv` forwards the termination signal to its Python child. Run the `ps` command above again to check that it exited. `No such process` means that PID has already exited. PID files can be stale, so check before sending a signal.

Stopping does not cancel school reservations. Keep the `state` directory, then run `uv run main.py` or the background startup commands above to resume the same task. Every normal launch logs in and checks university progress, even when locally marked complete. If still complete, it exits without another completion email; otherwise it resumes filling the goal. `--new-task` remains a compatibility entry for rechecking completed tasks and cannot reset scanning or pending tasks.

## Before relying on it

- A nonempty live lecture list has been checked. Selection uses the reservation window and publication status; online/offline classification accepts only an explicit leading `【线上】` / `【线下】` title marker. Unknown schemas raise errors. University preflight checks remain authoritative for capacity and eligibility; keep the server clock accurate. See [API boundaries](docs/usage.md#当前业务数据契约与限制) (Chinese).
- Earlier versions have live login, read-only query and test-mail delivery records. This update uses offline regression checks; actual reservations, SMS recovery and long-running operation after the update still need acceptance testing. Confirm bookings and attendance in the [SEU portal](https://ehall.seu.edu.cn/); these counts do not replace final departmental assessment.
- `.env` contains credentials; do not upload it. When a submission result is unclear, the client checks that activity before proceeding instead of blindly resubmitting.
- Development checks, detailed settings and acceptance records are in the [usage guide](docs/usage.md) (Chinese).

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Project](https://github.com/MuZiJin701/seu-lecture-reserve-http) · [Browser version](https://github.com/MuZiJin701/SEU_Lecture_Reserve)
