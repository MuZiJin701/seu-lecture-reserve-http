# SEU Lecture Reservation · HTTPS

[中文](README.md)

A browser-free lecture reservation client for Linux servers, with foreground support on Windows. It polls for lectures, reads CAPTCHAs, submits reservations and checks the results.

The fixed goal is **8 attended series lectures or salons, including at least one each in law, art and mental health**. Once reservations cover the goal, it waits for attendance; it exits when attendance meets the goal. Test activities and ticket notices are not reserved or counted. The university makes the final assessment.

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

The stopped service may be collected and reported as not found; task state and logs remain. Running the startup commands again resumes the task without cancelling reservations. If SMS authentication requires input, background operation saves the task and exits; confirm it has exited, handle the prompt in the foreground, then resume background operation. An uncertain reservation result is re-checked against the original activity; if it stays unconfirmed, the program refreshes the school record and continues without a manual restart.

This version has passed offline checks. A real submit-and-cancel flow was verified on a test activity on 2026-10-25. Target-lecture hit rate, SMS recovery and at least 24 hours of operation still require separate acceptance testing; see [acceptance boundaries](docs/usage.md#验收记录). [Business rules](docs/requirements.md) and the [usage guide](docs/usage.md) contain the details (Chinese).

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Browser version](https://github.com/MuZiJin701/seu_lecture_reserve) · [Historical promotional assets](docs/promo/usage.md)
