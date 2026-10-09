"""Main-menu and settings callback routes."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from handlers.admin import logs_command, stats_command, workers_command
from handlers.orders import orders_command
from handlers.start import help_command
from handlers.workers import worker_status_command


async def _show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = update.callback_query
    if user is None or query is None:
        return
    database = context.application.bot_data["database"]
    settings = await database.get_customer(user.id)
    enabled = True if settings is None else bool(settings["notifications_enabled"])
    button = (
        "Turn off progress notifications"
        if enabled
        else "Turn on progress notifications"
    )
    text = (
        "Settings\n"
        f"Order progress notifications: {'on' if enabled else 'off'}.\n"
        "This controls routine order updates. Messages sent directly to configured bot administrators are unaffected."
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(button, callback_data="settings:notifications")]]
        ),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer(
            "Please use the main menu in a private chat with the bot.", show_alert=True
        )
        return
    await query.answer()
    data = query.data or ""
    if data == "menu:orders":
        await orders_command(update, context)
    elif data == "menu:worker_status":
        await worker_status_command(update, context)
    elif data == "menu:stats":
        await stats_command(update, context)
    elif data == "menu:settings":
        await _show_settings(update, context)
    elif data == "menu:help":
        await help_command(update, context)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer(
            "Please manage your settings in a private chat with the bot.",
            show_alert=True,
        )
        return
    await query.answer()
    database = context.application.bot_data["database"]
    existing = await database.get_customer(user.id)
    currently_enabled = (
        True if existing is None else bool(existing["notifications_enabled"])
    )
    await database.set_notifications(user.id, not currently_enabled)
    await _show_settings(update, context)


async def admin_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return
    await query.answer()
    if chat.type != "private":
        await query.edit_message_text(
            "Please use administrator controls in a private chat with the bot."
        )
        return
    if not await context.application.bot_data["database"].is_administrator(user.id):
        await query.edit_message_text("This administrator menu is restricted.")
        return
    data = query.data or ""
    if data == "admin:workers":
        await workers_command(update, context)
    elif data == "admin:orders":
        await orders_command(update, context)
    elif data == "admin:stats":
        await stats_command(update, context)
    elif data == "admin:logs":
        await logs_command(update, context)


def register_callback_handlers(application) -> None:
    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^menu:(?:orders|worker_status|stats|settings|help)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(settings_callback, pattern=r"^settings:notifications$")
    )
    application.add_handler(
        CallbackQueryHandler(
            admin_menu_callback, pattern=r"^admin:(?:workers|orders|stats|logs)$"
        )
    )
