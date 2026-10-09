"""Telegram community migration bot using only the official Bot API."""

from __future__ import annotations

import logging

from telegram import ChatMemberUpdated, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

from config import Settings
from database import Database
from handlers.admin import register_admin_handlers
from handlers.callbacks import register_callback_handlers
from handlers.orders import cancel_conversation, register_order_handlers
from handlers.start import chat_id_command, help_command, my_id_command, start_command
from handlers.workers import register_worker_handlers
from services.invite_service import InviteService
from services.notification_service import NotificationService
from services.order_manager import OrderManager
from services.quota_manager import QuotaManager
from services.task_scheduler import TaskScheduler
from services.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    config: Settings = application.bot_data["config"]
    database = Database(config.database_path)
    await database.open()
    await database.seed_administrators(config.admin_ids)
    await database.seed_setting("action_quota", str(config.default_action_quota))
    await database.seed_setting("quota_window_hours", str(config.quota_window_hours))

    quota_manager = QuotaManager(
        database, config.default_action_quota, config.quota_window_hours
    )
    worker_manager = WorkerManager(database, quota_manager)
    invite_service = InviteService(config.invite_expiry_days)
    notifications = NotificationService(application.bot, database, config.admin_ids)
    order_manager = OrderManager(
        database,
        invite_service,
        notifications,
        application.bot,
        config.maximum_order_size,
        config.migration_announcement_template,
    )
    scheduler = TaskScheduler(
        database,
        order_manager,
        worker_manager,
        quota_manager,
        notifications,
        application.bot,
        config.scheduler_poll_seconds,
    )

    application.bot_data.update(
        {
            "database": database,
            "quota_manager": quota_manager,
            "worker_manager": worker_manager,
            "invite_service": invite_service,
            "notifications": notifications,
            "order_manager": order_manager,
            "task_scheduler": scheduler,
        }
    )
    scheduler.start()
    logger.info("Telegram migration bot initialized; secrets are not logged")


async def _post_stop(application: Application) -> None:
    scheduler = application.bot_data.get("task_scheduler")
    if scheduler is not None:
        await scheduler.stop()


async def _post_shutdown(application: Application) -> None:
    database = application.bot_data.get("database")
    if database is not None:
        await database.close()


async def _handle_chat_member(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    member_update: ChatMemberUpdated | None = update.chat_member
    if member_update is None:
        return
    old_member = member_update.old_chat_member
    new_member = member_update.new_chat_member
    old_is_member = getattr(old_member, "status", "") in {
        "member",
        "administrator",
        "creator",
    } or bool(getattr(old_member, "is_member", False))
    new_is_member = getattr(new_member, "status", "") in {
        "member",
        "administrator",
        "creator",
    } or bool(getattr(new_member, "is_member", False))
    if old_is_member or not new_is_member:
        return
    invite = member_update.invite_link
    if invite is None:
        return
    try:
        await context.application.bot_data["order_manager"].record_chat_member_join(
            member_update.chat.id,
            invite.invite_link,
            new_member.user.id,
        )
    except Exception as exc:  # noqa: BLE001 - sanitize and isolate all join-update failures
        # Do not log raw Telegram objects, invite URLs, usernames, or error text.
        logger.error("Join update processing failed (%s)", type(exc).__name__)


async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error(
        "Unhandled Telegram update error (%s)",
        type(error).__name__ if error else "unknown",
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong while processing that request. No credentials were stored; please try again or contact an administrator."
            )
        except TelegramError as exc:
            logger.debug(
                "Could not send the generic error reply (%s)", type(exc).__name__
            )


def build_application(config: Settings) -> Application:
    application = (
        Application.builder()
        .token(config.bot_token)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["config"] = config

    # The order conversation is registered before the general menu callback,
    # so its create-order button enters the correct state machine.
    register_order_handlers(application)
    register_worker_handlers(application)
    register_admin_handlers(application)
    register_callback_handlers(application)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", my_id_command))
    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(CommandHandler("cancel", cancel_conversation))
    application.add_handler(
        ChatMemberHandler(_handle_chat_member, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_error_handler(_handle_error)
    return application


def main() -> None:
    try:
        config = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=config.log_level,
    )
    if not config.admin_ids:
        logger.warning("ADMIN_IDS is empty; all administrator commands are disabled")
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
