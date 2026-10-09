"""Persistent async scheduler for validation and human-approved campaigns."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from database import Database, utc_iso, utc_now
from models import OrderStatus
from services.invite_service import TransientTelegramError
from services.notification_service import NotificationService
from services.order_manager import OrderManager
from services.quota_manager import QuotaManager
from services.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(
        self,
        database: Database,
        order_manager: OrderManager,
        worker_manager: WorkerManager,
        quota_manager: QuotaManager,
        notifications: NotificationService,
        bot,
        poll_seconds: float = 3.0,
    ) -> None:
        self.database = database
        self.order_manager = order_manager
        self.worker_manager = worker_manager
        self.quota_manager = quota_manager
        self.notifications = notifications
        self.bot = bot
        self.poll_seconds = max(1.0, poll_seconds)
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="telegram-transfer-scheduler"
            )
        return self._task

    def wake(self) -> None:
        self._wake_event.set()

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:  # noqa: BLE001 - report safely during shutdown
                logger.error(
                    "Scheduler shutdown observed an error (%s)", type(exc).__name__
                )
            self._task = None

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                recovered = await self.database.recover_after_restart()
                await self.database.recover_pending_tasks_for_cancelled_orders()
                for order_id in recovered:
                    order = await self.database.get_order(order_id)
                    if order:
                        await self.notifications.order_update(
                            order,
                            "The result of an operation during restart could not be verified. "
                            "It has been paused for administrator review and will not be retried automatically.",
                        )
                        await self.notifications.administrators(
                            f"Order {order_id} needs review after restart; no worker was switched or retried."
                        )
                await self._redispatch_existing_assignments()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient startup recovery failures
                logger.error("Scheduler recovery failed (%s)", type(exc).__name__)
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self.poll_seconds
                    )
                except TimeoutError:
                    pass

        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.quota_manager.refresh_workers()
                await self.worker_manager.sync_runtime_state()
                task = await self.database.claim_next_task()
                if task is not None:
                    await self._process_task(task)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the persistent scheduler alive
                # Do not log exception strings: Telegram errors can contain links.
                logger.error("Scheduler iteration failed (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self.poll_seconds
                )
            except TimeoutError:
                pass

    async def _process_task(self, task: dict) -> None:
        order_id = task["order_id"]
        job_type = task["job_type"]
        try:
            if job_type == "validate_order":
                await self.order_manager.validate_order(order_id)
            elif job_type == "run_campaign":
                await self._assign_campaign(task)
            else:
                await self.database.mark_order_needs_review(
                    order_id, "Unknown task type.", task_type=job_type
                )
        except TransientTelegramError as exc:
            delay = max(1.0, float(exc.retry_after))
            await self.database.defer_task(
                int(task["id"]),
                utc_iso(utc_now() + timedelta(seconds=delay)),
                "Temporary Bot API condition; next attempt follows Telegram's retry delay.",
            )
            if int(task.get("attempt_count") or 0) <= 1:
                order = await self.database.get_order(order_id)
                if order:
                    await self.notifications.order_update(
                        order,
                        f"Telegram temporarily delayed validation. The saved task will retry after approximately {int(delay)} seconds.",
                    )
        except Exception as exc:  # noqa: BLE001 - persist unexpected failures for administrator review
            safe_reason = (
                f"Unexpected {type(exc).__name__} while processing the saved task."
            )
            logger.error("Task failed order=%s type=%s", order_id, type(exc).__name__)
            order = await self.database.mark_order_needs_review(
                order_id, safe_reason, task_type=job_type
            )
            if order:
                await self.notifications.order_update(
                    order,
                    "The operation has been paused for administrator review. No automatic worker rotation or retry was attempted.",
                )
                await self.notifications.administrators(
                    f"Order {order_id} needs review ({type(exc).__name__})."
                )

    async def _assign_campaign(self, task: dict) -> None:
        quota_limit, window_seconds = await self.quota_manager.policy()
        assignment = await self.database.assign_queued_campaign(
            int(task["id"]), task["order_id"], quota_limit, window_seconds
        )
        if not assignment.assigned:
            if assignment.order is None or assignment.order.get("status") in {
                OrderStatus.CANCELLED.value,
                OrderStatus.COMPLETED.value,
                OrderStatus.NEEDS_REVIEW.value,
            }:
                return
            delay_until = assignment.next_eligible_at
            if delay_until is None:
                delay_until = utc_iso(
                    utc_now() + timedelta(seconds=max(10, self.poll_seconds * 5))
                )
            changed = await self.database.defer_task(
                int(task["id"]),
                delay_until,
                assignment.reason or "No eligible worker is currently available.",
                queue_reason=assignment.reason
                or "No eligible worker is currently available.",
            )
            if changed:
                order = await self.database.get_order(task["order_id"])
                if order:
                    await self.order_manager.queue_delay(
                        task["order_id"],
                        assignment.reason
                        or "No eligible worker is currently available.",
                        assignment.next_eligible_at,
                    )
            return

        worker = assignment.worker
        campaign = assignment.campaign
        order = assignment.order
        if not worker or not campaign or not order:
            await self.database.mark_order_needs_review(
                task["order_id"],
                "The worker assignment could not be persisted consistently.",
                task_type="run_campaign",
            )
            return
        await self.order_manager.notify_assigned(order)
        try:
            await self.notifications.worker_assignment(worker, campaign, order)
        except Exception as exc:  # noqa: BLE001 - notification failure must not rotate workers
            reason = f"Assigned operator could not be notified ({type(exc).__name__})."
            reviewed = await self.database.mark_campaign_for_review(
                int(worker["id"]), int(campaign["id"]), order["order_id"], reason
            )
            if reviewed:
                await self.notifications.order_update(
                    reviewed,
                    "Processing is paused because the assigned operator could not be reached. An administrator must review the worker setup.",
                )
                await self.notifications.administrators(
                    f"Order {order['order_id']} needs review; its assigned worker could not be notified."
                )

    async def _redispatch_existing_assignments(self) -> None:
        """Resend safe-to-repeat operator prompts after restart, to the same worker."""
        assigned = await self.database.list_assigned_tasks()
        for item in assigned:
            worker = await self.database.get_worker(int(item["worker_id"]))
            campaign = await self.database.get_campaign(int(item["campaign_id"]))
            order = await self.database.get_order(item["order_id"])
            if not worker or not campaign or not order:
                continue
            try:
                await self.notifications.worker_assignment(worker, campaign, order)
            except Exception as exc:  # noqa: BLE001 - preserve assignment and require review
                logger.warning(
                    "Could not restore assignment notification for order %s (%s)",
                    item["order_id"],
                    type(exc).__name__,
                )
                reviewed = await self.database.mark_campaign_for_review(
                    int(worker["id"]),
                    int(campaign["id"]),
                    item["order_id"],
                    f"Assigned operator notification failed after restart ({type(exc).__name__}).",
                )
                if reviewed:
                    await self.notifications.order_update(
                        reviewed,
                        "The assigned worker could not be reached after restart. The order is paused for administrator review.",
                    )
