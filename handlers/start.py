"""Start screen, main menu, and user help."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛒 Create Transfer Order", callback_data="menu:create"
                )
            ],
            [
                InlineKeyboardButton("📦 My Orders", callback_data="menu:orders"),
                InlineKeyboardButton(
                    "👷 Worker Status", callback_data="menu:worker_status"
                ),
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data="menu:stats"),
                InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return
    if chat.type != "private":
        await message.reply_text(
            "Please open a private chat with me to manage transfer orders."
        )
        return

    app_data = context.application.bot_data
    display_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    await app_data["database"].upsert_customer(user.id, user.username, display_name)
    worker = await app_data["worker_manager"].activate(user.id)
    text = (
        f"Welcome, {user.first_name or 'there'}!\n\n"
        "This bot helps an authorized group admin announce an optional destination "
        "join-request link. It never reads source member lists or adds people automatically.\n\n"
        "Create an order only for groups you administer. A registered human operator "
        "must approve the single announcement before it is posted. Join requests are "
        "reviewed by destination admins."
    )
    if worker is not None:
        text += (
            f"\n\nYou are registered as worker #{worker['id']} ({worker['status']})."
        )
    await message.reply_text(text, reply_markup=main_menu_keyboard())


async def my_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text("Please use /myid in a private chat with the bot.")
        return
    await message.reply_text(f"Your Telegram user ID is {user.id}.")


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat is not None and message is not None:
        await message.reply_text(f"This chat's numeric Telegram ID is {chat.id}.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "How it works:\n"
        "• A customer who is an administrator in both groups submits a transfer order.\n"
        "• The bot verifies the groups and creates a time-limited destination join-request link.\n"
        "• An explicitly registered human operator reviews and approves one announcement in the source group.\n"
        "• Members choose whether to request access; destination admins decide whether to approve.\n\n"
        "Commands: /start, /myid, /chatid, /orders, /status, /cancel, /cancel_order ID.\n"
        "Admins: /admin, /workers, /add_worker, /stats, /set_quota, /set_window_hours, /logs.\n\n"
        "The Bot API does not expose arbitrary group participant lists. Invitations distributed and confirmed joins are tracked separately."
    )
