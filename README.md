# Telegram Member Transfer Bot (consent-based)

A local-first Python 3.12+ Telegram bot for community migrations. It validates that the customer and bot are authorized group administrators, creates an expiring destination **join-request** link with the official Bot API, and queues one optional announcement for an explicitly registered human operator to approve.

It uses the Bot API only. **Workers are human operator records, not personal Telegram account sessions.** All group operations are performed by the one configured bot identity.

## Safety, scope, and Bot API limits

- The bot never asks for or stores Telegram login codes, cloud passwords, or personal-account sessions.
- It does not call `get_participants()`, read arbitrary membership lists, or add/invite existing members individually.
- A source-group administrator must confirm an order. A registered operator must separately approve the announcement. Members choose whether to request entry, and destination admins review join requests.
- The Bot API cannot inspect arbitrary personal-account membership data. It can only access groups the bot has been added to. Customers must be admins in both groups; the bot must be an admin in both groups and have invite-link permission in the destination.
- Public `@usernames`, `t.me/username` links, and numeric group IDs are accepted. A private `t.me/+...` invite link cannot be resolved by the Bot API. Add the bot to that group and run `/chatid` there, then use its numeric ID.
- Creating a join-request link verifies that Telegram accepts the link request, but cannot guarantee a group has capacity or that admins will approve a request.
- Telegram does not allow an invite link to combine `creates_join_request` with a `member_limit`. This bot uses join requests and a 7-day expiry instead. The requested join count is a **completion threshold**, not a guaranteed hard cap; pending or near-simultaneous requests may result in a few extra joins before the link is revoked.
- Confirmed joins are counted only from Bot API `chat_member` updates carrying the campaign invite link. If Telegram does not expose the link on an update, that join is not attributed to the campaign. **Links generated, announcements distributed, and confirmed joins are separate metrics.** An order is completed only after the requested number of attributed joins is verified.
- A quota action is one successfully posted source-group announcement, not one invite link click or one member. The default is 50 successful announcements per worker per rolling 24 hours. Failed sends are retained separately and do not count as successes; ambiguous sends hold a quota slot until administrator review. Quotas are safeguards, not a promise against Telegram limits.
- If Telegram rejects or restricts an operation, that order is paused for review. The scheduler does not rotate workers to continue a restricted operation or automatically retry an uncertain announcement.

The SQLite database contains group IDs, order data, worker Telegram IDs, invitation links, and keyed pseudonymous join-deduplication hashes; raw joiner Telegram IDs are not stored. Protect and back up the runtime `data/` directory; it is ignored by Git. Do not share database files or invite links publicly beyond the approved campaign.

> **Token security:** the bot token previously pasted into chat must be treated as compromised. Revoke it with BotFather and create a fresh token before running this project. Never commit `.env` or put a real token in source code.

## Project layout

```text
bot.py
config.py
database.py
models.py
requirements.txt
.env.example
.gitignore
handlers/
  start.py
  orders.py
  workers.py
  admin.py
  callbacks.py
services/
  worker_manager.py
  order_manager.py
  task_scheduler.py
  invite_service.py
  quota_manager.py
  notification_service.py
tests/
  test_orders.py
  test_quotas.py
  test_scheduler.py
```

SQLite uses foreign keys, indexes, parameterized SQL, atomic `BEGIN IMMEDIATE` transactions, unique idempotency keys, and a persistent task queue. Safe validation tasks are restored after a restart. If the process stops while an announcement send is in flight, the result is treated as uncertain and sent to administrator review rather than retried.

## Windows PowerShell: install and run

### 1. Install and verify Python

Install Python 3.12 from the official Python installer, or in an elevated/ordinary PowerShell window with `winget` available:

```powershell
winget install --exact --id Python.Python.3.12
py -3.12 --version
```

The version command should print Python 3.12.x. If `py` is not found, reopen PowerShell after installation or use the Python installer and enable its PATH option.

### 2. Get the project from GitHub

This session's code is on the branch below. Clone it into a local project folder:

```powershell
Set-Location $HOME
git clone --branch arena/373b4c2e-transfer-bot https://github.com/SUDOTerbinos/TRANSFER-bot.git telegram-member-transfer
Set-Location .\telegram-member-transfer
```

If you already cloned the repository, `Set-Location` to its folder and run `git pull origin arena/373b4c2e-transfer-bot`.

### 3. Create and activate a virtual environment

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python --version
```

The execution-policy change applies only to the current PowerShell process. If activation is blocked, use `\.venv\Scripts\python.exe` directly for the following commands.

### 4. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 5. Configure `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `BOT_TOKEN` to the **new** BotFather token. For first-run administrator-ID setup, `.env.example` leaves `ADMIN_IDS` blank, which disables all administrator commands. Start the bot, send `/myid` to it in a private chat, stop the bot with `Ctrl+C`, put that numeric ID in `ADMIN_IDS`, then restart. You can configure multiple administrators as a comma-separated list, for example `ADMIN_IDS=12345678,87654321`.

