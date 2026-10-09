"""Customer, operator, and administrator notifications."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from database import Database

logger = logging.getLogger(__name__)


def _short(value: str | None, limit: int = 100) -> str:
    return (value or "").replace("\n", " ").strip()[:limit]


class NotificationService:
    def __init__(self, bot, database: Database, admin_ids: tuple[int, ...]) -> None:
        self.bot = bot
        self.database = database
        self.admin_ids = admin_ids

    async def customer(self, order: dict[str, Any], text: str) -> bool:
        customer_id = int(order["customer_id"])
        settings = await self.database.get_customer(customer_id)
        if settings is not None and not settings["notifications_enabled"]:
            return False
        try:
            await self.bot.send_message(chat_id=customer_id, text=text[:4000])
            return True
        except TelegramError as exc:
            logger.warning(
                "Could not notify customer for order %s (%s)",
                order.get("order_id", "unknown"),
                type(exc).__name__,
            )
            return False

    async def worker_assignment(
        self, worker: dict[str, Any], campaign: dict[str, Any], order: dict[str, Any]
    ) -> None:
        campaign_id = int(campaign["id"])
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Distribute approved announcement",
                        callback_data=f"worker:distribute:{campaign_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Request administrator review",
                        callback_data=f"worker:review:{campaign_id}",
                    )
                ],
            ]
        )
        source_title = order.get("source_title") or order["source_ref"]
        destination_title = order.get("destination_title") or order["destination_ref"]
        text = (
            f"Campaign assigned to you (worker #{worker['id']}).\n"
            f"Order: {order['order_id']}\n"
            f"Campaign: {_short(campaign['campaign_name'], 80)}\n"
            f"Source: {_short(source_title)}\n"
            f"Destination: {_short(destination_title)}\n"
            f"Requested joins: {campaign['requested_size']}\n\n"
            "The customer is an administrator in both groups and confirmed this order. "
            "Review the details, then choose whether to post the opt-in announcement. "
            "This posts one join-request link; it does not add anyone automatically."
        )
        await self.bot.send_message(
            chat_id=int(worker["telegram_user_id"]), text=text, reply_markup=keyboard
        )

    async def administrators(self, text: str) -> None:
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(chat_id=admin_id, text=text[:4000])
            except TelegramError as exc:
                logger.warning(
                    "Could not notify an administrator (%s)", type(exc).__name__
                )

    async def order_update(self, order: dict[str, Any], event: str) -> None:
        await self.customer(order, format_order_progress(order, event))


def format_order_progress(order: dict[str, Any], event: str | None = None) -> str:
    source = order.get("source_title") or order.get("source_ref") or "Not validated"
    destination = (
        order.get("destination_title")
        or order.get("destination_ref")
        or "Not validated"
    )
    updated = order.get("last_updated") or "unknown"
    lines = [
        f"Order {order['order_id']}",
        f"Status: {order['status']}",
        f"Campaign: {_short(order.get('campaign_name'), 80)}",
        f"Source: {_short(source)}",
        f"Destination: {_short(destination)}",
        f"Invitations generated: {order.get('invitations_generated', 0)}",
        f"Announcements distributed: {order.get('invitations_distributed', 0)}",
        f"Confirmed joins: {order.get('confirmed_joins', 0)} / {order.get('requested_size', 0)}",
        f"Failed actions: {order.get('failed_actions', 0)}",
        f"Last update: {updated}",
    ]
    if order.get("queue_reason"):
        lines.append(f"Queue delay: {_short(order['queue_reason'], 250)}")
    if order.get("status_reason"):
        lines.append(f"Review note: {_short(order['status_reason'], 300)}")
    if event:
        lines.insert(0, event)
    return "\n".join(lines)


def format_datetime(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return value
