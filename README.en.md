# SEU Lecture Reservation · HTTPS Edition

[中文](README.md)

Keep looking for suitable SEU lectures on a server, recognize image CAPTCHAs, and attempt a reservation. The client uses HTTPS directly, needs no browser, and works toward 8 school-recorded attendances, including at least one each in law, art and mental health.

Offline tests, live login and read-only queries have passed. Live reservations and long-running use still need verification. Start with the read-only check.

## Quick start

Use Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/). Commands below use a Linux shell. Windows compatibility paths have passed offline simulations; acceptance on Windows is still pending.

**1. Download and install dependencies**

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

If you already have the project, enter its existing directory instead.

Dependencies include `tzdata` to provide `Asia/Shanghai` on systems without an IANA time zone database, including Windows. After updating the dependencies and lockfile, run `uv sync --locked` again to install it.

**2. Fill in `.env`**

Copy the example for first-time setup. Keep your existing `.env` if you have already filled it in.

```bash
cp -n .env.example .env
chmod 600 .env
```

In Windows PowerShell, use the following to keep an existing configuration or copy the example. Windows ACLs control access to `.env` and the state directory; keep them in a directory only you can access.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Open `.env` in a text editor and enter your credentials and filters, for example:

```dotenv
SEU_CARD_NUMBER="your-card-number"
SEU_PASSWORD='your-password'
SEU_PREFERRED_CAMPUS='["无锡校区"]'
```

Campus names must match the university system; change the example to suit your needs. Credentials, filters and mail settings share this file. The client loads it automatically; no `export` or `source` is needed.

**3. Check login and queries**

```bash
uv run main.py --check
```

This only logs in, queries data and fetches a CAPTCHA. It does not submit a reservation or require mail settings. Success prints `event=read_only_complete`.

On failure, `event=task_failed` includes `stage` and a safe `reason` code, plus `http_status` for HTTP errors. TLS failures include the school domain in `host`, a `tls_reason` category, and `proxy_used` when available. Include this entire line when reporting a failure.

If local DNS is confirmed to resolve `vpn.seu.edu.cn` incorrectly, set the optional `SEU_VPN_IP` in the project's `.env`. Domain certificate verification remains enabled, and system DNS and TUN settings stay unchanged. See the [usage guide](docs/usage.md#仅在项目内绕过网关-dns-故障) for configuration and limitations. Leave it unset on working networks.

**4. Start reservations**

Email notifications are optional. Without mail configuration, reservations still work and events such as success, required manual authentication and persistent errors remain in the terminal and local log.

For Gmail notifications, enter just your full email address and app password in `.env`. Messages go to the same address by default:

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

The client automatically uses `smtp.gmail.com:465` with implicit TLS. See the [Gmail setup guide](docs/usage.md#gmail-示例) (Chinese) for app passwords and advanced settings for other providers. Existing complete SMTP configurations still work. Incomplete or malformed mail settings stop startup.

When upgrading, nonempty `SMTP_HOST`, `MAIL_FROM` and `MAIL_TO` values keep overriding automatic defaults. To disable email, clear all mail fields; `SMTP_PORT` may be empty, absent or left at the default `465`. Also check matching process environment variables.

```bash
uv run main.py
```

`main.py` calls the existing CLI and accepts the same check, status and new-task options. Use `uv run --locked main.py` to require an up-to-date lockfile. The original `seu-reserve` command remains available.

This command submits real reservations. The client checks every 10 seconds, prioritizes missing required categories, and continues after each confirmed booking. Once recorded attendance plus valid upcoming bookings covers the goal, it pauses new bookings and keeps checking. It exits only when recorded attendance meets the goal. Cancelled or breached bookings are replaced; ended bookings without a confirmed attendance or breach status pause new bookings to avoid replacements during reporting delays. SEU Coffee Salon events are neither booked nor counted. There is no assessment deadline setting.

Progress comes from “My activities,” including manual and other-tool bookings, deduplicated by activity ID. You do not maintain completion counts yourself. School check-in records are the agreed counting basis and do not replace final departmental assessment. See the [API investigation](docs/completion-records.md) (Chinese) for evidence and historical-completeness limits.

**A quiet terminal does not necessarily mean the client is stuck.** Successful queries with no suitable lectures lead to a quiet wait and do not count as failures. Errors produce events and longer retry delays; restarting preserves any remaining delay. See [status and logs](docs/usage.md#状态与日志) (Chinese).

## Common settings

Edit `.env`, then restart. Lists use JSON arrays, as in the campus example above.

| Setting | Purpose |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | Allowed offline campuses; multiple names are supported. The default empty list excludes offline lectures |
| `SEU_REQUIRED_CATEGORIES` | Candidate categories and their order; keep mental health, law, art and other series. Goal counts remain independent of these filters |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | Allow online / offline lectures; use `true` or `false` |
| `SEU_PREFERRED_LECTURES` | Match exact full titles; `'[]'` leaves titles unrestricted |
| `SMTP_*` / `MAIL_*` | Optional email notifications; Gmail needs only `SMTP_USERNAME` and `SMTP_PASSWORD` |

Exact titles still obey all other filters and time-conflict checks. Process environment variables take precedence over matching `.env` settings.
See [.env.example](.env.example) for all settings and the [usage guide](docs/usage.md) (Chinese) for formats and troubleshooting.

## Stop and resume

- Keep an interactive SSH terminal open for foreground use. See [server background operation](docs/usage.md#服务器后台运行) (Chinese) to continue after disconnecting SSH. If SMS verification is required, enter `send` when prompted, then enter the six-digit code.
- Press `Ctrl+C` to stop without cancelling any reservation. Running the original start command resumes the same task.
- If a submission result is unclear, the client checks that reservation before doing anything else. Do not delete task state to force another attempt.
- In the new state format, `done` means the entire attendance goal is complete; restarting exits immediately. Use this command to recheck current school records; it will not book extra lectures if the goal remains complete. Old single-booking completion states are automatically migrated and rechecked, preserving pending submissions, cooldowns and backoff:

```bash
uv run main.py --new-task
```

Check local task status:

```bash
uv run main.py --status
```

This reads the local task record, not process health. `status=scanning` does not prove the process is running or its latest query succeeded. You do not need to start another reservation process to check status.

## Current status

Live login, activity queries and CAPTCHA retrieval have been verified, and the status, activity ID and time fields in “My activities” have been adapted. The available-activity list was empty during verification. Nonempty candidate data, actual reservations, email delivery, SMS recovery and operation across days still need acceptance testing. Passing the read-only check does not verify reservations; confirm the final result in the [SEU portal](https://ehall.seu.edu.cn/).

The real `.env` is excluded from Git. Do not upload credentials or personal activity records. Development checks are `uv run --locked pytest` and `uv run --locked mypy seu_reserve`.

Related project: the [browser version](https://github.com/MuZiJin701/SEU_Lecture_Reserve). This project is an independent HTTPS implementation.
