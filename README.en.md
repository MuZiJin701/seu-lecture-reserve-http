# SEU Lecture Reservation · HTTP console

[中文](README.md)

A multi-account lecture reservation tool with a Chinese console and the existing CLI. The console uses plain HTTP, has no administrator login and must run only on a trusted LAN or VPN; the owner or a few trusted operators manage school accounts from it. Each account independently authenticates, reserves, verifies results and tracks attendance progress. Requests to the university still use certificate-validated HTTPS.

The goal remains **8 attended humanities and scientific literacy lectures, including law, art and mental health**. Enabled salons can be reserved but do not count. Test activities and ticket notices are excluded. The university makes the final assessment.

![Web console offline demo (pre-simplification version)](docs/images/web-console.png)

The screenshot uses offline synthetic accounts and state, not live school records.

## Install

Production deployment targets Linux with Python 3.12 and uv. Windows supports the CLI and offline checks.

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
cp -n .env.example .env
```

Keep an existing configuration and add the Web fields from [.env.example](.env.example). The program loads `.env` from the working directory; do not source it. Process environment variables take precedence.

## Configure and start the console

The console uses the settings below; no administrator password or TLS certificate is needed:

```dotenv
SEU_WEB_HOST=0.0.0.0
SEU_WEB_PORT=8080
SEU_DATA_DIR=state
```

Use `--config /absolute/path/.env` to select another configuration file. There is no administrator login; deploy only on a trusted LAN or VPN. Any reachable host on the same network can operate the console and access plaintext school credentials, so never expose the port publicly.

```bash
uv run --locked seu-reserve-web
```

Open `http://127.0.0.1:8080` (or the server address). Add a school account, configure filters and its recipient, then explicitly start it. Send SMS and enter its code in that account's authentication panel; other accounts continue. Closing the page does not stop reservations.

See the [Web console, systemd and risk notes](docs/web.md) (Chinese). A new deployment starts from an empty data root and does not detect or import old single-account task files.

## Mail and account data

Keep the shared SMTP sender in the server `.env`, using `SMTP_USERNAME` and `SMTP_PASSWORD`. Set one recipient per school account in the console. An empty recipient disables its mail; the sender is never a fallback recipient. Enabling mail with invalid shared configuration blocks that account's startup. Delivery failure does not change reservation results. Only major alerts and optional success notices are sent; the console can send a test email.

As explicitly chosen, school passwords and cookies are stored **locally in plaintext**, used only on the server, and never returned to the page. Keep `.env` and the data directory outside version control. Because storage is plaintext and file permissions are not restricted, anyone who can access the data directory can read the credentials. Stop an account before deletion; unresolved submissions or interval holds must be checked first. Deleting local data does not cancel school reservations.

Each school account has its own process, and the console allows only one worker per account. The identity lock has been removed, so one school account may use concurrent sessions (single account, single machine, roughly 4 minutes of read-only evidence; see the [concurrent login record](docs/acceptance/2026-10-30-concurrent-login.md)); avoid extra diagnostic logins during the opening period. Ten accounts is an acceptance target, not a hard cap; actual capacity depends on the machine. Two task sets in different data roots cannot discover or coordinate with each other.

## Existing CLI

```bash
# Managed accounts use their unified state. The selector accepts a card number or internal ID.
uv run --locked main.py --account YOUR_CARD_NUMBER --status
uv run --locked main.py --account YOUR_CARD_NUMBER --check
uv run --locked main.py --account YOUR_CARD_NUMBER
```

The old single-account `.env` mode without `--account` is retained: `uv run --locked main.py` still works. Use `--account` for managed accounts. See the [CLI usage guide](docs/usage.md) (Chinese).

## Validation boundaries

Existing single-account live reservations are recorded for [2026-10-26](docs/acceptance/2026-10-26-live-run.md), [2026-10-28](docs/acceptance/2026-10-28-live-run.md) and [2026-10-29](docs/acceptance/2026-10-29-live-run.md). Evidence for the HTTP console simplification and concurrent sessions is in the [current HTTP simplification record](docs/acceptance/2026-10-30-http-console.md) and the [concurrent login record](docs/acceptance/2026-10-30-concurrent-login.md); the earlier HTTPS-version offline evidence is kept in the [2026-10-30 Web offline acceptance](docs/acceptance/2026-10-30-web-offline.md). Real multi-account operation, Linux systemd and long runs require separate acceptance.

Existing 429/`Retry-After`, short backoff and fallback handling is retained. The earlier claim that the school actively throttles requests was an unsupported assumption and has been withdrawn. [Existing measurements](docs/acceptance/2026-10-29-read-only-probe.md) observed no business-API throttling; that assumption must not explain failures or set polling rates. Single-account samples do not establish multi-account capacity.

Maintainer: [MuZiJin701](https://github.com/MuZiJin701) · [Requirements](docs/requirements.md) · [Glossary](GLOSSARY.md) · [Historical promotional assets](docs/promo/usage.md)
