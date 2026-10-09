"""Official Bot API group validation and opt-in join-request invitations."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

from models import GroupInfo, GroupValidationError

logger = logging.getLogger(__name__)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_CHAT_ID_RE = re.compile(r"^-100\d{5,}$|^-\d{5,}$|^\d{5,}$")
_ALLOWED_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
ADMIN_STATUSES = {"creator", "administrator"}


class TransientTelegramError(Exception):
    """A temporary Bot API error with a safe retry delay."""

    def __init__(self, message: str, retry_after: float = 15.0) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))


class DistributionUncertainError(Exception):
    """The send request timed out, so delivery cannot safely be retried."""


class FloodWaitDistributionError(GroupValidationError):
    """Telegram requested a specific cooldown before any later distribution."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(1.0, float(retry_after_seconds))
        super().__init__(
            f"Telegram requested a {int(self.retry_after_seconds)} second cooldown; "
            "no automatic retry was attempted."
        )


def normalize_group_reference(raw: str) -> str:
    """Normalize public usernames/links or numeric IDs without resolving them.

    Telegram's Bot API cannot resolve private invite links to chat IDs. A bot
    must first be added to that group; /chatid then provides the numeric ID.
    """
    value = (raw or "").strip()
    if not value or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        raise GroupValidationError(
            "Enter a valid @username, public group link, or numeric chat ID."
        )
    if _CHAT_ID_RE.fullmatch(value):
        return str(int(value))
    if value.startswith("-"):
        raise GroupValidationError(
            "That chat ID does not look valid. Use /chatid in the group."
        )

    candidate = value
    if " " in candidate:
        raise GroupValidationError("Group references cannot contain spaces.")
    if candidate.startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        candidate = "https://" + candidate
    if candidate.startswith(("https://", "http://")):
        parsed = urlparse(candidate)
        if parsed.hostname not in _ALLOWED_HOSTS:
            raise GroupValidationError("Use a Telegram t.me group link or @username.")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            raise GroupValidationError(
                "The Telegram link does not contain a group username."
            )
        first = segments[0]
        if first.startswith("+") or first == "joinchat" or first == "c":
            raise GroupValidationError(
                "The Bot API cannot resolve private invite links. Add me to the group and "
                "enter its numeric chat ID (run /chatid in that group)."
            )
        if first == "s":
            if len(segments) < 2:
                raise GroupValidationError("The public group link is incomplete.")
            first = segments[1]
        candidate = first
    candidate = candidate.removeprefix("@")
    if not _USERNAME_RE.fullmatch(candidate):
        raise GroupValidationError(
            "Enter a public group @username, a t.me/username link, or a numeric chat ID."
        )
    return f"@{candidate}"


