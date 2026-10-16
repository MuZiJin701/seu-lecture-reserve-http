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

Prepare Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

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

**3. Check login and queries**

```bash
uv run main.py --check
```

`event=read_only_complete` means the check passed. This does not submit reservations or send mail. Follow the terminal prompts if SMS verification is needed. On failure, check the `event=task_failed` line against the [troubleshooting guide](docs/usage.md#先做只读检查) (Chinese).

**4. Start reservations**

```bash
uv run main.py
```

This command submits real reservations. The client quietly waits when no suitable lecture is available, checking every 10 seconds by default. Keep your SSH terminal open for foreground operation. Press `Ctrl+C` to stop; running the same command resumes the task without cancelling existing reservations.

## Run in the background on a server

Confirm no other instance is running, then run these commands from the project directory:

```bash
mkdir -p -m 700 state
nohup uv run main.py >> state/runner.log 2>&1 < /dev/null &
echo $! > state/runner.pid
tail -n 20 state/runner.log
```

The process can survive an SSH disconnect, but it will not restart automatically after exiting or a server reboot. If manual authentication is needed, it exits and preserves the task. Confirm the background process has exited, then run `uv run main.py` in the foreground to handle it. Adjust the log paths if you changed `SEU_STATE_DIR`. See [background operation](docs/usage.md#服务器后台运行) (Chinese) for process checks and recovery.

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

Check `process_running=true`, a recent successful query and the current phase together. Zero candidates with an advancing success timestamp means normal waiting. Normal polling stays quiet. `phase=unknown` means an old task has no new runtime information, or its information is invalid; it is not evidence of health.

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

Stopping does not cancel school reservations. Keep the `state` directory, then run `uv run main.py` or the background startup commands above to resume the same task. Use `uv run main.py --new-task` to recheck a completed goal, including tasks completed before this update that need the new overlap-counting rules.

## Common settings

Restart after editing `.env`. Lists use JSON arrays, such as `'["无锡校区"]'`.

| Setting | Purpose |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | Allowed offline campuses; `'[]'` excludes offline lectures |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | Allow online / offline lectures; use `true` or `false` |
| `SEU_PREFERRED_LECTURES` | Match full titles; `'[]'` leaves titles unrestricted |
| `SEU_REQUIRED_CATEGORIES` | Candidate categories and their order; usually keep the four example categories |
| `SEU_VPN_IP` | Backup gateway IP for initial login; normal DNS is always tried first. Empty disables fallback |

Exact titles still obey other filters and time-conflict checks. The example backup IP is `121.249.14.2`. Fallback happens once after gateway DNS failure, connection closure/reset or unexpected TLS disconnection; ordinary timeouts and certificate errors do not trigger it. Once switched, the IP is used until the process exits. The next launch tries DNS again. See [gateway DNS fallback](docs/usage.md#仅在项目内绕过网关-dns-故障) (Chinese) for full limits and IP updates.

Email notifications are optional. For Gmail, add just:

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

Messages go to the same address by default. Use an app password. See the [usage guide](docs/usage.md#gmail-示例) (Chinese) for setup, other providers and disabling notifications.

Send one test email without logging into the university or starting a reservation task:

```bash
uv run main.py --test-mail
```

`event=test_mail_accepted` means SMTP accepted the message; check your inbox separately. Normal operation attempts mail for confirmed bookings, goal completion, required manual action and caught fatal errors. Three consecutive query or processing failures trigger one alert; recovery resets the count. Single failures and normal waiting do not send mail. Failed mail records `notification_failed` without automatic resending. Forced termination, power loss or an unavailable mail service can prevent notifications.

## Before relying on it

- Live login, read-only queries and delivery of the test email have been verified. Actual reservations, SMS recovery and long-running operation still need acceptance testing. Confirm bookings and attendance in the [SEU portal](https://ehall.seu.edu.cn/); these counts do not replace final departmental assessment.
- `.env` contains credentials; do not upload it. When a submission result is unclear, the client checks that activity before proceeding instead of blindly resubmitting.
- Development checks, detailed settings and acceptance records are in the [usage guide](docs/usage.md) (Chinese).

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Project](https://github.com/MuZiJin701/seu-lecture-reserve-http) · [Browser version](https://github.com/MuZiJin701/SEU_Lecture_Reserve)
