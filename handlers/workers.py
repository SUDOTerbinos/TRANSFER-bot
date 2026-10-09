"""Worker status and human-confirmed campaign distribution actions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from models import (
    ActionAlreadyHandled,
    GroupValidationError,
    QuotaExceeded,
    WorkerStatus,
)
from services.invite_service import (
    DistributionUncertainError,
    FloodWaitDistributionError,
    TransientTelegramError,
)
from services.notification_service import format_datetime, format_order_progress

logger = logging.getLogger(__name__)


def _worker_summary(worker: dict) -> str:
    quota_line = (
        f"Actions in rolling window: {worker.get('quota_used', 0)} / "
        f"{worker.get('quota_limit', 50)}\n"
        f"Remaining: {worker.get('quota_remaining', 0)}"
    )
    next_at = worker.get("quota_next_eligible_at") or worker.get("next_eligible_at")
    if next_at and worker.get("quota_remaining", 0) == 0:
        quota_line += f"\nNext eligible: {format_datetime(next_at)}"
    assigned = worker.get("assigned_campaign_id")
    return (
        f"Worker #{worker['id']} — {worker['display_name']}\n"
        f"Status: {worker['status']}\n"
        f"Enabled: {'yes' if worker['enabled'] else 'no'}\n"
        f"Assigned campaign: {assigned if assigned is not None else 'none'}\n"
        f"Last activity: {format_datetime(worker.get('last_activity_at'))}\n"
        f"{quota_line}"
    )


async def worker_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(
            "Please check worker status in a private chat with the bot."
        )
        return
    worker = await context.application.bot_data["worker_manager"].get_for_user(user.id)
    if worker is None:
        await message.reply_text(
            "You are not registered as a worker. A bot administrator can authorize you with /add_worker."
        )
        return
    await message.reply_text(_worker_summary(worker))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await message.reply_text(
            "Please check your status in a private chat with the bot."
        )
        return
    worker = await context.application.bot_data["worker_manager"].get_for_user(user.id)
    if worker is not None:
        await message.reply_text(_worker_summary(worker))
        return
    database = context.application.bot_data["database"]
    orders = await database.list_orders(customer_id=user.id, limit=1)
    if not orders:
        await message.reply_text("You have no orders yet. Use /start to create one.")
        return
    await message.reply_text(format_order_progress(orders[0]))


async def worker_action_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        await query.answer(
            "Please handle worker assignments in a private chat with the bot.",
            show_alert=True,
        )
        return
    data = (query.data or "").split(":")
    if len(data) != 3 or data[0] != "worker":
        await query.answer("This action is invalid.", show_alert=True)
        return
    action = data[1]
    try:
        campaign_id = int(data[2])
    except ValueError:
        await query.answer("This campaign reference is invalid.", show_alert=True)
        return

    services = context.application.bot_data
    worker = await services["worker_manager"].get_for_user(user.id)
    if worker is None:
        await query.answer("You are not a registered worker.", show_alert=True)
        return
    campaign = await services["database"].get_campaign(campaign_id)
    if campaign is None:
        await query.answer("This campaign no longer exists.", show_alert=True)
        return
    if (
        worker["assigned_campaign_id"] != campaign_id
        or worker["status"] != WorkerStatus.PROCESSING.value
    ):
        await query.answer(
            "This campaign is no longer assigned to you.", show_alert=True
        )
        return
    order = await services["database"].get_order(campaign["order_id"])
    if order is None:
        await query.answer("The order could not be loaded.", show_alert=True)
        return

    if action == "review":
        await query.answer()
        reviewed = await services["database"].mark_campaign_for_review(
            int(worker["id"]),
            campaign_id,
            campaign["order_id"],
            "The assigned operator requested administrator review.",
        )
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "The assigned operator requested review; no invitation was distributed.",
            )
            await services["notifications"].administrators(
                f"Order {campaign['order_id']} was paused at the assigned worker's request."
            )
            if query.message:
                await query.edit_message_text(
                    "This campaign is paused for administrator review."
                )
        return

    if action != "distribute":
        await query.answer("Unknown worker action.", show_alert=True)
        return

    expires_at = campaign.get("invite_expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(expires_at)
        except ValueError:
            expires = datetime.min.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            await query.answer(
                "The destination invite link expired; this order needs review.",
                show_alert=True,
            )
            reviewed = await services["database"].mark_campaign_for_review(
                int(worker["id"]),
                campaign_id,
                campaign["order_id"],
                "The destination join-request link expired before distribution.",
            )
            if reviewed:
                await services["notifications"].order_update(
                    reviewed,
                    "The invite link expired before distribution. An administrator must review the order.",
                )
            return

    await query.answer()
    try:
        action_id = await services["quota_manager"].reserve_distribution(
            int(worker["id"]), campaign_id, campaign["order_id"]
        )
    except QuotaExceeded as exc:
        reviewed = await services["database"].mark_campaign_for_review(
            int(worker["id"]),
            campaign_id,
            campaign["order_id"],
            "Quota changed after assignment; administrator review is required.",
        )
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "The worker quota was reached before distribution. The operation is paused for review.",
            )
        if query.message:
            await query.edit_message_text(
                "Quota is unavailable; the order was paused for review."
            )
        logger.info("Worker quota reservation denied (%s)", type(exc).__name__)
        return
    except ActionAlreadyHandled as exc:
        if query.message:
            await query.edit_message_text(
                "This action has already been handled or is no longer available."
            )
        logger.info("Duplicate worker callback ignored (%s)", type(exc).__name__)
        return

    try:
        message_id = await services["invite_service"].distribute_announcement(
            campaign, order, context.bot
        )
    except DistributionUncertainError as exc:
        safe_reason = f"Announcement delivery is uncertain ({type(exc).__name__}); administrator review is required."
        reviewed = await services["database"].mark_distribution_uncertain(
            action_id, int(worker["id"]), campaign_id, campaign["order_id"], safe_reason
        )
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "Delivery could not be verified. The quota is held and the order is paused for administrator review.",
            )
            await services["notifications"].administrators(
                f"Order {campaign['order_id']} has an uncertain Bot API send; its quota was conservatively held."
            )
        if query.message:
            await query.edit_message_text(
                "Delivery is uncertain. This action will not be retried automatically."
            )
        logger.warning(
            "Uncertain Bot API delivery order=%s (%s)",
            campaign["order_id"],
            type(exc).__name__,
        )
        return
    except FloodWaitDistributionError as exc:
        safe_reason = (
            f"Telegram requested a {int(exc.retry_after_seconds)} second cooldown; "
            "administrator review is required before another attempt."
        )
        reviewed = await services["database"].mark_distribution_failed(
            action_id,
            int(worker["id"]),
            campaign_id,
            campaign["order_id"],
            "FloodWait",
            safe_reason,
        )
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "Telegram rate-limited the announcement. The campaign is paused; an administrator must wait for the indicated cooldown and review before any retry.",
            )
            await services["notifications"].administrators(
                f"Order {campaign['order_id']} received a {int(exc.retry_after_seconds)} second Telegram cooldown. No retry was attempted."
            )
        if query.message:
            await query.edit_message_text(
                f"Telegram requested a {int(exc.retry_after_seconds)} second cooldown. No retry was attempted; the order is paused for review."
            )
        return
    except (TransientTelegramError, GroupValidationError) as exc:
        safe_reason = f"Telegram announcement failed ({type(exc).__name__}); administrator review is required."
        reviewed = await services["database"].mark_distribution_failed(
            action_id,
            int(worker["id"]),
            campaign_id,
            campaign["order_id"],
            type(exc).__name__,
            safe_reason,
        )
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "The announcement was not confirmed as delivered; the order is paused for administrator review.",
            )
            await services["notifications"].administrators(
                f"Order {campaign['order_id']} needs review after a Bot API distribution error ({type(exc).__name__})."
            )
        if query.message:
            await query.edit_message_text(
                "Distribution failed or was refused. The order is paused for administrator review."
            )
        return
    except Exception as exc:  # noqa: BLE001 - an unexpected send error has an ambiguous delivery result
        safe_reason = f"Unexpected {type(exc).__name__} during Bot API send; delivery result is uncertain."
        logger.error(
            "Distribution outcome uncertain order=%s (%s)",
            campaign["order_id"],
            type(exc).__name__,
        )
        try:
            reviewed = await services["database"].mark_distribution_uncertain(
                action_id,
                int(worker["id"]),
                campaign_id,
                campaign["order_id"],
                safe_reason,
            )
        except Exception:  # noqa: BLE001 - retain the reserved quota if reconciliation also fails
            reviewed = None
        if reviewed:
            await services["notifications"].order_update(
                reviewed,
                "The delivery result is uncertain. The quota is held and the order requires administrator review.",
            )
            await services["notifications"].administrators(
                f"Order {campaign['order_id']} has an uncertain distribution result; its quota was conservatively held."
            )
        if query.message:
            await query.edit_message_text(
                "Delivery could not be verified. This action will not be retried automatically."
            )
        return

    try:
        updated_order = await services["quota_manager"].complete_distribution(
            action_id,
            int(worker["id"]),
            campaign_id,
            campaign["order_id"],
            message_id,
        )
    except Exception as exc:  # noqa: BLE001 - the API send succeeded but persistence may be uncertain
        logger.error(
            "Could not persist distribution result order=%s (%s)",
            campaign["order_id"],
            type(exc).__name__,
        )
        try:
            updated_order = await services["database"].mark_distribution_uncertain(
                action_id,
                int(worker["id"]),
                campaign_id,
                campaign["order_id"],
                f"Announcement was posted but persistence failed ({type(exc).__name__}).",
            )
        except Exception:  # noqa: BLE001 - restart recovery will reconcile the reserved action
            updated_order = None
        if updated_order:
            await services["notifications"].order_update(
                updated_order,
                "The announcement was posted, but the result needs administrator reconciliation.",
            )
        if query.message:
            await query.edit_message_text(
                "Announcement posted; database reconciliation is required. No retry was attempted."
            )
        return

    if updated_order:
        await services["order_manager"].distribution_completed(updated_order)
    if query.message:
        await query.edit_message_text(
            "The opt-in announcement was posted. This is one distributed invitation, not a confirmed join."
        )
    await services["database"].wake_queued_campaigns()
    scheduler = services.get("task_scheduler")
    if scheduler is not None:
        scheduler.wake()


def register_worker_handlers(application) -> None:
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("worker_status", worker_status_command))
    application.add_handler(
        CallbackQueryHandler(
            worker_action_callback, pattern=r"^worker:(?:distribute|review):\d+$"
        )
    )
