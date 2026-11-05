# SEU Lecture Reservation Console

[中文](README.md)

Manage multiple school accounts and reserve lectures based on category, campus and schedule conflicts. A Chinese web console and a CLI are available. You complete SMS verification when required.

**Use on a trusted LAN or VPN.** The console uses plain HTTP, and school passwords and login cookies are stored locally in plaintext. Keep configuration and data private. Requests to the school use certificate-verified HTTPS.

## Install and start

Prepare Python 3.12 and uv. For continuous operation, see the [Linux deployment guide](docs/web.md#systemd-常驻) (Chinese). Windows supports the CLI and offline checks.

1. Download the project and install the locked dependencies:

   ```bash
   git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
   cd seu-lecture-reserve-http
   uv sync --locked
   ```

2. Copy [.env.example](.env.example) to `.env` and set an administrator account:

   ```dotenv
   SEU_WEB_ADMINS='{"admin":"replace-with-your-own-password"}'
   ```

3. Start the console:

   ```bash
   uv run --locked seu-reserve-web
   ```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) locally. For a server, use its IP over the trusted network. `0.0.0.0` is only the bind address.

## Use

Log in → add a school account → choose categories and campuses → save → start reservations. When prompted, send an SMS and enter the six-digit code for that account.

- Closing the browser or signing out leaves the task running. Use the Stop button to stop it.
- The machine's clock drives polling phases, my-activities warmup, captcha prefetch and reservation-window checks. Keep it synchronized with NTP or an equivalent time service. Captcha prefetch targets 2.7 seconds before the published opening time. See the [polling strategy](docs/thread-query-strategy.md) (Chinese).
- The reference goal is eight valid reservations, including at least one each in law, art and mental health. Attendance is not required for this count; salons do not count. **Reservations continue after the goal is reached.** The university decides official credit.
- If a submission has an uncertain result, the program checks school records before retrying. Keep its state files. Stopping a task or deleting a local account does not cancel school reservations.

| What you need | Guide (Chinese) |
| --- | --- |
| Accounts, SMS, deployment, backups or upgrades | [Web operations and deployment](docs/web.md) |
| CLI commands, email or troubleshooting | [Usage guide](docs/usage.md) |
| Query intervals, captcha preparation or timeout recovery | [Reservation query policy](docs/thread-query-strategy.md) |
| Filtering, counts, conflicts or uncertain submissions | [Business rules](docs/requirements.md) |

Accounts added in the console also work with the CLI. See [commands and recovery](docs/usage.md#入口与恢复).

## Live results and development

Neither account submitted a reservation during the [November 4 opening](docs/acceptance/2026-11-04-live-opening.md). Submissions now use the last reliable my-activities result without waiting for a refresh. Earlier deadline and retry fixes passed real-school read-only checks. **Their performance during a congested opening is still unverified.** See [current validation status](docs/acceptance/2026-11-05-prep-history-goal.md) for evidence and limits.

See [development checks](docs/usage.md#开发检查) and the [glossary](GLOSSARY.md). Retired designs and offline reports remain in Git history.
