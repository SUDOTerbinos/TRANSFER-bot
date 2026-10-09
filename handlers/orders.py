"""Customer order conversation and order lookup/cancellation commands."""

from __future__ import annotations

import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from models import GroupValidationError, OrderStatus
from services.invite_service import normalize_group_reference
from services.notification_service import format_order_progress

logger = logging.getLogger(__name__)
SOURCE, DESTINATION, CAMPAIGN_NAME, REQUESTED_SIZE, SUMMARY = range(5)
DRAFT_KEY = "transfer_order_draft"

_STAGE_NAMES = {
    SOURCE: "source",
    DESTINATION: "destination",
    CAMPAIGN_NAME: "name",
    REQUESTED_SIZE: "size",
    SUMMARY: "summary",
}
_STAGE_FROM_NAME = {value: key for key, value in _STAGE_NAMES.items()}
_PROMPTS = {
    SOURCE: "Step 1 of 4 — send the source group's public @username, t.me link, or numeric chat ID.",
    DESTINATION: "Step 2 of 4 — send the destination group's public @username, t.me link, or numeric chat ID.",
    CAMPAIGN_NAME: "Step 3 of 4 — enter a short campaign name (2–80 characters).",
    REQUESTED_SIZE: "Step 4 of 4 — enter the target number of confirmed joins (a positive whole number).",
    SUMMARY: "Review the order details below. Nothing is queued until you confirm.",
}


def _navigation_keyboard(state: int, retry: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if retry:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔁 Retry this step",
                    callback_data=f"order:retry:{_STAGE_NAMES[state]}",
                )
            ]
        )
    if state != SOURCE:
        back_target = {
            DESTINATION: "source",
            CAMPAIGN_NAME: "destination",
            REQUESTED_SIZE: "name",
            SUMMARY: "size",
        }[state]
        rows.append(
            [InlineKeyboardButton("⬅️ Back", callback_data=f"order:back:{back_target}")]
        )
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="order:cancel:draft")])
    return InlineKeyboardMarkup(rows)


def _summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm and queue", callback_data="order:confirm"
                )
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="order:back:size")],
            [InlineKeyboardButton("✖️ Cancel", callback_data="order:cancel:draft")],
        ]
    )


