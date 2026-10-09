"""Management of explicitly registered human campaign operators.

Workers are not Telegram user-account sessions. They authorize a Bot API
announcement by pressing a private confirmation button; all group operations
are performed by the one configured bot identity.
"""

from __future__ import annotations

from database import Database
from models import DomainError, WorkerStatus
from services.quota_manager import QuotaManager


class WorkerManager:
    def __init__(self, database: Database, quota_manager: QuotaManager) -> None:
        self.database = database
        self.quota_manager = quota_manager
        # Volatile cache for current process state; SQLite remains authoritative.
        self.runtime_state: dict[int, dict] = {}

    async def sync_runtime_state(self) -> dict[int, dict]:
        workers = await self.list_workers()
        self.runtime_state = {
            int(worker["id"]): {
                "status": worker["status"],
                "assigned_campaign_id": worker["assigned_campaign_id"],
                "last_activity_at": worker["last_activity_at"],
                "quota_used": worker.get("quota_used", 0),
                "quota_remaining": worker.get("quota_remaining", 0),
                "next_eligible_at": worker.get("next_eligible_at"),
            }
            for worker in workers
        }
        return self.runtime_state

    async def register(self, telegram_user_id: int, display_name: str) -> dict:
        display_name = " ".join(display_name.split()).strip()
        if telegram_user_id <= 0:
            raise DomainError("Worker Telegram user ID must be a positive integer.")
        if not 2 <= len(display_name) <= 80:
            raise DomainError(
                "Worker display name must be between 2 and 80 characters."
            )
        return await self.database.register_worker(telegram_user_id, display_name)

    async def activate(self, telegram_user_id: int) -> dict | None:
        worker = await self.database.activate_worker(telegram_user_id)
        if worker is None:
            return None
        return await self._with_quota(worker)

    async def get_for_user(self, telegram_user_id: int) -> dict | None:
        worker = await self.database.get_worker_for_user(telegram_user_id)
        if worker is None:
            return None
        return await self._with_quota(worker)

    async def get_by_id(self, worker_id: int) -> dict | None:
        worker = await self.database.get_worker(worker_id)
        if worker is None:
            return None
        return await self._with_quota(worker)

    async def list_workers(self) -> list[dict]:
        limit, window_seconds = await self.quota_manager.policy()
        return await self.database.list_workers(limit, window_seconds)

    async def _with_quota(self, worker: dict) -> dict:
        snapshot = await self.quota_manager.snapshot(int(worker["id"]))
        result = dict(worker)
        result["quota_used"] = snapshot.used
        result["quota_remaining"] = snapshot.remaining
        result["quota_limit"] = snapshot.limit
        result["quota_next_eligible_at"] = snapshot.next_eligible_at
        if not result["enabled"]:
            result["status"] = WorkerStatus.DISABLED.value
        elif result["status"] == WorkerStatus.COOLDOWN.value and snapshot.remaining:
            result["status"] = WorkerStatus.IDLE.value
        return result

    async def pause(
        self, worker_id: int, reason: str
    ) -> tuple[dict | None, dict | None]:
        return await self.database.set_worker_paused(worker_id, reason)

    async def resume(self, worker_id: int) -> tuple[dict | None, dict | None]:
        return await self.database.resume_worker(worker_id)

    async def disable(
        self, worker_id: int, reason: str
    ) -> tuple[dict | None, dict | None]:
        return await self.database.disable_worker(worker_id, reason)