class InviteService:
    def __init__(self, expiry_days: int = 7) -> None:
        self.expiry_days = expiry_days

    @staticmethod
    def _api_error(exc: TelegramError, task: str) -> Exception:
        if isinstance(exc, RetryAfter):
            retry_after = exc.retry_after
            if isinstance(retry_after, timedelta):
                retry_seconds = retry_after.total_seconds()
            else:
                retry_seconds = float(retry_after)
            return TransientTelegramError(
                f"Telegram asked the bot to wait before {task}.", retry_seconds
            )
        if isinstance(exc, (TimedOut, NetworkError)):
            return TransientTelegramError(
                f"A temporary network issue occurred while {task}.", 20.0
            )
        return GroupValidationError(
            f"Telegram could not {task}. Check the group's accessibility and bot permissions."
        )

    async def _resolve_group(self, bot, reference: str) -> tuple[Any, GroupInfo]:
        normalized = normalize_group_reference(reference)
        chat_id: int | str = (
            int(normalized) if normalized.lstrip("-").isdigit() else normalized
        )
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramError as exc:
            raise self._api_error(exc, "access the group") from None
        if chat.type not in {"group", "supergroup"}:
            raise GroupValidationError(
                "Only Telegram groups and supergroups are supported."
            )
        info = GroupInfo(
            chat_id=int(chat.id),
            title=chat.title or chat.username or str(chat.id),
            username=chat.username,
            chat_type=chat.type,
        )
        return chat, info

    async def _require_group_admin(self, bot, chat_id: int, user_id: int) -> Any:
        try:
            bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            if getattr(bot_member, "status", "") not in ADMIN_STATUSES:
                raise GroupValidationError(
                    "The bot must be an administrator in both groups so it can verify "
                    "permissions and publish the approved announcement."
                )
            user_member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except GroupValidationError:
            raise
        except TelegramError as exc:
            raise self._api_error(
                exc, "verify group administrator permissions"
            ) from None
        if getattr(user_member, "status", "") not in ADMIN_STATUSES:
            raise GroupValidationError(
                "The customer placing the order must be an administrator in both groups."
            )
        return bot_member

    async def validate_order(
        self, order: dict[str, Any], bot
    ) -> tuple[GroupInfo, GroupInfo]:
        """Verify bot/customer admin access, without reading member lists."""
        _, source = await self._resolve_group(bot, order["source_ref"])
        _, destination = await self._resolve_group(bot, order["destination_ref"])
        if source.chat_id == destination.chat_id:
            raise GroupValidationError(
                "Source and destination groups must be different."
            )

        await self._require_group_admin(bot, source.chat_id, int(order["customer_id"]))
        destination_bot = await self._require_group_admin(
            bot, destination.chat_id, int(order["customer_id"])
        )
        if not (
            getattr(destination_bot, "status", "") == "creator"
            or (
                getattr(destination_bot, "status", "") == "administrator"
                and bool(getattr(destination_bot, "can_invite_users", False))
            )
        ):
            raise GroupValidationError(
                "In the destination group, make the bot an administrator with permission "
                "to invite users/create invite links."
            )
        return source, destination

    async def create_join_request_link(
        self, destination_chat_id: int, order_id: str, bot
    ) -> tuple[str, str]:
        expires = datetime.now(timezone.utc) + timedelta(days=self.expiry_days)
        link_name = f"Migration {order_id[-16:]}"[:32]
        try:
            result = await bot.create_chat_invite_link(
                chat_id=destination_chat_id,
                name=link_name,
                expire_date=expires,
                creates_join_request=True,
            )
        except TelegramError as exc:
            converted = self._api_error(exc, "create a destination join-request link")
            if isinstance(converted, TransientTelegramError):
                raise converted from None
            raise GroupValidationError(
                "Telegram could not create a destination invite link. Verify that the "
                "destination accepts join requests and the bot has invite-link permission."
            ) from None
        expires_iso = expires.replace(microsecond=0).isoformat()
        return result.invite_link, expires_iso

    async def validate_and_prepare(
        self, order: dict[str, Any], bot
    ) -> tuple[GroupInfo, GroupInfo, str, str]:
        source, destination = await self.validate_order(order, bot)
        invite_link, expires_at = await self.create_join_request_link(
            destination.chat_id, order["order_id"], bot
        )
        return source, destination, invite_link, expires_at

    async def distribute_announcement(
        self, campaign: dict[str, Any], order: dict[str, Any], bot
    ) -> int:
        invite_link = campaign.get("invite_link")
        if not invite_link:
            raise GroupValidationError(
                "This campaign has no active destination invite link."
            )
        expires_at = campaign.get("invite_expires_at")
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at)
            except ValueError:
                raise GroupValidationError(
                    "The saved destination invite expiry is invalid."
                ) from None
            if expires <= datetime.now(timezone.utc):
                raise GroupValidationError(
                    "The destination invite link expired before distribution."
                )
        announcement = (
            campaign.get("announcement")
            or campaign.get("campaign_name")
            or "Community migration"
        )
        announcement = announcement.strip()[:500]
        text = (
            f"{announcement}\n\n"
            "We are sharing an optional invitation to a destination community. "
            "Joining is entirely your choice. Tap below to request access; destination "
            "admins may review the request. No one is added automatically."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Request to join", url=invite_link)]]
        )
        try:
            message = await bot.send_message(
                chat_id=int(order["source_group_id"]),
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            converted = self._api_error(
                exc, "post the approved source-group announcement"
            )
            retry_seconds = (
                converted.retry_after
                if isinstance(converted, TransientTelegramError)
                else 60.0
            )
            raise FloodWaitDistributionError(retry_seconds) from None
        except (TimedOut, NetworkError):
            raise DistributionUncertainError(
                "The announcement request timed out; delivery could not be confirmed."
            ) from None
        except TelegramError:
            raise GroupValidationError(
                "Telegram did not accept the announcement. The operation is paused for "
                "administrator review; check source-group permissions."
            ) from None
        return int(message.message_id)

    async def revoke_link(self, chat_id: int, invite_link: str, bot) -> bool:
        if not invite_link:
            return False
        try:
            await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite_link)
            return True
        except TelegramError as exc:
            logger.warning("Could not revoke an invite link (%s)", type(exc).__name__)
            return False

    @staticmethod
    def announcement_message_text(campaign: dict[str, Any]) -> str:
        return (
            f"Opt-in announcement for campaign {campaign.get('campaign_name', '')[:80]}"
        )