async def _reply_callback(
    query, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    if query.message is None:
        return
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except TelegramError:
        await query.message.reply_text(text, reply_markup=markup)


async def _ask_stage(query, state: int, retry: bool = False) -> None:
    await _reply_callback(
        query,
        _PROMPTS[state],
        _navigation_keyboard(state, retry=retry),
    )


async def order_flow_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if update.effective_chat is None or update.effective_chat.type != "private":
        await _reply_callback(
            query, "Please create transfer orders in a private chat with the bot."
        )
        return ConversationHandler.END
    context.user_data[DRAFT_KEY] = {"request_key": uuid.uuid4().hex}
    await _ask_stage(query, SOURCE)
    return SOURCE


async def receive_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message is None or not message.text:
        return SOURCE
    try:
        source_ref = normalize_group_reference(message.text)
    except GroupValidationError as exc:
        await message.reply_text(
            f"I couldn't use that source reference: {exc}",
            reply_markup=_navigation_keyboard(SOURCE, retry=True),
        )
        return SOURCE
    draft = context.user_data.get(DRAFT_KEY, {})
    draft.update({"source_ref": source_ref})
    context.user_data[DRAFT_KEY] = draft
    await message.reply_text(
        _PROMPTS[DESTINATION], reply_markup=_navigation_keyboard(DESTINATION)
    )
    return DESTINATION


async def receive_destination(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if message is None or not message.text:
        return DESTINATION
    try:
        destination_ref = normalize_group_reference(message.text)
    except GroupValidationError as exc:
        await message.reply_text(
            f"I couldn't use that destination reference: {exc}",
            reply_markup=_navigation_keyboard(DESTINATION, retry=True),
        )
        return DESTINATION
    draft = context.user_data.get(DRAFT_KEY, {})
    if destination_ref.casefold() == str(draft.get("source_ref", "")).casefold():
        await message.reply_text(
            "Source and destination must be different groups. Enter a different destination.",
            reply_markup=_navigation_keyboard(DESTINATION, retry=True),
        )
        return DESTINATION
    draft.update({"destination_ref": destination_ref})
    context.user_data[DRAFT_KEY] = draft
    await message.reply_text(
        _PROMPTS[CAMPAIGN_NAME], reply_markup=_navigation_keyboard(CAMPAIGN_NAME)
    )
    return CAMPAIGN_NAME


async def receive_campaign_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if message is None or not message.text:
        return CAMPAIGN_NAME
    name = " ".join(message.text.split()).strip()
    if not 2 <= len(name) <= 80 or any(ord(char) < 32 for char in name):
        await message.reply_text(
            "Use a campaign name of 2–80 printable characters.",
            reply_markup=_navigation_keyboard(CAMPAIGN_NAME, retry=True),
        )
        return CAMPAIGN_NAME
    draft = context.user_data.get(DRAFT_KEY, {})
    draft["campaign_name"] = name
    context.user_data[DRAFT_KEY] = draft
    await message.reply_text(
        _PROMPTS[REQUESTED_SIZE], reply_markup=_navigation_keyboard(REQUESTED_SIZE)
    )
    return REQUESTED_SIZE


async def receive_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message is None or not message.text:
        return REQUESTED_SIZE
    try:
        size = int(message.text.strip())
    except ValueError:
        size = 0
    maximum = context.application.bot_data["config"].maximum_order_size
    if not 1 <= size <= maximum:
        await message.reply_text(
            f"Enter a whole number from 1 to {maximum:,}.",
            reply_markup=_navigation_keyboard(REQUESTED_SIZE, retry=True),
        )
        return REQUESTED_SIZE
    draft = context.user_data.get(DRAFT_KEY, {})
    draft["requested_size"] = size
    context.user_data[DRAFT_KEY] = draft
    summary = (
        f"{_PROMPTS[SUMMARY]}\n\n"
        f"Source: {draft['source_ref']}\n"
        f"Destination: {draft['destination_ref']}\n"
        f"Campaign: {draft['campaign_name']}\n"
        f"Target: {size:,} confirmed joins\n\n"
        "On confirmation, the bot verifies administrator access, generates a "
        "join-request link, and queues one announcement for an authorized operator. "
        "It does not scrape members or add anyone; joins are voluntary and approvals "
        "are controlled by destination admins."
    )
    await message.reply_text(summary, reply_markup=_summary_keyboard())
    return SUMMARY


async def order_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    data = query.data or ""
    if data.startswith("order:cancel:"):
        context.user_data.pop(DRAFT_KEY, None)
        await _reply_callback(query, "Order draft cancelled.")
        return ConversationHandler.END

    if data.startswith("order:retry:"):
        field = data.rsplit(":", 1)[-1]
        state = _STAGE_FROM_NAME.get(field)
        if state is None:
            await _reply_callback(
                query, "That step has expired. Start again with /start."
            )
            return ConversationHandler.END
        await _ask_stage(query, state)
        return state

    if data.startswith("order:back:"):
        field = data.rsplit(":", 1)[-1]
        state = _STAGE_FROM_NAME.get(field)
        if state is None:
            return ConversationHandler.END
        draft = context.user_data.get(DRAFT_KEY, {})
        keep = {
            SOURCE: set(),
            DESTINATION: {"source_ref"},
            CAMPAIGN_NAME: {"source_ref", "destination_ref"},
            REQUESTED_SIZE: {"source_ref", "destination_ref", "campaign_name"},
            SUMMARY: set(draft),
        }[state]
        for key in ("source_ref", "destination_ref", "campaign_name", "requested_size"):
            if key not in keep:
                draft.pop(key, None)
        context.user_data[DRAFT_KEY] = draft
        await _ask_stage(query, state)
        return state

    await _reply_callback(
        query, "This order action has expired. Use /start to begin again."
    )
    return ConversationHandler.END


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    await query.answer()
    draft = context.user_data.get(DRAFT_KEY)
    if not draft or not all(
        key in draft
        for key in (
            "request_key",
            "source_ref",
            "destination_ref",
            "campaign_name",
            "requested_size",
        )
    ):
        context.user_data.pop(DRAFT_KEY, None)
        await _reply_callback(
            query, "The order draft expired. Please start again with /start."
        )
        return ConversationHandler.END

    display_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    try:
        order = await context.application.bot_data["order_manager"].create_order(
            customer_id=user.id,
            username=user.username,
            display_name=display_name,
            source_ref=draft["source_ref"],
            destination_ref=draft["destination_ref"],
            campaign_name=draft["campaign_name"],
            requested_size=int(draft["requested_size"]),
            request_key=draft["request_key"],
        )
    except Exception as exc:  # noqa: BLE001 - preserve draft and offer retry on storage failures
        logger.error("Order creation failed (%s)", type(exc).__name__)
        await _reply_callback(
            query,
            "I couldn't save the order. Your draft is still available; retry or cancel.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Retry", callback_data="order:confirm")],
                    [
                        InlineKeyboardButton(
                            "✖️ Cancel", callback_data="order:cancel:draft"
                        )
                    ],
                ]
            ),
        )
        return SUMMARY

    context.user_data.pop(DRAFT_KEY, None)
    scheduler = context.application.bot_data.get("task_scheduler")
    if scheduler is not None:
        scheduler.wake()
    text = (
        f"Order accepted: {order['order_id']}\n"
        f"Status: {order['status']}\n"
        "The saved queue will validate the groups. You will be notified of the result."
    )
    await _reply_callback(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "View order", callback_data=f"order:view:{order['order_id']}"
                    )
                ]
            ]
        ),
    )
    return ConversationHandler.END


async def cancel_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    context.user_data.pop(DRAFT_KEY, None)
    if message:
        await message.reply_text("Order draft cancelled.")
    return ConversationHandler.END


