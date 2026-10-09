"""Administrator-only controls, workforce management, quota settings, and logs."""

from __future__ import annotations

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from models import DomainError, WorkerStatus
from services.notification_service import format_datetime

logger = logging.getLogger(__name__)
_TOKEN_PATTERN = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Workers", callback_data="admin:workers"),
                InlineKeyboardButton("Orders", callback_data="admin:orders"),
            ],
            [
                InlineKeyboardButton("Statistics", callback_data="admin:stats"),
                InlineKeyboardButton("Recent errors", callback_data="admin:logs"),
            ],
        ]
    )


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return False
    if chat.type != "private":
        await message.reply_text(
            "Please use administrator controls in a private chat with the bot."
        )
        return False
    if not await context.application.bot_data["database"].is_administrator(user.id):
        await message.reply_text(
            "This command is restricted to configured administrators."
        )
        return False
    return True


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    await update.effective_message.reply_text(
        "Administrator controls\n"
        "/workers — list worker status and quota\n"
        "/add_worker USER_ID DISPLAY_NAME — register an authorized operator\n"
        "/pause_worker ID [reason], /resume_worker ID, /disable_worker ID\n"
        "/orders and /order_status ID — inspect orders\n"
        "/cancel_order ID — cancel an order\n"
        "/stats, /set_quota NUMBER, /set_window_hours HOURS, /logs",
        reply_markup=admin_menu_keyboard(),
    )


async def workers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    workers = await context.application.bot_data["worker_manager"].list_workers()
    if not workers:
        await update.effective_message.reply_text(
            "No workers are registered. Use /add_worker USER_ID DISPLAY_NAME."
        )
        return
    policy_limit, policy_window = await context.application.bot_data[
        "quota_manager"
    ].policy()
    window_hours = policy_window // 3600
    for worker in workers:
        next_time = worker.get("next_eligible_at")
        assigned = worker.get("assigned_campaign_id")
        lines = [
            f"Worker #{worker['id']} — {worker['display_name']}",
            f"Telegram user ID: {worker['telegram_user_id']}",
            f"Status: {worker['status']} | Enabled: {'yes' if worker['enabled'] else 'no'}",
            f"Quota: {worker.get('quota_used', 0)}/{policy_limit} used; "
            + f"{worker.get('quota_remaining', 0)} remaining per {window_hours}h",
            f"Next eligible: {format_datetime(next_time) if next_time else 'now / not applicable'}",
            f"Assigned campaign: {assigned if assigned is not None else 'none'}",
            f"Last activity: {format_datetime(worker.get('last_activity_at'))}",
        ]
        await update.effective_message.reply_text("\n".join(lines))


async def add_worker_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) < 2:
        if message:
            await message.reply_text("Usage: /add_worker TELEGRAM_USER_ID DISPLAY_NAME")
        return
    try:
        telegram_user_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Worker ID must be a numeric Telegram user ID.")
        return
    display_name = " ".join(context.args[1:]).strip()
    try:
        worker = await context.application.bot_data["worker_manager"].register(
            telegram_user_id, display_name
        )
    except DomainError as exc:
        await message.reply_text(str(exc))
        return

    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=(
                f"You have been registered as an authorized campaign operator "
                f"(worker #{worker['id']}). Use /start to receive assignment notifications. "
                "You do not need to share account codes or passwords."
            ),
        )
        dm_note = "A private onboarding message was sent."
    except TelegramError:
        dm_note = "Ask the worker to open a private chat with the bot and send /start before assignment."
    await context.application.bot_data["database"].wake_queued_campaigns()
    scheduler = context.application.bot_data.get("task_scheduler")
    if scheduler:
        scheduler.wake()
    await message.reply_text(
        f"Registered worker #{worker['id']} ({worker['display_name']}). {dm_note}"
    )


async def pause_worker_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or not context.args:
        if message:
            await message.reply_text("Usage: /pause_worker WORKER_ID [reason]")
        return
    try:
        worker_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Worker ID must be an integer.")
        return
    reason = " ".join(context.args[1:]) or "Paused by administrator for review."
    worker, order = await context.application.bot_data["worker_manager"].pause(
        worker_id, reason
    )
    if worker is None:
        await message.reply_text("Worker not found.")
        return
    if worker.get("action_in_flight"):
        await message.reply_text(
            "A Bot API announcement is currently in flight. Wait for its result, then pause the worker."
        )
        return
    if not worker["enabled"]:
        await message.reply_text("That worker is disabled.")
        return
    if worker["status"] == WorkerStatus.NEEDS_ATTENTION.value:
        await message.reply_text(
            "This worker already needs administrator review. Resolve or cancel its assigned order first."
        )
        return
    if order:
        await context.application.bot_data["notifications"].order_update(
            order,
            "This order was paused by an administrator. It remains assigned to the same worker.",
        )
    await message.reply_text(
        f"Worker #{worker_id} paused. No operation was moved to another worker."
    )


