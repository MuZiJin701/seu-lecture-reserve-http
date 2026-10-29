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

The stopped service may be collected and reported as not found; task state and logs remain. Running the startup commands again resumes the task without cancelling reservations. If SMS authentication requires input, background operation saves the task and exits; confirm it has exited, handle the prompt in the foreground, then resume background operation.

An uncertain reservation is checked through both my activities and the activity list while other submissions pause. A failed, contradictory or unreadable check is not a valid check and does not advance automatic release. After 3 valid checks without the record, scanning resumes only when a separate time hold can protect each unresolved reservation. Conflicting candidates are skipped, each original activity may be submitted at most twice more, and explicit success evidence immediately counts toward conflicts and planned progress. Holds expire after about ten minutes or at the activity's start; server-directed waits take priority. Missing time or category data in old state preserves protection and pauses new submissions. `uv run --locked main.py --resolve-pending` is a manual fallback that clears only the matching activity and never submits. See [recovery](docs/usage.md#入口与恢复) and [opening and congestion recovery](docs/usage.md#开抢与拥塞恢复) (Chinese).

Stop the old instance before updating, keeping `.env` and the state directory. Run `uv sync --locked` after updating, then start the program as before. An existing process does not load fixes automatically.

As of 2026-10-29, **534 offline tests pass and 3 are skipped for platform limitations**; mypy passes. This round speeds up openings: the opening wait is driven by completion events from the background poller instead of sleeping the full interval, waking only for polls newer than the consumed version so stale notifications do not spin and a poll completed across the clear cannot be lost; the snapshot and its version are read under the same lock. An optional poller alignment target (`SEU_POLLER_ALIGN`, off by default until its error is measured in a real opening) is available. About 2 seconds before an opening the main session prewarms my activities (`SEU_PREWARM`), and about 0.6 seconds before it prefetches and recognises a CAPTCHA (`SEU_CAPTCHA_PREFETCH`); prewarm and prefetch are on by default and can be disabled separately, both are bounded by a pre-opening timeout and skipped when too little time remains, and preparation is deduplicated by the school opening timestamp, and the local opening estimate subtracts a margin of `1.1 s + the list-snapshot age + the list-request time` from the school-clock-adjusted timestamp; when that margin exceeds 2 seconds, prewarm, prefetch and optional alignment are skipped and `preparation_skipped` is recorded (only earlier corrections). Preparation and alignment target the next not-yet-open lecture, so an older window still inside the fast-polling period does not block later ones. During openings a background “my activities” refresh also runs (`SEU_HISTORY_BACKGROUND`, on by default, about every 20 seconds; the main loop consumes only snapshots at most 30 seconds old and error-free, falls back to a synchronous query on stale, failed or invalid data, shares one server-directed wait deadline with the activity-list thread; each scan checks that shared deadline before restarting either reader and the readers stop internally while it is pending, and during backoff both readers pause with cooldowns clamped to the shared deadline, restarting on their own cooldowns; about 20 extra requests per window, and disabling it restores the 60-second cache plus synchronous refresh), and a stale activity-list snapshot with a poll in flight is reused after waiting at most 0.3 seconds for a version newer than that stale snapshot. Candidate-to-submit keeps only two persists (captcha-phase entry and pending+`submitting` combined), with `rows_fetch_seconds`, `rows_data_age`, `precommit_save_seconds` and background-history diagnostics added. Event wake-up and the optional alignment add no requests and never bypass server-directed 429/`Retry-After` waits, and a prefetched CAPTCHA that times out, is older than 5 seconds or rejected by the server falls back to a fresh fetch. Pending checks prefer positive evidence from the in-memory poller snapshot, and coalesced health snapshots make `--status` lag at most about one second during normal waits while critical phases are written before their blocking request. This round has offline evidence only and has not been re-verified in a real opening. Earlier rounds excluded salons from goal counts while keeping them bookable, and fixed findings from the 19:00 live-opening monitor: a duplicate confirmation diagnostic and flooded `health_write_failed` notices when a Windows reader briefly holds the state file. Earlier repairs fixed the shared background rate-limit wait (including re-checks before captcha/submission) and unified absence counting so that any aborted check — failed, rate-limited, or unreadable history — restarts the consecutive-valid count. Background-path regressions were added, and normal-opening and combined 503/504/429 congestion replays keep their previous request and waiting behaviour. A real submit-and-cancel flow was verified on a test activity on 2026-10-25. On 2026-10-26 a real offline art lecture was reserved locally and confirmed by activity ID; on 2026-10-28 at 19:00 another offline frontier-technology lecture was reserved and confirmed by activity ID (see the 2026-10-28 live-run log). Hit rate, SMS recovery, at least 24 hours of operation and this round's gains still require separate acceptance testing; passing offline checks does not guarantee a reservation. On 2026-10-29 three offline-reproduced check/wait defects were fixed: a server-directed wait learned by a background reader now stops both query threads and is checked before every synchronous history request; an activity-list row without an ID no longer counts as valid absence evidence or releases a pending task; and a background history snapshot is validated against the structure the scan actually uses, falling back to the synchronous query in the same scan when it is malformed. Three regressions were added; a follow-up now shares one lock-protected wait deadline between both reader threads, validates conflict intervals for all reservations (including salons) and gives background readers the 6-second opening budget, with five more regressions; 527 offline tests pass and 3 are skipped, mypy passes, and none of this has been deployed or re-verified live. The same day continued the speed-up: during openings the history reader is refreshed on demand (2 s throttle) and the candidate path waits for that single background snapshot instead of immediately racing it with a duplicate synchronous query; stopping a reader only signals it, and the worker closes its session after the in-flight request so the main loop no longer waits a fixed second; a stale list snapshot now waits for the single in-flight list query before falling back to a duplicate synchronous one; and the submission reply diagnostic records the submit connection time. Four regressions were added; 531 offline tests pass and 3 are skipped, mypy passes, and these changes are not deployed or live-verified. Two further fixes landed the same day: when history resolution takes longer than 0.5 s, a newer background list snapshot is consumed and candidates are recomputed before selection; and a refresh request made while a history query is already in flight is satisfied by that query instead of adding an immediate second one. Two regressions were added; 533 offline tests pass and 3 are skipped, mypy passes, and none of this has been deployed or live-verified. The refresh/stop race is also fixed: when an in-flight query receives a refresh request and then a stop signal, clearing the satisfied refresh no longer swallows the stop wake, so the worker exits without a full interval delay. One combined regression was added; 534 offline tests pass and 3 are skipped, mypy passes, and none of this has been deployed or live-verified. See [acceptance boundaries](docs/usage.md#验收记录), the [live-run log (2026-10-26)](docs/acceptance/2026-10-26-live-run.md), [2026-10-28](docs/acceptance/2026-10-28-live-run.md), [business rules](docs/requirements.md) and the [usage guide](docs/usage.md) (Chinese).

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Browser version](https://github.com/MuZiJin701/seu_lecture_reserve) · [Historical promotional assets](docs/promo/usage.md)
