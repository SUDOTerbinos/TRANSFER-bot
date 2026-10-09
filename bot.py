"""Opt-in Telegram group migration helper.

This bot intentionally does not log into personal Telegram accounts, collect
Telegram login codes/passwords, scrape group membership, or add people without
their action. It creates short-lived join-request links for a group admin's
configured destination.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from store import DestinationStore

logger = logging.getLogger(__name__)
GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"creator", "administrator"}
INVITE_TTL = timedelta(days=7)
INVITE_COOLDOWN_SECONDS = 30

Reply = Callable[..., Awaitable[Message]]


def _store(context: ContextTypes.DEFAULT_TYPE) -> DestinationStore:
    return context.application.bot_data["destination_store"]


def _is_group(chat_type: str) -> bool:
    return chat_type in GROUP_TYPES


def _bot_can_invite(member: object) -> bool:
    status = getattr(member, "status", "")
    return status == "creator" or (
        status == "administrator" and bool(getattr(member, "can_invite_users", False))
    )


async def _source_admin_error(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> str | None:
    """Return an explanation unless the caller is an admin in a managed group."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or not _is_group(chat.type):
        return "Run this command in the source group."

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if getattr(bot_member, "status", "") not in ADMIN_STATUSES:
            return (
                "Please make me an administrator in this source group so I can "
                "verify who is allowed to configure it."
            )
        user_member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        logger.info("Could not verify group administrator permissions")
        return (
            "I couldn't verify permissions. Make sure I am an administrator in "
            "the source group, then try again."
        )

    if getattr(user_member, "status", "") not in ADMIN_STATUSES:
        return "Only a source-group administrator can change this configuration."
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    text = (
        "Hi! I'm an opt-in group migration helper. I can provide a time-limited "
        "link that lets people request to join a destination group. I don't log "
        "into personal accounts, collect Telegram login codes or passwords, "
        "scrape member lists, or add people automatically.\n\n"
        "A group admin can configure a destination with /setdestination in the "
        "source group. Members can then use /invite to request their own invite. "
        "Use /help for setup details."
    )
    if _is_group(chat.type):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Get opt-in invite", callback_data="optin_invite")]]
        )
        await message.reply_text(text, reply_markup=keyboard)
    else:
        await message.reply_text(text)


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is not None and chat is not None:
        await message.reply_text(f"This chat's ID is {chat.id}.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Setup (admins):\n"
        "1. Add this bot as an administrator to both groups. In the destination, "
        "it needs permission to invite users.\n"
        "2. In the source group, run /setdestination followed by the destination "
        "group's @username or numeric chat ID. For a private group, use /chatid "
        "there to see its ID. You must be an administrator in both groups.\n"
        "3. Members use /invite in the source group. The generated link expires "
        "after 7 days and asks the destination admins to approve each join request.\n\n"
        "Admin commands: /status and /cleardestination.\n"
        "This bot only facilitates voluntary join requests; it does not transfer "
        "or enumerate members."
    )


def _clear_invite_cache(context: ContextTypes.DEFAULT_TYPE, source_chat_id: int) -> None:
    cache: dict[tuple[int, int], tuple[str, float]] = context.application.bot_data.get(
        "invite_link_cache", {}
    )
    for key in list(cache):
        if key[0] == source_chat_id:
            cache.pop(key, None)


async def set_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    error = await _source_admin_error(update, context)
    if error:
        await message.reply_text(error)
        return
    if len(context.args) != 1:
        await message.reply_text(
            "Usage: /setdestination @groupusername (or /setdestination -1001234567890)"
        )
        return

    try:
        destination = await context.bot.get_chat(context.args[0])
    except TelegramError:
        logger.info("Could not resolve requested destination group")
        await message.reply_text(
            "I couldn't access that destination. Use its @username or numeric chat "
            "ID, and make sure I have been added there."
        )
        return

    if not _is_group(destination.type):
        await message.reply_text("The destination must be a group or supergroup.")
        return
    if destination.id == chat.id:
        await message.reply_text("The destination must be different from this source group.")
        return

    try:
        destination_bot = await context.bot.get_chat_member(
            destination.id, context.bot.id
        )
        destination_admin = await context.bot.get_chat_member(destination.id, user.id)
    except TelegramError:
        logger.info("Could not verify destination group permissions")
        await message.reply_text(
            "I couldn't verify destination permissions. Add me as an administrator "
            "there with permission to invite users, and make sure you are also an "
            "administrator, then try again."
        )
        return

    if not _bot_can_invite(destination_bot):
        await message.reply_text(
            "I must be an administrator in the destination with permission to "
            "invite users so I can create join-request links."
        )
        return
    if getattr(destination_admin, "status", "") not in ADMIN_STATUSES:
        await message.reply_text(
            "You must also be an administrator in the destination group."
        )
        return

    title = destination.title or destination.username or str(destination.id)
    _store(context).set(chat.id, destination.id, title)
    _clear_invite_cache(context, chat.id)
    await message.reply_text(
        f"Destination set to {title}. Members can now use /invite to request "
        "their own join link."
    )