async def resume_worker_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) != 1:
        if message:
            await message.reply_text("Usage: /resume_worker WORKER_ID")
        return
    try:
        worker_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Worker ID must be an integer.")
        return
    worker, order = await context.application.bot_data["worker_manager"].resume(
        worker_id
    )
    if worker is None:
        await message.reply_text("Worker not found.")
        return
    if worker["status"] == WorkerStatus.NEEDS_ATTENTION.value:
        await message.reply_text(
            "This worker has an unresolved issue. Review or cancel its assigned order before resuming."
        )
        return
    if not worker["enabled"]:
        await message.reply_text(
            "This worker is disabled. Register it again or use /add_worker after review."
        )
        return
    if order:
        campaign = await context.application.bot_data["database"].get_campaign(
            int(order["campaign_id"])
        )
        if campaign:
            try:
                await context.application.bot_data["notifications"].worker_assignment(
                    worker, campaign, order
                )
            except TelegramError as exc:
                reviewed = await context.application.bot_data[
                    "database"
                ].mark_campaign_for_review(
                    worker_id,
                    int(campaign["id"]),
                    order["order_id"],
                    f"Worker could not be notified after resume ({type(exc).__name__}).",
                )
                if reviewed:
                    await context.application.bot_data["notifications"].order_update(
                        reviewed,
                        "The worker could not be reached after resume; the order needs review.",
                    )
                await message.reply_text(
                    "Worker could not be reached; the order was returned to review."
                )
                return
            await context.application.bot_data["notifications"].order_update(
                order,
                "The administrator resumed this order with its original assigned worker.",
            )
    else:
        await context.application.bot_data["database"].wake_queued_campaigns()
        await context.application.bot_data["quota_manager"].refresh_workers()
        scheduler = context.application.bot_data.get("task_scheduler")
        if scheduler:
            scheduler.wake()
    await message.reply_text(f"Worker #{worker_id} resumed after administrator review.")


async def disable_worker_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) != 1:
        if message:
            await message.reply_text("Usage: /disable_worker WORKER_ID")
        return
    try:
        worker_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Worker ID must be an integer.")
        return
    worker, order = await context.application.bot_data["worker_manager"].disable(
        worker_id, "Disabled by administrator; any assigned order requires review."
    )
    if worker is None:
        await message.reply_text("Worker not found.")
        return
    if worker.get("action_in_flight"):
        await message.reply_text(
            "A Bot API announcement is currently in flight. Wait for its result before disabling this worker."
        )
        return
    if order:
        await context.application.bot_data["notifications"].order_update(
            order,
            "This order needs administrator review because its worker was disabled.",
        )
    await context.application.bot_data["database"].wake_queued_campaigns()
    scheduler = context.application.bot_data.get("task_scheduler")
    if scheduler:
        scheduler.wake()
    await message.reply_text(
        f"Worker #{worker_id} disabled. Its assigned order was not transferred to another worker."
    )


async def set_quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) != 1:
        if message:
            await message.reply_text("Usage: /set_quota NUMBER")
        return
    try:
        limit = int(context.args[0])
        await context.application.bot_data["quota_manager"].set_limit(limit)
    except (ValueError, DomainError) as exc:
        await message.reply_text(
            str(exc) or "Quota must be an integer from 1 to 100000."
        )
        return
    data = context.application.bot_data
    await data["database"].wake_queued_campaigns()
    await data["quota_manager"].refresh_workers()
    scheduler = data.get("task_scheduler")
    if scheduler:
        scheduler.wake()
    await message.reply_text(
        f"Rolling action quota set to {limit} per configured reporting window."
    )


async def set_window_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None or len(context.args) != 1:
        if message:
            await message.reply_text("Usage: /set_window_hours HOURS")
        return
    try:
        hours = int(context.args[0])
        await context.application.bot_data["quota_manager"].set_window_hours(hours)
    except (ValueError, DomainError) as exc:
        await message.reply_text(
            str(exc) or "Window must be an integer from 1 to 8760 hours."
        )
        return
    data = context.application.bot_data
    await data["database"].wake_queued_campaigns()
    await data["quota_manager"].refresh_workers()
    scheduler = data.get("task_scheduler")
    if scheduler:
        scheduler.wake()
    await message.reply_text(f"Quota reporting window set to {hours} hour(s).")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(
            "Please review statistics in a private chat with the bot."
        )
        return
    database = context.application.bot_data["database"]
    is_admin = await database.is_administrator(user.id)
    stats = await database.statistics(None if is_admin else user.id)
    lines = [
        "Campaign statistics",
        f"Orders: {stats['orders_total']}",
        f"Waiting: {stats['orders_pending']}",
        f"In progress: {stats['orders_in_progress']}",
        f"Completed: {stats['orders_completed']}",
        f"Needs review: {stats['orders_needs_review']}",
        f"Invitation links generated: {stats['generated']}",
        f"Announcement distributions: {stats['distributed']}",
        f"Confirmed joins: {stats['joins']}",
        f"Failed/uncertain actions: {stats['failures']}",
    ]
    if is_admin:
        lines.extend(
            [
                f"Enabled workers: {stats['workers_enabled']}",
                f"Active queue tasks: {stats['queue_active']}",
            ]
        )
    await message.reply_text("\n".join(lines))


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    entries = await context.application.bot_data["database"].recent_errors(limit=10)
    if not entries:
        await message.reply_text("No recent worker errors or review events.")
        return
    lines = ["Recent sanitized diagnostics:"]
    for item in entries:
        safe_message = _URL_PATTERN.sub(
            "[link redacted]", item.get("error_message") or ""
        )
        safe_message = _TOKEN_PATTERN.sub("[token redacted]", safe_message)
        lines.append(
            f"#{item['id']} order {item['order_id']} worker #{item['worker_id']} "
            f"({item['display_name']}) — {item['error_code'] or item['status']}: "
            f"{safe_message[:240]}"
        )
    await message.reply_text("\n".join(lines)[:4000])


def register_admin_handlers(application) -> None:
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("workers", workers_command))
    application.add_handler(CommandHandler("add_worker", add_worker_command))
    application.add_handler(CommandHandler("pause_worker", pause_worker_command))
    application.add_handler(CommandHandler("resume_worker", resume_worker_command))
    application.add_handler(CommandHandler("disable_worker", disable_worker_command))
    application.add_handler(CommandHandler("set_quota", set_quota_command))
    application.add_handler(CommandHandler("set_window_hours", set_window_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("logs", logs_command))
