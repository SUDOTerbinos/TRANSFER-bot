"""Order creation, validation, progress, cancellation, and join reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

from database import Database
from models import GroupValidationError, OrderStatus
from services.invite_service import InviteService, TransientTelegramError
from services.notification_service import NotificationService, format_order_progress

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(
        self,
        database: Database,
        invite_service: InviteService,
        notifications: NotificationService,
        bot,
        maximum_order_size: int = 100_000,
        migration_announcement_template: str = "Community migration: {campaign_name}",
    ) -> None:
        self.database = database
        self.invite_service = invite_service
        self.notifications = notifications
        self.bot = bot
        self.maximum_order_size = maximum_order_size
        self.migration_announcement_template = migration_announcement_template
        self._join_hash_key: str | None = None

    async def create_order(
        self,
        *,
        customer_id: int,
        username: str | None,
        display_name: str,
        source_ref: str,
        destination_ref: str,
        campaign_name: str,
        requested_size: int,
        request_key: str,
    ) -> dict[str, Any]:
        if not 1 <= requested_size <= self.maximum_order_size:
            raise ValueError(
                f"Requested size must be between 1 and {self.maximum_order_size}."
            )
        order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
        clean_name = campaign_name.strip()[:80]
        announcement = self.migration_announcement_template.replace(
            "{campaign_name}", clean_name
        ).strip()[:500]
        if not announcement:
            announcement = f"Community migration: {clean_name}"
        order = await self.database.create_order(
            order_id=order_id,
            request_key=request_key,
            customer_id=customer_id,
            username=username,
            display_name=display_name,
            source_ref=source_ref,
            destination_ref=destination_ref,
            campaign_name=campaign_name.strip()[:80],
            requested_size=requested_size,
            announcement=announcement,
        )
        return order

    async def validate_order(self, order_id: str) -> dict[str, Any] | None:
        order = await self.database.get_order(order_id)
        if order is None or order["status"] in {
            OrderStatus.CANCELLED.value,
            OrderStatus.COMPLETED.value,
            OrderStatus.NEEDS_REVIEW.value,
        }:
            return order
        if not await self.database.set_order_validating(order_id):
            return await self.database.get_order(order_id)

        order = await self.database.get_order(order_id)
        if order is None:
            return None
        await self.notifications.order_update(
            order, "Order accepted. Validating group access and permissions."
        )
        try:
            (
                source,
                destination,
                invite_link,
                expires_at,
            ) = await self.invite_service.validate_and_prepare(order, self.bot)
        except TransientTelegramError:
            raise
        except GroupValidationError as exc:
            reviewed = await self.database.mark_order_needs_review(
                order_id, f"Group validation failed: {exc}", task_type="validate_order"
            )
            if reviewed:
                await self.notifications.order_update(
                    reviewed,
                    "Validation could not be completed. Check group access and administrator permissions, then contact an administrator.",
                )
                await self.notifications.administrators(
                    f"Order {order_id} needs review: {exc}"
                )
            return reviewed

        validated = await self.database.save_validation_success(
            order_id, source, destination, invite_link, expires_at
        )
        if validated is None:
            await self.invite_service.revoke_link(
                destination.chat_id, invite_link, self.bot
            )
            return None
        await self.notifications.order_update(
            validated,
            "Group validation succeeded. The order is queued for an authorized operator to review and distribute one opt-in announcement.",
        )
        return validated

    async def queue_delay(
        self, order_id: str, reason: str, next_eligible_at: str | None
    ) -> None:
        order = await self.database.get_order(order_id)
        if order is None:
            return
        next_line = (
            f" Next expected eligibility: {next_eligible_at}."
            if next_eligible_at
            else ""
        )
        await self.notifications.order_update(
            order,
            f"Order remains queued: {reason}{next_line}",
        )

    async def notify_assigned(self, order: dict[str, Any]) -> None:
        await self.notifications.order_update(
            order,
            "Processing has started. An authorized operator is reviewing the opt-in announcement.",
        )

    async def distribution_completed(self, order: dict[str, Any]) -> None:
        await self.notifications.order_update(
            order,
            "The opt-in invitation announcement was posted. This counts as one distributed announcement, not as transferred members. Confirmed joins are tracked separately when Telegram reports them.",
        )

    async def record_chat_member_join(
        self, chat_id: int, invite_link: str | None, telegram_user_id: int
    ) -> None:
        if not invite_link:
            return
        campaign = await self.database.find_campaign_for_invite(chat_id, invite_link)
        if campaign is None:
            return
        if self._join_hash_key is None:
            self._join_hash_key = await self.database.get_or_create_join_hash_key()
        subject_hash = hmac.new(
            self._join_hash_key.encode(),
            f"{campaign['campaign_id']}:{telegram_user_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        order, inserted, completed_now = await self.database.record_confirmed_join(
            int(campaign["campaign_id"]),
            campaign["order_id"],
            subject_hash,
            int(campaign["requested_size"]),
        )
        if not inserted or order is None:
            return
        event = (
            "Completion target verified from Telegram join updates."
            if completed_now
            else "Telegram confirmed a member joined using the campaign link."
        )
        await self.notifications.order_update(order, event)
        if completed_now:
            campaign_details = await self.database.get_campaign(
                int(campaign["campaign_id"])
            )
            if campaign_details and campaign_details.get("destination_group_id"):
                await self.invite_service.revoke_link(
                    int(campaign_details["destination_group_id"]), invite_link, self.bot
                )

    async def cancel_order(
        self, order_id: str, requester_id: int, is_admin: bool = False
    ) -> tuple[dict[str, Any] | None, str]:
        order, result, invite_link = await self.database.cancel_order(
            order_id, requester_id, is_admin
        )
        if result != "cancelled" or order is None:
            return order, result
        destination_id = order.get("resolved_destination_id") or order.get(
            "destination_group_id"
        )
        if destination_id and invite_link:
            await self.invite_service.revoke_link(
                int(destination_id), invite_link, self.bot
            )
        await self.notifications.order_update(
            order, "The order was cancelled by an authorized user."
        )
        await self.database.wake_queued_campaigns()
        return order, result

    async def order_progress(self, order: dict[str, Any]) -> str:
        return format_order_progress(order)