Configuration keys:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Fresh Telegram Bot API token; required. |
| `ADMIN_IDS` | Comma-separated administrator Telegram user IDs. Blank means no admin is authorized. |
| `DATABASE_PATH` | SQLite file path; defaults to `data/telegram_migration.sqlite3`. |
| `DEFAULT_ACTION_QUOTA` | Initial per-worker quota; defaults to `50`. |
| `QUOTA_WINDOW_HOURS` | Initial rolling window; defaults to `24`. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `MIGRATION_ANNOUNCEMENT` | Announcement template; `{campaign_name}` is replaced with the order's campaign name. |

The admin commands `/set_quota` and `/set_window_hours` update persistent SQLite settings. Those admin-set values take precedence over environment defaults after first initialization.

### 6. Start the bot

```powershell
python bot.py
```

Keep this terminal running. The bot uses long polling; no paid service is needed for local operation.

### 7. Configure Telegram groups and workers

1. Add the bot as an administrator to both source and destination groups. It needs permission to create invite links in the destination. It must be able to post messages in the source group.
2. Start a private chat with the bot and send `/start`.
3. Add authorized human operators in the admin chat:
   ```text
   /add_worker TELEGRAM_USER_ID Display Name
   ```
   Each worker must open a private chat with the bot and send `/start` to receive assignments.
4. A customer who is an admin in both groups creates an order with **Create Transfer Order**, enters the source and destination references, campaign name, and target join count, then confirms.
5. A registered worker reviews the private assignment and taps **Distribute approved announcement**. This posts one optional join-request link in the source group. Destination admins approve or reject requests in Telegram.

Private group IDs can be displayed by running `/chatid` while the bot is in that group. Customer order conversations should be started in a private chat with the bot.

### 8. Run automated tests

```powershell
python -m unittest discover -s tests -v
```

The tests cover order idempotency and persistence, admin/owner authorization rules, rolling quotas and concurrent reservations, task scheduling, and restart recovery.

## Commands

### Customers and operators

- `/start` — main menu
- `/help` — workflow and Bot API limitations
- `/myid` — display your numeric Telegram user ID for initial setup
- `/orders` — your orders (admins can use `/orders` for the admin listing)
- `/order_status ORDER_ID` — inspect an order you own, or any order as an admin
- `/cancel_order ORDER_ID` — cancel your own order; admins may cancel any order
- `/status` or `/worker_status` — worker quota/status, or your latest order status
- `/cancel` — abandon an unfinished order form
- `/chatid` — display the current chat ID

### Administrators

- `/admin` — administrator menu
- `/workers` — worker state, current assignment, quota, and next eligible time
- `/add_worker USER_ID DISPLAY_NAME` — register an authorized human operator
- `/pause_worker ID [reason]`, `/resume_worker ID`, `/disable_worker ID`
- `/orders`, `/order_status ID`, `/cancel_order ID`
- `/stats` — aggregate statistics (customers see only their own)
- `/set_quota NUMBER` — configure rolling-window actions per worker
- `/set_window_hours HOURS` — configure the reporting window
- `/logs` — recent sanitized errors and review events

Administrator commands are checked against the IDs in `ADMIN_IDS` at runtime. Disabling a worker with an assigned order puts that order into review; resuming a paused active assignment sends it back to the **same** worker.

## Troubleshooting

- **`BOT_TOKEN is missing or malformed`** — replace the placeholder in `.env` with a fresh BotFather token. Do not paste tokens into chat or commit `.env`.
- **Admin commands say restricted** — check `ADMIN_IDS` is the numeric Telegram user ID returned by `/myid`, then restart. An empty value deliberately disables admin access.
- **Group inaccessible or `chat not found`** — add the bot to the group. For private groups, use `/chatid` and enter the numeric ID; private invite URLs cannot be resolved by this Bot API workflow.
- **Permission check fails** — the customer must be an admin in both groups; the bot must be an admin in both and have invite-link permission in the destination.
- **Worker never receives an assignment** — confirm `/add_worker` used the right numeric user ID and that the worker has sent `/start` to the bot. Check `/workers` and `/logs`.
- **Order remains queued** — `/order_status ID` shows the queue reason. Add/resume a worker or wait for the displayed quota eligibility time. The scheduler will reconsider the saved task.
- **PowerShell cannot activate `.venv`** — run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal, or invoke `\.venv\Scripts\python.exe` directly.
- **SQLite reports locked** — stop duplicate bot processes using the same token/database and restart one process. Back up the database before manually editing it.

## Optional deployment

The initial setup is completely local and free. For an always-on deployment, use a host that provides a persistent disk for the SQLite database and supports Python 3.12. Set environment variables through the host's secret manager (do not upload `.env`), keep one polling process per bot token, configure a restricted service account, and back up the SQLite file securely. Test restore/restart behavior before routing real communities through it. A stateless or ephemeral filesystem will lose the database and is unsuitable.