def build_order_conversation() -> ConversationHandler:
    fallbacks = [CommandHandler("cancel", cancel_conversation)]
    navigation = CallbackQueryHandler(
        order_navigation, pattern=r"^order:(?:back|cancel|retry):"
    )
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(order_flow_start, pattern=r"^menu:create$")],
        states={
            SOURCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_source),
                navigation,
            ],
            DESTINATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_destination),
                navigation,
            ],
            CAMPAIGN_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_campaign_name),
                navigation,
            ],
            REQUESTED_SIZE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_size),
                navigation,
            ],
            SUMMARY: [
                CallbackQueryHandler(confirm_order, pattern=r"^order:confirm$"),
                navigation,
            ],
        },
        fallbacks=fallbacks,
        name="order_creation",
        persistent=False,
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )


def _order_button(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("View order", callback_data=f"order:view:{order_id}")]]
    )


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(
            "Please manage order history in a private chat with the bot."
        )
        return
    is_admin = await context.application.bot_data["database"].is_administrator(user.id)
    is_my_orders_menu = bool(
        update.callback_query and update.callback_query.data == "menu:orders"
    )
    orders = await context.application.bot_data["database"].list_orders(
        None if is_admin and not is_my_orders_menu else user.id, limit=10
    )
    if not orders:
        await message.reply_text("No transfer orders found.")
        return
    for order in orders:
        await message.reply_text(
            f"{order['order_id']} — {order['status']} — {order['campaign_name']}\n"
            f"Confirmed joins: {order['confirmed_joins']} / {order['requested_size']}",
            reply_markup=_order_button(order["order_id"]),
        )


async def order_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(
            "Please check order status in a private chat with the bot."
        )
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /order_status ORDER_ID")
        return
    database = context.application.bot_data["database"]
    order = await database.get_order(context.args[0].upper())
    if order is None:
        await message.reply_text("Order not found.")
        return
    is_admin = await database.is_administrator(user.id)
    if not is_admin and order["customer_id"] != user.id:
        await message.reply_text("You can only view your own orders.")
        return
    await message.reply_text(format_order_progress(order))


async def cancel_order_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text("Please manage orders in a private chat with the bot.")
        return
    if len(context.args) != 1:
        await message.reply_text("Usage: /cancel_order ORDER_ID")
        return
    manager = context.application.bot_data["order_manager"]
    database = context.application.bot_data["database"]
    is_admin = await database.is_administrator(user.id)
    _order, result = await manager.cancel_order(
        context.args[0].upper(), user.id, is_admin
    )
    messages = {
        "not_found": "Order not found.",
        "forbidden": "You can only cancel your own orders.",
        "already_final": "That order is already completed or cancelled.",
        "in_flight": "An announcement is currently being sent. Wait for its result before cancelling.",
        "cancelled": f"Order {context.args[0].upper()} cancelled. Any saved invite link was revoked where possible.",
    }
    await message.reply_text(messages.get(result, "The order could not be cancelled."))
    scheduler = context.application.bot_data.get("task_scheduler")
    if result == "cancelled" and scheduler is not None:
        scheduler.wake()


async def order_view_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer(
            "Please view order details in a private chat with the bot.", show_alert=True
        )
        return
    await query.answer()
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return
    order = await context.application.bot_data["database"].get_order(parts[2])
    if order is None:
        await query.edit_message_text("Order not found.")
        return
    database = context.application.bot_data["database"]
    is_admin = await database.is_administrator(user.id)
    if not is_admin and order["customer_id"] != user.id:
        await query.edit_message_text("You can only view your own orders.")
        return
    keyboard = None
    if order["status"] not in {
        OrderStatus.COMPLETED.value,
        OrderStatus.CANCELLED.value,
    }:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Cancel this order",
                        callback_data=f"order:cancel_order:{order['order_id']}",
                    )
                ]
            ]
        )
    await query.edit_message_text(format_order_progress(order), reply_markup=keyboard)


async def cancel_order_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer(
            "Please manage orders in a private chat with the bot.", show_alert=True
        )
        return
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("This order action is invalid.", show_alert=True)
        return
    database = context.application.bot_data["database"]
    is_admin = await database.is_administrator(user.id)
    order, result = await context.application.bot_data["order_manager"].cancel_order(
        parts[2], user.id, is_admin
    )
    if result == "cancelled" and order:
        await query.answer("Order cancelled.")
        await query.edit_message_text(format_order_progress(order))
        scheduler = context.application.bot_data.get("task_scheduler")
        if scheduler:
            scheduler.wake()
    elif result == "in_flight":
        await query.answer(
            "An announcement is being sent. Try again after it finishes.",
            show_alert=True,
        )
    elif result == "forbidden":
        await query.answer(
            "You cannot cancel another customer's order.", show_alert=True
        )
    else:
        await query.answer(
            "That order is already final or could not be found.", show_alert=True
        )
        await query.edit_message_text(
            "That order is already final or could not be found."
        )


def register_order_handlers(application) -> None:
    application.add_handler(build_order_conversation())
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("order_status", order_status_command))
    application.add_handler(CommandHandler("cancel_order", cancel_order_command))
    application.add_handler(
        CallbackQueryHandler(order_view_callback, pattern=r"^order:view:[A-Z0-9-]+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cancel_order_callback, pattern=r"^order:cancel_order:[A-Z0-9-]+$"
        )
    )
