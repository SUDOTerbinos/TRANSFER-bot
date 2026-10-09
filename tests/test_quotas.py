import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import Database
from models import ActionAlreadyHandled, GroupInfo, WorkerStatus
from services.quota_manager import QuotaManager


class QuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "quota.sqlite3"
        self.database = Database(self.path)
        await self.database.open()
        self.worker = await self.database.register_worker(7001, "Authorized Operator")
        await self._create_queued_order("ORD-QUOTATEST1", "request-1")
        task = await self.database.claim_next_task()
        self.assertEqual(task["job_type"], "run_campaign")
        self.assignment = await self.database.assign_queued_campaign(
            task["id"], "ORD-QUOTATEST1", 50, 24 * 3600
        )
        self.assertTrue(self.assignment.assigned)

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.temp_dir.cleanup()

    async def _create_queued_order(self, order_id: str, request_key: str) -> None:
        await self.database.create_order(
            order_id=order_id,
            request_key=request_key,
            customer_id=8100,
            username=None,
            display_name="Customer",
            source_ref="@source",
            destination_ref="@destination",
            campaign_name="Move",
            requested_size=2,
            announcement="Community move announcement",
        )
        validation_task = await self.database.claim_next_task()
        self.assertEqual(validation_task["job_type"], "validate_order")
        await self.database.save_validation_success(
            order_id,
            GroupInfo(-10010001, "Source", "source", "supergroup"),
            GroupInfo(-10010002, "Destination", "destination", "supergroup"),
            f"https://t.me/+invite-{order_id.lower()}",
            "2099-01-01T00:00:00+00:00",
        )

    async def test_concurrent_callbacks_reserve_only_one_action(self) -> None:
        campaign_id = int(self.assignment.campaign["id"])
        attempts = await asyncio.gather(
            *[
                self.database.reserve_distribution_action(
                    int(self.worker["id"]), campaign_id, "ORD-QUOTATEST1", 1, 24 * 3600
                )
                for _ in range(8)
            ],
            return_exceptions=True,
        )
        reserved = [result for result in attempts if isinstance(result, int)]
        rejected = [
            result for result in attempts if isinstance(result, ActionAlreadyHandled)
        ]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(len(rejected), 7)

        snapshot = await self.database.get_quota_snapshot(
            int(self.worker["id"]), quota_limit=1, window_seconds=24 * 3600
        )
        self.assertEqual(snapshot.used, 1)
        self.assertEqual(snapshot.remaining, 0)
        self.assertIsNotNone(snapshot.next_eligible_at)

    async def test_failed_actions_are_audited_but_do_not_consume_quota(self) -> None:
        campaign_id = int(self.assignment.campaign["id"])
        action_id = await self.database.reserve_distribution_action(
            int(self.worker["id"]), campaign_id, "ORD-QUOTATEST1", 1, 24 * 3600
        )
        await self.database.mark_distribution_failed(
            action_id,
            int(self.worker["id"]),
            campaign_id,
            "ORD-QUOTATEST1",
            "Forbidden",
            "Telegram refused the authorized group announcement.",
        )
        snapshot = await self.database.get_quota_snapshot(
            int(self.worker["id"]), quota_limit=1, window_seconds=24 * 3600
        )
        errors = await self.database.recent_errors()
        self.assertEqual(snapshot.used, 0)
        self.assertEqual(snapshot.remaining, 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["status"], "failed")

    async def test_quota_eligibility_returns_after_rolling_window_expires(self) -> None:
        campaign_id = int(self.assignment.campaign["id"])
        action_id = await self.database.reserve_distribution_action(
            int(self.worker["id"]), campaign_id, "ORD-QUOTATEST1", 1, 3600
        )
        await self.database.mark_distribution_success(
            action_id,
            int(self.worker["id"]),
            campaign_id,
            "ORD-QUOTATEST1",
            56,
            1,
            3600,
        )
        expired_at = (
            (datetime.now(timezone.utc) - timedelta(hours=2))
            .replace(microsecond=0)
            .isoformat()
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE worker_actions SET occurred_at = ? WHERE id = ?",
                (expired_at, action_id),
            )

        await self.database.refresh_worker_cooldowns(1, 3600)
        worker = await self.database.get_worker(int(self.worker["id"]))
        snapshot = await self.database.get_quota_snapshot(
            int(self.worker["id"]), quota_limit=1, window_seconds=3600
        )
        self.assertEqual(worker["status"], WorkerStatus.IDLE.value)
        self.assertEqual(snapshot.used, 0)
        self.assertEqual(snapshot.remaining, 1)

    async def test_quota_prevents_assignment_until_window_expires(self) -> None:
        campaign_id = int(self.assignment.campaign["id"])
        action_id = await self.database.reserve_distribution_action(
            int(self.worker["id"]), campaign_id, "ORD-QUOTATEST1", 1, 24 * 3600
        )
        await self.database.mark_distribution_success(
            action_id,
            int(self.worker["id"]),
            campaign_id,
            "ORD-QUOTATEST1",
            55,
            1,
            24 * 3600,
        )
        worker = await self.database.get_worker(int(self.worker["id"]))
        self.assertEqual(worker["status"], WorkerStatus.COOLDOWN.value)

        await self._create_queued_order("ORD-QUOTATEST2", "request-2")
        queued = await self.database.claim_next_task()
        self.assertEqual(queued["job_type"], "run_campaign")
        denied = await self.database.assign_queued_campaign(
            queued["id"], "ORD-QUOTATEST2", 1, 24 * 3600
        )
        self.assertFalse(denied.assigned)
        self.assertIn("quota", denied.reason.lower())
        self.assertIsNotNone(denied.next_eligible_at)

        quota = QuotaManager(self.database, default_limit=50)
        await quota.set_limit(1)
        current = await quota.snapshot(int(self.worker["id"]))
        self.assertEqual(current.used, 1)
        self.assertEqual(current.remaining, 0)


if __name__ == "__main__":
    unittest.main()
