# SEU Lecture Reservation · HTTPS

[中文](README.md)

A browser-free lecture reservation client for Linux servers, with foreground support on Windows. It polls for lectures, reads CAPTCHAs, submits reservations and checks the results.

The fixed goal is **8 attended humanities and scientific literacy series lectures, including at least one each in law, art and mental health**. Once series lecture reservations cover the goal, it waits for attendance; it exits when attendance meets the goal. “SEU Coffee Room” salons can still be booked when enabled, but do not count toward the goal. Test activities and ticket notices are not reserved or counted. The university makes the final assessment.

After upgrading, all history is recalculated: 7 series lectures plus 1 salon do not complete the goal. Salon reservations do not cover pending lecture requirements, but still block conflicting bookings. Salons are enabled by default and remain subject to existing filters and task pause/completion rules; see [configuration and migration](docs/usage.md#配置) (Chinese).

## 1. Install

Ubuntu 24.04: connect over SSH and install the tools. Skip tools already installed; omit `sudo` when running as root.

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows: install [Git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/getting-started/installation/), then open Git Bash for the project commands below. Ubuntu system installation and systemd commands apply only to Linux.

Download the project and install locked dependencies. uv prepares Python 3.12:

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

Run all subsequent commands from the project directory, using the same account for installation and operation. Keep the original directory and configuration of an existing checkout.

## 2. Configure

Copy the example on first use. If `.env` already exists, skip copying and edit it directly.

```bash
cp -n .env.example .env
chmod 600 .env
```

Open `.env` in a text editor and enter your university credentials:

```dotenv
SEU_CARD_NUMBER="your-card-number"
SEU_PASSWORD='your-password'
SEU_PREFERRED_CAMPUS='["九龙湖校区","四牌楼校区","丁家桥校区","无锡校区","苏州校区"]'
```

All university campuses are allowed for offline lectures by default. An empty campus list allows online lectures only; you can also keep just some campuses. Online lectures remain enabled. See [all settings](docs/usage.md#配置) (Chinese) for other filters.

Email is optional: leave its fields empty to disable it. To enable it, enter your own mailbox and its SMTP authorization code:

```dotenv
SMTP_USERNAME=your-address@qq.com
SMTP_PASSWORD='your-authorization-code'
```

QQ, 163 and 126 use authorization codes generated after enabling SMTP; Gmail uses an app password. Messages go to yourself by default. Follow [password setup](docs/usage.md#获取发信密码) before continuing. See [email settings](docs/usage.md#邮件配置) for changing providers or recipients (Chinese).

Save `.env` and restart the program to apply changes. The program reads the file directly; do not run `source .env`. Process environment variables take precedence. Keep credentials and state files private.

## 3. Check first

```bash
uv run --locked main.py --check
```

`event=read_only_complete` means login, current data and CAPTCHA image checks finished. This command does not book, change task state or send email. See [troubleshooting](docs/usage.md#排错) (Chinese) if it fails.

If email is configured, run the following command and confirm receipt in your inbox. Otherwise skip it.

```bash
uv run --locked main.py --test-mail
```

## 4. Start reservations

These commands submit real reservations. **Run only one instance per university account across all machines and both projects.** If upgrading from `nohup`, follow the [migration steps](docs/usage.md#从-nohup-迁移) first and keep your state.

Run in the foreground locally; press `Ctrl+C` to stop:

```bash
uv run --locked main.py
```

For Ubuntu background operation, first run `uv run --locked main.py --status`, check `process_running=false`, and confirm no other machine is using the same account. Then start the service:

```bash
mkdir -p state
chmod 700 state
touch state/runner.log
chmod 600 state/runner.log
sudo systemd-run --unit=seu-lecture-reserve-http --collect --service-type=exec \
  --uid="$(id -un)" --working-directory="$PWD" --expand-environment=no \
  --property=Restart=no --property=UMask=0077 --property=TimeoutStopSec=60 \
  --property="StandardOutput=append:$PWD/state/runner.log" \
  --property=StandardError=inherit \
  "$(command -v uv)" run --locked main.py
```

It continues after SSH disconnects. Run these startup commands again after the program exits or the server reboots. The service reads the project's `.env`; it does not automatically inherit temporary SSH settings or proxy variables. See [service environment](docs/usage.md#服务环境) before using a proxy. Adjust log paths if you changed the state directory.

## 5. View, stop and resume

On either Windows or Ubuntu, open another terminal in the project directory:

```bash
uv run --locked main.py --status
```

Read `process_running`, `phase` and `last_query_at` together. `status=scanning` describes the task; it does not prove process health or reservation success. See [status and logs](docs/usage.md#状态与日志) for selection reasons and more fields (Chinese).

On Ubuntu, inspect the service and recent output:

```bash
sudo systemctl status seu-lecture-reserve-http.service --no-pager
tail -n 20 state/runner.log
```

Stop the background service, then confirm `process_running=false`:

```bash
sudo systemctl stop seu-lecture-reserve-http.service
uv run --locked main.py --status
```

The stopped service may be collected and reported as not found; task state and logs remain. Running the startup commands again resumes the task without cancelling reservations. If SMS authentication requires input, background operation saves the task and exits. Confirm it has exited before handling the prompt in the foreground. Stopping that foreground process and starting a new background process creates a new login session and may require SMS again; authenticate early in the final process and keep it running through the opening.

An uncertain reservation is checked through both my activities and the activity list while other submissions pause. A failed, contradictory or unreadable check is not a valid check and does not advance automatic release. After 3 valid checks without the record, scanning resumes only when a separate time hold can protect each unresolved reservation. Conflicting candidates are skipped, each original activity may be submitted at most twice more, and explicit success evidence immediately counts toward conflicts and planned progress. Holds expire after about ten minutes or at the activity's start; server-directed waits take priority. Missing time or category data in old state preserves protection and pauses new submissions. `uv run --locked main.py --resolve-pending` is a manual fallback that clears only the matching activity and never submits. See [recovery](docs/usage.md#入口与恢复) and [opening and congestion recovery](docs/usage.md#开抢与拥塞恢复) (Chinese).

Stop the old instance before updating, keeping `.env` and the state directory. Run `uv sync --locked` after updating, then start the program as before. An existing process does not load fixes automatically.

As of 2026-10-29, **545 offline tests pass and 3 are skipped for platform limitations**; mypy passes. The main thread makes decisions and submits reservations serially. During openings, one read-only thread polls the activity list at a **0.10-second** target interval; another refreshes my activities about every **5 seconds**. New history replaces the main-loop cache promptly, with a 10-second cache limit while background refresh is enabled. Normal scans remain 10 seconds and opening network-error retries remain 0.25 seconds; server rate limits and `Retry-After` take priority. See the [usage guide](docs/usage.md) for configuration and recovery.

Campus application CAS login is preferred when the entry supplies that service. The program does not change Clash/TUN, DNS or routes; users control the network path. Whether campus login requires SMS still needs real verification. `--check` and normal operation are separate processes and do not share login sessions. Complete authentication early in the process that will make reservations, and keep it running through the opening.

Real reservations were confirmed by activity ID on [2026-10-26](docs/acceptance/2026-10-26-live-run.md) and [2026-10-28 at 19:00](docs/acceptance/2026-10-28-live-run.md). Today's [read-only comparison](docs/acceptance/2026-10-29-read-only-probe.md) completed 592 activity-list queries without observing rate limiting or an obvious slowdown at higher polling rates. This short test does not establish opening success rates. Real campus CAS login, both readers running together, CAPTCHA prefetch gains, SMS recovery after session expiry and at least 24 hours of stability still require separate acceptance. See the [acceptance boundaries](docs/usage.md#验收记录) and [business rules](docs/requirements.md) (Chinese).

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Browser version](https://github.com/MuZiJin701/seu_lecture_reserve) · [Historical promotional assets](docs/promo/usage.md)
