import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import _issue_invite
from store import DestinationStore


class InviteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        store = DestinationStore(Path(self.temp_dir.name) / "groups.json")
        store.set(-1001, -1002, "Destination")
        self.create_link = AsyncMock(
            return_value=SimpleNamespace(invite_link="https://t.me/+example")
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"destination_store": store}),
            bot=SimpleNamespace(create_chat_invite_link=self.create_link),
        )
        self.reply = AsyncMock()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_issues_and_reuses_join_request_link(self) -> None:
        await _issue_invite(-1001, "supergroup", 10, self.reply, self.context)

        self.create_link.assert_awaited_once()
        kwargs = self.create_link.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -1002)
        self.assertTrue(kwargs["creates_join_request"])
        self.assertEqual(kwargs["name"], "Opt-in group move")
        markup = self.reply.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].url,
            "https://t.me/+example",
        )

        # Other members get the active link instead of creating more invite links.
        await _issue_invite(-1001, "supergroup", 11, AsyncMock(), self.context)
        self.create_link.assert_awaited_once()

    async def test_same_user_is_rate_limited(self) -> None:
        await _issue_invite(-1001, "supergroup", 10, self.reply, self.context)
        second_reply = AsyncMock()
        await _issue_invite(-1001, "supergroup", 10, second_reply, self.context)

        self.create_link.assert_awaited_once()
        self.assertIn("wait", second_reply.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