async def clear_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    error = await _source_admin_error(update, context)
    if error:
        await message.reply_text(error)
        return

    if _store(context).remove(chat.id):
        _clear_invite_cache(context, chat.id)
        await message.reply_text("Destination configuration cleared for this source group.")
    else:
        await message.reply_text("No destination is configured for this source group.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not _is_group(chat.type):
        await message.reply_text("Use /status inside a configured source group.")
        return

    destination = _store(context).get(chat.id)
    if destination is None:
        await message.reply_text(
            "No destination is configured. A group admin can use "
            "/setdestination @groupusername."
        )
        return
    await message.reply_text(
        f"Opt-in destination: {destination.title} ({destination.chat_id}).\n"
        "Use /invite to create a 7-day join-request link."
    )


def _cooldown_remaining(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int
) -> int:
    now = time.monotonic()
    cooldowns: dict[tuple[int, int], float] = context.application.bot_data.setdefault(
        "invite_cooldowns", {}
    )
    # Bound the in-memory map for a long-running bot.
    for key, last_used in list(cooldowns.items()):
        if now - last_used > 3600:
            cooldowns.pop(key, None)
    last_used = cooldowns.get((chat_id, user_id))
    if last_used is None:
        return 0
    return max(0, int(INVITE_COOLDOWN_SECONDS - (now - last_used) + 0.999))


async def _issue_invite(
    chat_id: int,
    chat_type: str,
    user_id: int,
    reply: Reply,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_group(chat_type):
        await reply("Use /invite inside the source group.")
        return

    destination = _store(context).get(chat_id)
    if destination is None:
        await reply("No destination is configured for this source group yet.")
        return

    remaining = _cooldown_remaining(context, chat_id, user_id)
    if remaining:
        await reply(f"Please wait {remaining} seconds before requesting another link.")
        return

    cache: dict[tuple[int, int], tuple[str, float]] = context.application.bot_data.setdefault(
        "invite_link_cache", {}
    )
    cache_key = (chat_id, destination.chat_id)
    now = time.monotonic()
    cached = cache.get(cache_key)
    invite_url = cached[0] if cached and cached[1] - now > 60 else None

    if invite_url is None:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=destination.chat_id,
                name="Opt-in group move",
                expire_date=datetime.now(timezone.utc) + INVITE_TTL,
                creates_join_request=True,
            )
        except TelegramError:
            logger.exception("Could not create an opt-in join-request link")
            await reply(
                "I couldn't create the link. Please ask a destination-group admin to "
                "check that I still have permission to invite users."
            )
            return
        invite_url = invite.invite_link
        cache[cache_key] = (invite_url, now + INVITE_TTL.total_seconds())

    cooldowns = context.application.bot_data["invite_cooldowns"]
    cooldowns[(chat_id, user_id)] = time.monotonic()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Request to join destination", url=invite_url)]]
    )
    await reply(
        f"Tap below to request to join {destination.title}. The link expires in "
        "7 days, and destination admins will review your request.",
        reply_markup=keyboard,
    )


async def invite_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    await _issue_invite(chat.id, chat.type, user.id, message.reply_text, context)


async def invite_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None or not callable(getattr(message, "reply_text", None)):
        await query.answer("Please use /invite in the source group.", show_alert=True)
        return
    await query.answer()
    await _issue_invite(
        message.chat.id,
        message.chat.type,
        query.from_user.id,
        message.reply_text,
        context,
    )


def build_application(token: str, data_path: Path) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["destination_store"] = DestinationStore(data_path)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(CommandHandler("setdestination", set_destination))
    application.add_handler(CommandHandler("cleardestination", clear_destination))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(
        CallbackQueryHandler(invite_button, pattern=r"^optin_invite$")
    )
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set BOT_TOKEN in the environment before starting the bot.")

    data_path = Path(os.getenv("BOT_DATA_PATH", "data/groups.json"))
    application = build_application(token, data_path)
    application.run_polling()


if __name__ == "__main__":
    main()
