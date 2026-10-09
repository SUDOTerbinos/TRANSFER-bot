import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import Database
from models import GroupInfo, OrderStatus, TaskStatus, WorkerStatus
from services.quota_manager import QuotaManager
from services.task_scheduler import TaskScheduler
from services.worker_manager import WorkerManager


class FakeBot:
    async def send_message(self, **kwargs):
        return SimpleNamespace(message_id=1)


class RecordingNotifications:
    def __init__(self):
        self.assignments = []
        self.order_updates = []
        self.admin_updates = []

    async def worker_assignment(self, worker, campaign, order):
        self.assignments.append((worker["id"], campaign["id"], order["order_id"]))

    async def order_update(self, order, event):
        self.order_updates.append((order["order_id"], event))

    async def administrators(self, text):
        self.admin_updates.append(text)


class RecordingOrderManager:
    def __init__(self):
        self.assigned_orders = []
        self.queue_delays = []

    async def notify_assigned(self, order):
        self.assigned_orders.append(order["order_id"])

    async def queue_delay(self, order_id, reason, next_eligible_at):
        self.queue_delays.append((order_id, reason, next_eligible_at))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "scheduler.sqlite3"
        self.database = Database(self.path)
        await self.database.open()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def _add_validated_order(
        self, order_id="ORD-SCHEDULER1", request_key="schedule-1"
    ):
        await self.database.create_order(
            order_id=order_id,
            request_key=request_key,
            customer_id=9300,
            username=None,
            display_name="Community Owner",
            source_ref="@source",
            destination_ref="@destination",
            campaign_name="Migration",
            requested_size=3,
            announcement="Migration announcement",
        )
        await self.database.claim_next_task()
        await self.database.save_validation_success(
            order_id,
            GroupInfo(-10030001, "Source", "source", "supergroup"),
            GroupInfo(-10030002, "Destination", "destination", "supergroup"),
            f"https://t.me/+link-{order_id.lower()}",
            "2099-01-01T00:00:00+00:00",
        )
        return await self.database.claim_next_task()

    async def test_worker_becomes_active_when_it_opens_the_bot(self) -> None:
        worker = await self.database.register_worker(9502, "Returning Operator")
        worker_manager = WorkerManager(
            self.database, QuotaManager(self.database, 50, 24)
        )
        activated = await worker_manager.activate(9502)
        self.assertEqual(activated["id"], worker["id"])
        self.assertEqual(activated["status"], WorkerStatus.ACTIVE.value)
        self.assertIsNotNone(activated["last_activity_at"])

    async def test_scheduler_assigns_and_notifies_an_authorized_worker(self) -> None:
        task = await self._add_validated_order()
        await self.database.register_worker(9500, "Operator")
        quota_manager = QuotaManager(self.database, 50, 24)
        worker_manager = WorkerManager(self.database, quota_manager)
        notifications = RecordingNotifications()
        order_manager = RecordingOrderManager()
        scheduler = TaskScheduler(
            self.database,
            order_manager,
            worker_manager,
            quota_manager,
            notifications,
            FakeBot(),
            poll_seconds=1,
        )

        await scheduler._process_task(task)
        order = await self.database.get_order("ORD-SCHEDULER1")
        workers = await self.database.list_workers(50, 86400)
        queue_task = await self.database.get_task("run_campaign", "ORD-SCHEDULER1")
        self.assertEqual(order["status"], OrderStatus.IN_PROGRESS.value)
        self.assertEqual(workers[0]["status"], WorkerStatus.PROCESSING.value)
        self.assertEqual(queue_task["status"], TaskStatus.ASSIGNED.value)
        self.assertEqual(notifications.assignments[0][2], "ORD-SCHEDULER1")

    async def test_order_stays_queued_without_workers_then_resumes(self) -> None:
        task = await self._add_validated_order()
        quota_manager = QuotaManager(self.database, 50, 24)
        worker_manager = WorkerManager(self.database, quota_manager)
        notifications = RecordingNotifications()
        order_manager = RecordingOrderManager()
        scheduler = TaskScheduler(
            self.database,
            order_manager,
            worker_manager,
            quota_manager,
            notifications,
            FakeBot(),
            poll_seconds=1,
        )

        await scheduler._process_task(task)
        order = await self.database.get_order("ORD-SCHEDULER1")
        self.assertEqual(order["status"], OrderStatus.QUEUED.value)
        self.assertIn("No enabled workers", order["queue_reason"])

        await self.database.register_worker(9501, "New Operator")
        await self.database.wake_queued_campaigns()
        resumed_task = await self.database.claim_next_task()
        await scheduler._process_task(resumed_task)
        order = await self.database.get_order("ORD-SCHEDULER1")
        self.assertEqual(order["status"], OrderStatus.IN_PROGRESS.value)
        self.assertIsNone(order["queue_reason"])

    async def test_scheduler_shuts_down_gracefully(self) -> None:
        quota_manager = QuotaManager(self.database, 50, 24)
        worker_manager = WorkerManager(self.database, quota_manager)
        scheduler = TaskScheduler(
            self.database,
            RecordingOrderManager(),
            worker_manager,
            quota_manager,
            RecordingNotifications(),
            FakeBot(),
            poll_seconds=1,
        )
        task = scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()
        self.assertTrue(task.done())

    async def test_restart_requeues_safe_work_and_holds_uncertain_sends(self) -> None:
        # A validation task has no member-distribution side effect and is safe to resume.
        await self.database.create_order(
            order_id="ORD-RECOVER1",
            request_key="recover-1",
            customer_id=9300,
            username=None,
            display_name="Community Owner",
            source_ref="@source",
            destination_ref="@destination",
            campaign_name="Recovery",
            requested_size=1,
            announcement="Recovery announcement",
        )
        await self.database.claim_next_task()
        await self.database.close()
        self.database = Database(self.path)
        await self.database.open()
        recovered = await self.database.recover_after_restart()
        self.assertEqual(recovered, [])
        validate_task = await self.database.get_task("validate_order", "ORD-RECOVER1")
        self.assertEqual(validate_task["status"], TaskStatus.QUEUED.value)

        # Persist a real assigned campaign and an in-flight reservation, then simulate restart.
        await self.database.claim_next_task()
        await self.database.save_validation_success(
            "ORD-RECOVER1",
            GroupInfo(-10040001, "Source", "source", "supergroup"),
            GroupInfo(-10040002, "Destination", "destination", "supergroup"),
            "https://t.me/+recovery-link",
            "2099-01-01T00:00:00+00:00",
        )
        run_task = await self.database.claim_next_task()
        worker = await self.database.register_worker(9600, "Recovery Operator")
        assignment = await self.database.assign_queued_campaign(
            run_task["id"], "ORD-RECOVER1", 50, 86400
        )
        await self.database.reserve_distribution_action(
            worker["id"], assignment.campaign["id"], "ORD-RECOVER1", 50, 86400
        )
        await self.database.close()
        self.database = Database(self.path)
        await self.database.open()
        needs_review = await self.database.recover_after_restart()

        self.assertEqual(needs_review, ["ORD-RECOVER1"])
        order = await self.database.get_order("ORD-RECOVER1")
        worker = await self.database.get_worker(worker["id"])
        self.assertEqual(order["status"], OrderStatus.NEEDS_REVIEW.value)
        self.assertEqual(worker["status"], WorkerStatus.NEEDS_ATTENTION.value)
        self.assertEqual(
            (await self.database.get_task("run_campaign", "ORD-RECOVER1"))["status"],
            TaskStatus.NEEDS_REVIEW.value,
        )


if __name__ == "__main__":
    unittest.main()
