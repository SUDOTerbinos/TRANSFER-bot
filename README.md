# TRANSFER-bot

A Python Telegram bot for **consent-based group migration**. It lets group admins configure a destination and gives members a short-lived invite link that creates a join request for destination admins to review.

## Safety and scope

This implementation intentionally does **not** log in to personal Telegram accounts, collect Telegram authorization codes or cloud passwords, save MTProto user sessions, scrape group participants, or automatically add/invite existing members. Those flows can expose account credentials and create unwanted bulk invitations. Members choose whether to request access, and destination admins retain approval control.

> **Token warning:** if a bot token has been shared or exposed, treat it as compromised. Revoke it with BotFather and issue a fresh token before running the bot. No token is stored in this repository.

## Requirements

- Python 3.10+
- A Telegram bot created with BotFather
- `python-telegram-bot` (listed in `requirements.txt`)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set BOT_TOKEN to a fresh token from BotFather.
python bot.py
```

Optional configuration:

- `BOT_DATA_PATH` — JSON file for source-to-destination group mappings (default: `data/groups.json`).
- `LOG_LEVEL` — Python logging level (default: `INFO`).

The bot token is read from the environment and is never written to the JSON configuration. Keep `.env`, session files, and runtime data out of version control.

## Telegram setup and use

1. Add the bot as an administrator in both the source and destination groups. In the destination group, grant it the permission to invite users/create invite links.
2. In the source group, a person who is an administrator in **both** groups runs:
   ```text
   /setdestination @destination_group
   ```
   A numeric destination chat ID can be used instead of a username; run `/chatid` in a private destination group to see its ID.
3. Members in the source group run `/invite` (or tap **Get opt-in invite** after `/start`). The bot creates a link that expires after seven days and requires a join request. Destination admins approve requests in Telegram.
4. Source-group admins can use `/status` and `/cleardestination`.

The bot must remain an administrator in the destination with invite-link permission. It must be an administrator in the source group to verify admin-only configuration commands.

## Tests

```bash
python -m unittest discover -s tests -v
```
