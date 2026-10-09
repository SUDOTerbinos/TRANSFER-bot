import hashlib
import unittest
from types import SimpleNamespace

from database import Database
from models import GroupInfo, GroupValidationError, OrderStatus, TaskStatus
from services.invite_service import normalize_group_reference
from services.order_manager import OrderManager


class OrderPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = Database(":memory:")
        await self.database.open()

    async def asyncTearDown(self) -> None:
        await self.database.close()

    async def _create_order(
        self, request_key: str = "request-1", requested_size: int = 25
    ) -> dict:
        return await self.database.create_order(
            order_id="ORD-ORDERTEST1",
            request_key=request_key,
            customer_id=101,
            username="customer",
            display_name="Community Admin",
            source_ref="@source_group",
            destination_ref="@destination_group",
            campaign_name="Community move",
            requested_size=requested_size,
            announcement="Community migration: Community move",
        )

    async def test_order_confirmation_is_idempotent(self) -> None:
        first = await self._create_order()
        duplicate = await self._create_order()
        orders = await self.database.list_orders(customer_id=101)
        tasks = await self.database._fetchall("SELECT * FROM task_queue")

        self.assertEqual(first["order_id"], duplicate["order_id"])
        self.assertEqual(first["status"], OrderStatus.PENDING.value)
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["job_type"], "validate_order")

    async def test_validation_failure_is_persisted_for_administrator_review(
        self,
    ) -> None:
        await self._create_order()
        task = await self.database.claim_next_task()
        self.assertTrue(await self.database.set_order_validating("ORD-ORDERTEST1"))
        reviewed = await self.database.mark_order_needs_review(
            "ORD-ORDERTEST1",
            "Group access validation failed.",
            task_type="validate_order",
        )
        task_after = await self.database.get_task("validate_order", "ORD-ORDERTEST1")
        self.assertIsNotNone(task)
        self.assertEqual(reviewed["status"], OrderStatus.NEEDS_REVIEW.value)
        self.assertEqual(task_after["status"], TaskStatus.NEEDS_REVIEW.value)

    async def test_validation_persists_groups_and_generated_link(self) -> None:
        await self._create_order()
        task = await self.database.claim_next_task()
        self.assertEqual(task["job_type"], "validate_order")
        source = GroupInfo(-100111111, "Source group", "source_group", "supergroup")
        destination = GroupInfo(
            -100222222, "Destination", "destination_group", "supergroup"
        )

        order = await self.database.save_validation_success(
            "ORD-ORDERTEST1",
            source,
            destination,
            "https://t.me/+example-request-link",
            "2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(order["status"], OrderStatus.QUEUED.value)
        self.assertEqual(order["invitations_generated"], 1)
        self.assertEqual(order["source_title"], "Source group")
        self.assertEqual(order["destination_title"], "Destination")
        self.assertEqual(order["invite_link"], "https://t.me/+example-request-link")

        stored = await self.database.get_order("ORD-ORDERTEST1")
        self.assertEqual(stored["resolved_destination_id"], -100222222)

    async def test_admin_list_is_authoritative_and_order_cancellation_is_scoped(
        self,
    ) -> None:
        await self.database.seed_administrators((501,))
        self.assertTrue(await self.database.is_administrator(501))
        self.assertFalse(await self.database.is_administrator(502))
        await self.database.seed_administrators((502,))
        self.assertFalse(await self.database.is_administrator(501))
        self.assertTrue(await self.database.is_administrator(502))

        await self._create_order()
        _, result, _ = await self.database.cancel_order(
            "ORD-ORDERTEST1", requester_id=202, is_admin=False
        )
        self.assertEqual(result, "forbidden")
        order, result, _ = await self.database.cancel_order(
            "ORD-ORDERTEST1", requester_id=101, is_admin=False
        )
        self.assertEqual(result, "cancelled")
        self.assertEqual(order["status"], OrderStatus.CANCELLED.value)

    async def test_join_updates_are_deduplicated_with_keyed_pseudonyms(self) -> None:
        await self._create_order(requested_size=2)
        await self.database.claim_next_task()
        destination = GroupInfo(
            -100222222, "Destination", "destination_group", "supergroup"
        )
        await self.database.save_validation_success(
            "ORD-ORDERTEST1",
            GroupInfo(-100111111, "Source", "source_group", "supergroup"),
            destination,
            "https://t.me/+example-request-link",
            "2099-01-01T00:00:00+00:00",
        )

        class Notifications:
            async def order_update(self, order, event):
                return None

        class InviteService:
            async def revoke_link(self, chat_id, invite_link, bot):
                return True

        def manager():
            return OrderManager(
                self.database,
                InviteService(),
                Notifications(),
                SimpleNamespace(),
            )

        await manager().record_chat_member_join(
            destination.chat_id, "https://t.me/+example-request-link", 303
        )
        await manager().record_chat_member_join(
            destination.chat_id, "https://t.me/+example-request-link", 303
        )
        await manager().record_chat_member_join(
            destination.chat_id, "https://t.me/+example-request-link", 304
        )
        await manager().record_chat_member_join(
            destination.chat_id, "https://t.me/+example-request-link", 305
        )
        await manager().record_chat_member_join(
            destination.chat_id, "https://t.me/+example-request-link", 303
        )
        order = await self.database.get_order("ORD-ORDERTEST1")
        events = await self.database._fetchall(
            "SELECT subject_hash FROM campaign_events WHERE event_type = 'join_confirmed' ORDER BY id"
        )

        self.assertEqual(order["confirmed_joins"], 3)
        self.assertEqual(order["status"], OrderStatus.COMPLETED.value)
        self.assertEqual(len(events), 3)
        raw_hash = hashlib.sha256(b"1:303").hexdigest()
        self.assertNotEqual(events[0]["subject_hash"], raw_hash)
        self.assertNotIn("303", events[0]["subject_hash"])

    async def test_group_reference_validation_handles_private_links(self) -> None:
        self.assertEqual(normalize_group_reference("@public_group"), "@public_group")
        self.assertEqual(
            normalize_group_reference("https://t.me/public_group/42"), "@public_group"
        )
        self.assertEqual(
            normalize_group_reference("t.me/public_group"), "@public_group"
        )
        self.assertEqual(normalize_group_reference("-1001234567890"), "-1001234567890")
        with self.assertRaises(GroupValidationError):
            normalize_group_reference("https://t.me/+privateInviteHash")
        with self.assertRaises(GroupValidationError):
            normalize_group_reference("https://example.com/group")


if __name__ == "__main__":
    unittest.main()
