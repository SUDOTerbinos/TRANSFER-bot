"""Persistent rolling-window action quotas."""

from __future__ import annotations

from database import Database
from models import DomainError, QuotaSnapshot


class QuotaManager:
    def __init__(
        self,
        database: Database,
        default_limit: int = 50,
        default_window_hours: int = 24,
    ) -> None:
        self.database = database
        self.default_limit = default_limit
        self.default_window_hours = default_window_hours

    async def policy(self) -> tuple[int, int]:
        raw_limit = await self.database.get_setting(
            "action_quota", str(self.default_limit)
        )
        raw_hours = await self.database.get_setting(
            "quota_window_hours", str(self.default_window_hours)
        )
        try:
            limit = int(raw_limit)
            hours = int(raw_hours)
        except ValueError:
            limit, hours = self.default_limit, self.default_window_hours
        if not 1 <= limit <= 100_000:
            limit = self.default_limit
        if not 1 <= hours <= 24 * 365:
            hours = self.default_window_hours
        return limit, hours * 3600

    async def set_limit(self, limit: int) -> None:
        if not 1 <= limit <= 100_000:
            raise DomainError("Quota must be between 1 and 100000 actions.")
        await self.database.set_setting("action_quota", str(limit))

    async def set_window_hours(self, hours: int) -> None:
        if not 1 <= hours <= 24 * 365:
            raise DomainError("The reporting window must be between 1 and 8760 hours.")
        await self.database.set_setting("quota_window_hours", str(hours))

    async def snapshot(self, worker_id: int) -> QuotaSnapshot:
        limit, window_seconds = await self.policy()
        return await self.database.get_quota_snapshot(worker_id, limit, window_seconds)

    async def refresh_workers(self) -> None:
        limit, window_seconds = await self.policy()
        await self.database.refresh_worker_cooldowns(limit, window_seconds)

    async def reserve_distribution(
        self, worker_id: int, campaign_id: int, order_id: str
    ) -> int:
        limit, window_seconds = await self.policy()
        return await self.database.reserve_distribution_action(
            worker_id, campaign_id, order_id, limit, window_seconds
        )

    async def complete_distribution(
        self,
        action_id: int,
        worker_id: int,
        campaign_id: int,
        order_id: str,
        message_id: int,
    ) -> dict | None:
        limit, window_seconds = await self.policy()
        return await self.database.mark_distribution_success(
            action_id,
            worker_id,
            campaign_id,
            order_id,
            message_id,
            limit,
            window_seconds,
        )
