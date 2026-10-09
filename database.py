"""Async SQLite persistence and transaction-safe quota/task operations."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from models import (
    ActionAlreadyHandled,
    AssignmentResult,
    GroupInfo,
    OrderStatus,
    QuotaExceeded,
    QuotaSnapshot,
    TaskStatus,
    WorkerStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS administrators (
    telegram_user_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    telegram_user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    chat_type TEXT NOT NULL CHECK (chat_type IN ('group', 'supergroup')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    request_key TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(telegram_user_id),
    source_ref TEXT NOT NULL,
    destination_ref TEXT NOT NULL,
    source_group_id INTEGER REFERENCES groups(chat_id) ON DELETE SET NULL,
    destination_group_id INTEGER REFERENCES groups(chat_id) ON DELETE SET NULL,
    campaign_name TEXT NOT NULL,
    requested_size INTEGER NOT NULL CHECK (requested_size > 0),
    status TEXT NOT NULL,
    status_reason TEXT,
    queue_reason TEXT,
    accepted_at TEXT NOT NULL,
    validated_at TEXT,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
    campaign_name TEXT NOT NULL,
    requested_size INTEGER NOT NULL CHECK (requested_size > 0),
    status TEXT NOT NULL,
    announcement TEXT NOT NULL,
    invite_link TEXT,
    invite_expires_at TEXT,
    generated_count INTEGER NOT NULL DEFAULT 0,
    distributed_count INTEGER NOT NULL DEFAULT 0,
    confirmed_join_count INTEGER NOT NULL DEFAULT 0,
    failed_actions INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    status TEXT NOT NULL,
    assigned_campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    last_activity_at TEXT,
    daily_action_count INTEGER NOT NULL DEFAULT 0,
    next_eligible_at TEXT,
    error_history TEXT NOT NULL DEFAULT '[]',
    pause_reason TEXT,
    created_at TEXT NOT NULL,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'success', 'failed', 'needs_review')),
    event_key TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS invite_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL UNIQUE REFERENCES campaigns(id) ON DELETE CASCADE,
    destination_chat_id INTEGER NOT NULL REFERENCES groups(chat_id),
    invite_link TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    subject_hash TEXT,
    details TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL CHECK (job_type IN ('validate_order', 'run_campaign')),
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'assigned', 'completed', 'paused', 'needs_review', 'cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    run_after TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    UNIQUE(job_type, order_id)
);

CREATE TABLE IF NOT EXISTS application_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer_created
    ON orders(customer_id, accepted_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(status, last_updated DESC);
CREATE INDEX IF NOT EXISTS idx_workers_enabled_status
    ON workers(enabled, status, assigned_campaign_id);
CREATE INDEX IF NOT EXISTS idx_worker_actions_quota
    ON worker_actions(worker_id, status, occurred_at);
CREATE INDEX IF NOT EXISTS idx_campaign_events_campaign_type
    ON campaign_events(campaign_id, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_task_queue_ready
    ON task_queue(status, run_after, id);
CREATE INDEX IF NOT EXISTS idx_invite_links_destination
    ON invite_links(destination_chat_id, revoked_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_iso(value: datetime | None = None) -> str:
    return (
        (value or utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    )


def _row_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    """One async SQLite connection; mutating operations use BEGIN IMMEDIATE.

    The per-instance lock protects the shared aiosqlite connection, while
    BEGIN IMMEDIATE serializes writers at the SQLite level as well. This keeps
    quota reservations and task claims safe across concurrent handlers and
    across multiple application processes using the same database file.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path, timeout=10)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 10000")
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.executescript(SCHEMA)
        await connection.commit()
        self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    def _conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError(
                "Database.open() must be called before database operations"
            )
        return self._connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = self._conn()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _fetchone(
        self, query: str, parameters: tuple = ()
    ) -> dict[str, Any] | None:
        async with self._lock:
            cursor = await self._conn().execute(query, parameters)
            row = await cursor.fetchone()
            await cursor.close()
            return _row_dict(row)

    async def _fetchall(
        self, query: str, parameters: tuple = ()
    ) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._conn().execute(query, parameters)
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(row) for row in rows]

    async def seed_administrators(self, user_ids: tuple[int, ...]) -> None:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute("DELETE FROM administrators")
            for user_id in user_ids:
                await connection.execute(
                    "INSERT INTO administrators(telegram_user_id, added_at) VALUES (?, ?)",
                    (user_id, now),
                )

    async def is_administrator(self, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS allowed FROM administrators WHERE telegram_user_id = ?",
            (user_id,),
        )
        return row is not None

    async def upsert_customer(
        self, telegram_user_id: int, username: str | None, display_name: str
    ) -> None:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO customers(telegram_user_id, username, display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "username = excluded.username, display_name = excluded.display_name, updated_at = excluded.updated_at",
                (telegram_user_id, username, display_name, now, now),
            )

    async def get_customer(self, telegram_user_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM customers WHERE telegram_user_id = ?", (telegram_user_id,)
        )

    async def set_notifications(self, telegram_user_id: int, enabled: bool) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE customers SET notifications_enabled = ?, updated_at = ? "
                "WHERE telegram_user_id = ?",
                (int(enabled), utc_iso(), telegram_user_id),
            )

    async def create_order(
        self,
        *,
        order_id: str,
        request_key: str,
        customer_id: int,
        username: str | None,
        display_name: str,
        source_ref: str,
        destination_ref: str,
        campaign_name: str,
        requested_size: int,
        announcement: str,
    ) -> dict[str, Any]:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO customers(telegram_user_id, username, display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "username = excluded.username, display_name = excluded.display_name, updated_at = excluded.updated_at",
                (customer_id, username, display_name, now, now),
            )
            existing = await connection.execute(
                "SELECT order_id, customer_id FROM orders WHERE request_key = ?",
                (request_key,),
            )
            prior = await existing.fetchone()
            await existing.close()
            if prior is not None:
                if prior["customer_id"] != customer_id:
                    raise ValueError("The order request key is already in use")
                return await self._get_order_in_transaction(
                    connection, prior["order_id"]
                )

            await connection.execute(
                "INSERT INTO orders(order_id, request_key, customer_id, source_ref, destination_ref, "
                "campaign_name, requested_size, status, accepted_at, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    request_key,
                    customer_id,
                    source_ref,
                    destination_ref,
                    campaign_name,
                    requested_size,
                    OrderStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            await connection.execute(
                "INSERT INTO campaigns(order_id, campaign_name, requested_size, status, announcement, created_at, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    campaign_name,
                    requested_size,
                    OrderStatus.PENDING.value,
                    announcement,
                    now,
                    now,
                ),
            )
            await connection.execute(
                "INSERT INTO task_queue(job_type, order_id, status, payload_json, run_after, created_at, last_updated) "
                "VALUES ('validate_order', ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    TaskStatus.QUEUED.value,
                    json.dumps({"order_id": order_id}),
                    now,
                    now,
                    now,
                ),
            )
            return await self._get_order_in_transaction(connection, order_id)

    async def _get_order_in_transaction(
        self, connection: aiosqlite.Connection, order_id: str
    ) -> dict[str, Any] | None:
        cursor = await connection.execute(
            self._order_select() + " WHERE o.order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_dict(row)

    @staticmethod
    def _order_select() -> str:
        return (
            "SELECT o.*, c.id AS campaign_id, c.status AS campaign_status, "
            "c.generated_count AS invitations_generated, c.distributed_count AS invitations_distributed, "
            "c.confirmed_join_count AS confirmed_joins, c.failed_actions, c.invite_link, c.invite_expires_at, "
            "s.title AS source_title, s.chat_id AS resolved_source_id, "
            "d.title AS destination_title, d.chat_id AS resolved_destination_id "
            "FROM orders o JOIN campaigns c ON c.order_id = o.order_id "
            "LEFT JOIN groups s ON s.chat_id = o.source_group_id "
            "LEFT JOIN groups d ON d.chat_id = o.destination_group_id"
        )

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            self._order_select() + " WHERE o.order_id = ?", (order_id,)
        )

    async def list_orders(
        self, customer_id: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        query = self._order_select()
        parameters: tuple[Any, ...] = ()
        if customer_id is not None:
            query += " WHERE o.customer_id = ?"
            parameters = (customer_id,)
        query += " ORDER BY o.accepted_at DESC LIMIT ?"
        return await self._fetchall(query, (*parameters, limit))

    async def set_order_validating(self, order_id: str) -> bool:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE orders SET status = ?, status_reason = NULL, last_updated = ? "
                "WHERE order_id = ? AND status NOT IN (?, ?, ?)",
                (
                    OrderStatus.VALIDATING.value,
                    utc_iso(),
                    order_id,
                    OrderStatus.CANCELLED.value,
                    OrderStatus.COMPLETED.value,
                    OrderStatus.NEEDS_REVIEW.value,
                ),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            if changed:
                await connection.execute(
                    "UPDATE campaigns SET status = ?, last_updated = ? WHERE order_id = ?",
                    (OrderStatus.VALIDATING.value, utc_iso(), order_id),
                )
            return changed

    async def save_validation_success(
        self,
        order_id: str,
        source: GroupInfo,
        destination: GroupInfo,
        invite_link: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            order_cursor = await connection.execute(
                "SELECT status FROM orders WHERE order_id = ?", (order_id,)
            )
            order_row = await order_cursor.fetchone()
            await order_cursor.close()
            if order_row is None or order_row["status"] in {
                OrderStatus.CANCELLED.value,
                OrderStatus.COMPLETED.value,
                OrderStatus.NEEDS_REVIEW.value,
            }:
                return None

            for group in (source, destination):
                await connection.execute(
                    "INSERT INTO groups(chat_id, title, username, chat_type, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, username = excluded.username, "
                    "chat_type = excluded.chat_type, updated_at = excluded.updated_at",
                    (group.chat_id, group.title, group.username, group.chat_type, now),
                )
            await connection.execute(
                "UPDATE orders SET source_group_id = ?, destination_group_id = ?, status = ?, "
                "status_reason = NULL, queue_reason = NULL, validated_at = ?, last_updated = ? WHERE order_id = ?",
                (
                    source.chat_id,
                    destination.chat_id,
                    OrderStatus.QUEUED.value,
                    now,
                    now,
                    order_id,
                ),
            )
            await connection.execute(
                "UPDATE campaigns SET status = ?, generated_count = 1, invite_link = ?, "
                "invite_expires_at = ?, last_updated = ? WHERE order_id = ?",
                (OrderStatus.QUEUED.value, invite_link, expires_at, now, order_id),
            )
            campaign_cursor = await connection.execute(
                "SELECT id FROM campaigns WHERE order_id = ?", (order_id,)
            )
            campaign_row = await campaign_cursor.fetchone()
            await campaign_cursor.close()
            campaign_id = int(campaign_row["id"])
            await connection.execute(
                "INSERT INTO invite_links(campaign_id, destination_chat_id, invite_link, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(campaign_id) DO UPDATE SET "
                "destination_chat_id = excluded.destination_chat_id, invite_link = excluded.invite_link, "
                "expires_at = excluded.expires_at, created_at = excluded.created_at, revoked_at = NULL",
                (campaign_id, destination.chat_id, invite_link, expires_at, now),
            )
            await connection.execute(
                "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, details, occurred_at) "
                "VALUES (?, ?, 'invite_generated', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
                (
                    campaign_id,
                    order_id,
                    f"campaign:{campaign_id}:invite-generated",
                    "A destination join-request link was created.",
                    now,
                ),
            )
            await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = NULL, last_error = NULL, last_updated = ? "
                "WHERE job_type = 'validate_order' AND order_id = ?",
                (TaskStatus.COMPLETED.value, now, order_id),
            )
            await connection.execute(
                "INSERT INTO task_queue(job_type, order_id, status, payload_json, run_after, created_at, last_updated) "
                "VALUES ('run_campaign', ?, ?, ?, ?, ?, ?) ON CONFLICT(job_type, order_id) DO NOTHING",
                (
                    order_id,
                    TaskStatus.QUEUED.value,
                    json.dumps({"order_id": order_id}),
                    now,
                    now,
                    now,
                ),
            )
            return await self._get_order_in_transaction(connection, order_id)

    async def mark_order_needs_review(
        self, order_id: str, reason: str, task_type: str | None = None
    ) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT c.id, c.status AS campaign_status, o.status AS order_status "
                "FROM campaigns c JOIN orders o ON o.order_id = c.order_id WHERE c.order_id = ?",
                (order_id,),
            )
            campaign = await cursor.fetchone()
            await cursor.close()
            if (
                campaign is None
                or campaign["order_status"]
                in {
                    OrderStatus.CANCELLED.value,
                    OrderStatus.COMPLETED.value,
                }
                or campaign["campaign_status"]
                in {
                    OrderStatus.CANCELLED.value,
                    OrderStatus.COMPLETED.value,
                }
            ):
                return await self._get_order_in_transaction(connection, order_id)
            campaign_id = int(campaign["id"])
            await connection.execute(
                "UPDATE orders SET status = ?, status_reason = ?, queue_reason = NULL, last_updated = ? "
                "WHERE order_id = ? AND status NOT IN (?, ?)",
                (
                    OrderStatus.NEEDS_REVIEW.value,
                    reason[:500],
                    now,
                    order_id,
                    OrderStatus.CANCELLED.value,
                    OrderStatus.COMPLETED.value,
                ),
            )
            await connection.execute(
                "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                (OrderStatus.NEEDS_REVIEW.value, now, campaign_id),
            )
            if task_type:
                await connection.execute(
                    "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                    "WHERE job_type = ? AND order_id = ? AND status <> ?",
                    (
                        TaskStatus.NEEDS_REVIEW.value,
                        reason[:500],
                        now,
                        task_type,
                        order_id,
                        TaskStatus.CANCELLED.value,
                    ),
                )
            else:
                await connection.execute(
                    "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                    "WHERE order_id = ? AND status IN (?, ?, ?, ?)",
                    (
                        TaskStatus.NEEDS_REVIEW.value,
                        reason[:500],
                        now,
                        order_id,
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                        TaskStatus.ASSIGNED.value,
                        TaskStatus.PAUSED.value,
                    ),
                )
            await connection.execute(
                "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, details, occurred_at) "
                "VALUES (?, ?, 'needs_review', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
                (
                    campaign_id,
                    order_id,
                    f"campaign:{campaign_id}:review:{now}",
                    reason[:500],
                    now,
                ),
            )
            return await self._get_order_in_transaction(connection, order_id)

    async def claim_next_task(self) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM task_queue WHERE status = ? AND run_after <= ? "
                "ORDER BY run_after, id LIMIT 1",
                (TaskStatus.QUEUED.value, now),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            task_id = int(row["id"])
            update = await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = ?, attempt_count = attempt_count + 1, "
                "last_updated = ? WHERE id = ? AND status = ?",
                (
                    TaskStatus.RUNNING.value,
                    now,
                    now,
                    task_id,
                    TaskStatus.QUEUED.value,
                ),
            )
            changed = update.rowcount == 1
            await update.close()
            if not changed:
                return None
            claimed = await connection.execute(
                "SELECT * FROM task_queue WHERE id = ?", (task_id,)
            )
            claimed_row = await claimed.fetchone()
            await claimed.close()
            return _row_dict(claimed_row)

    async def defer_task(
        self,
        task_id: int,
        run_after: str,
        reason: str,
        queue_reason: str | None = None,
    ) -> bool:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT order_id FROM task_queue WHERE id = ?", (task_id,)
            )
            task = await cursor.fetchone()
            await cursor.close()
            if task is None:
                return False
            order_id = task["order_id"]
            await connection.execute(
                "UPDATE task_queue SET status = ?, run_after = ?, locked_at = NULL, last_error = ?, "
                "last_updated = ? WHERE id = ? AND status = ?",
                (
                    TaskStatus.QUEUED.value,
                    run_after,
                    reason[:500],
                    now,
                    task_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            changed = False
            if queue_reason is not None:
                existing_cursor = await connection.execute(
                    "SELECT queue_reason FROM orders WHERE order_id = ?", (order_id,)
                )
                existing = await existing_cursor.fetchone()
                await existing_cursor.close()
                changed = (
                    existing is not None and existing["queue_reason"] != queue_reason
                )
                await connection.execute(
                    "UPDATE orders SET status = ?, queue_reason = ?, last_updated = ? "
                    "WHERE order_id = ? AND status NOT IN (?, ?, ?)",
                    (
                        OrderStatus.QUEUED.value,
                        queue_reason[:500],
                        now,
                        order_id,
                        OrderStatus.CANCELLED.value,
                        OrderStatus.COMPLETED.value,
                        OrderStatus.NEEDS_REVIEW.value,
                    ),
                )
                await connection.execute(
                    "UPDATE campaigns SET status = ?, last_updated = ? WHERE order_id = ? "
                    "AND status NOT IN (?, ?, ?)",
                    (
                        OrderStatus.QUEUED.value,
                        now,
                        order_id,
                        OrderStatus.CANCELLED.value,
                        OrderStatus.COMPLETED.value,
                        OrderStatus.NEEDS_REVIEW.value,
                    ),
                )
            return changed

    async def get_task(self, job_type: str, order_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM task_queue WHERE job_type = ? AND order_id = ?",
            (job_type, order_id),
        )

    async def assign_queued_campaign(
        self, task_id: int, order_id: str, quota_limit: int, window_seconds: int
    ) -> AssignmentResult:
        now_dt = utc_now()
        now = utc_iso(now_dt)
        cutoff = utc_iso(now_dt - timedelta(seconds=window_seconds))
        async with self.transaction() as connection:
            order_cursor = await connection.execute(
                self._order_select() + " WHERE o.order_id = ?", (order_id,)
            )
            order_row = await order_cursor.fetchone()
            await order_cursor.close()
            if order_row is None:
                return AssignmentResult(False, reason="Order no longer exists.")
            order = dict(order_row)
            if order["status"] in {
                OrderStatus.CANCELLED.value,
                OrderStatus.NEEDS_REVIEW.value,
            }:
                await connection.execute(
                    "UPDATE task_queue SET status = ?, locked_at = NULL, last_updated = ? WHERE id = ?",
                    (TaskStatus.CANCELLED.value, now, task_id),
                )
                return AssignmentResult(
                    False, order=order, reason="Order is not eligible for scheduling."
                )

            worker_cursor = await connection.execute(
                "SELECT * FROM workers WHERE enabled = 1 AND status IN (?, ?, ?) "
                "AND assigned_campaign_id IS NULL ORDER BY id",
                (
                    WorkerStatus.IDLE.value,
                    WorkerStatus.ACTIVE.value,
                    WorkerStatus.COOLDOWN.value,
                ),
            )
            candidate_rows = await worker_cursor.fetchall()
            await worker_cursor.close()

            chosen = None
            quota_next_times: list[str] = []
            for worker_row in candidate_rows:
                usage, oldest = await self._quota_usage_in_transaction(
                    connection, int(worker_row["id"]), cutoff
                )
                if usage < quota_limit:
                    chosen = dict(worker_row)
                    chosen["quota_used"] = usage
                    break
                if oldest:
                    eligible_dt = datetime.fromisoformat(oldest) + timedelta(
                        seconds=window_seconds
                    )
                    quota_next_times.append(utc_iso(eligible_dt))

            if chosen is None:
                counts_cursor = await connection.execute(
                    "SELECT status, COUNT(*) AS count FROM workers WHERE enabled = 1 GROUP BY status"
                )
                status_counts = {
                    row["status"]: int(row["count"])
                    for row in await counts_cursor.fetchall()
                }
                await counts_cursor.close()
                total_enabled = sum(status_counts.values())
                if total_enabled == 0:
                    reason = "No enabled workers are registered."
                elif candidate_rows and quota_next_times:
                    reason = (
                        "All idle workers have reached their configured action quota."
                    )
                elif status_counts.get(WorkerStatus.PROCESSING.value, 0):
                    reason = (
                        "All enabled workers are currently processing another campaign."
                    )
                else:
                    reason = "Enabled workers are paused, disabled, or require administrator review."
                next_eligible = min(quota_next_times) if quota_next_times else None
                return AssignmentResult(
                    False,
                    order=order,
                    reason=reason,
                    next_eligible_at=next_eligible,
                )

            campaign_cursor = await connection.execute(
                "SELECT * FROM campaigns WHERE order_id = ?", (order_id,)
            )
            campaign_row = await campaign_cursor.fetchone()
            await campaign_cursor.close()
            if campaign_row is None:
                return AssignmentResult(
                    False, order=order, reason="Campaign record is missing."
                )
            campaign = dict(campaign_row)
            campaign_id = int(campaign["id"])
            worker_id = int(chosen["id"])

            update_worker = await connection.execute(
                "UPDATE workers SET status = ?, assigned_campaign_id = ?, last_activity_at = ?, "
                "next_eligible_at = NULL, last_updated = ? WHERE id = ? AND enabled = 1 "
                "AND assigned_campaign_id IS NULL",
                (
                    WorkerStatus.PROCESSING.value,
                    campaign_id,
                    now,
                    now,
                    worker_id,
                ),
            )
            worker_changed = update_worker.rowcount == 1
            await update_worker.close()
            if not worker_changed:
                return AssignmentResult(
                    False, order=order, reason="Worker assignment changed concurrently."
                )

            await connection.execute(
                "UPDATE orders SET status = ?, queue_reason = NULL, status_reason = NULL, last_updated = ? "
                "WHERE order_id = ?",
                (OrderStatus.IN_PROGRESS.value, now, order_id),
            )
            await connection.execute(
                "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                (OrderStatus.IN_PROGRESS.value, now, campaign_id),
            )
            await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = NULL, last_error = NULL, last_updated = ? "
                "WHERE id = ?",
                (TaskStatus.ASSIGNED.value, now, task_id),
            )
            worker_cursor = await connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            )
            worker_row = await worker_cursor.fetchone()
            await worker_cursor.close()
            return AssignmentResult(
                True,
                worker=dict(worker_row),
                campaign=campaign,
                order=await self._get_order_in_transaction(connection, order_id),
                task={"id": task_id, "job_type": "run_campaign", "order_id": order_id},
            )

    async def _quota_usage_in_transaction(
        self,
        connection: aiosqlite.Connection,
        worker_id: int,
        cutoff: str,
    ) -> tuple[int, str | None]:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS used, MIN(occurred_at) AS oldest FROM worker_actions "
            "WHERE worker_id = ? AND status IN ('reserved', 'success', 'needs_review') "
            "AND occurred_at >= ?",
            (worker_id, cutoff),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["used"]), row["oldest"]

    async def get_quota_snapshot(
        self, worker_id: int, quota_limit: int, window_seconds: int
    ) -> QuotaSnapshot:
        now = utc_now()
        cutoff = utc_iso(now - timedelta(seconds=window_seconds))
        row = await self._fetchone(
            "SELECT COUNT(*) AS used, MIN(occurred_at) AS oldest FROM worker_actions "
            "WHERE worker_id = ? AND status IN ('reserved', 'success', 'needs_review') "
            "AND occurred_at >= ?",
            (worker_id, cutoff),
        )
        used = int(row["used"]) if row else 0
        oldest = row["oldest"] if row else None
        next_eligible = None
        if used >= quota_limit and oldest:
            next_eligible = utc_iso(
                datetime.fromisoformat(oldest) + timedelta(seconds=window_seconds)
            )
        return QuotaSnapshot(
            limit=quota_limit,
            window_seconds=window_seconds,
            used=used,
            remaining=max(0, quota_limit - used),
            next_eligible_at=next_eligible,
        )

    async def reserve_distribution_action(
        self,
        worker_id: int,
        campaign_id: int,
        order_id: str,
        quota_limit: int,
        window_seconds: int,
    ) -> int:
        now_dt = utc_now()
        now = utc_iso(now_dt)
        cutoff = utc_iso(now_dt - timedelta(seconds=window_seconds))
        event_key = f"campaign:{campaign_id}:source-announcement"
        async with self.transaction() as connection:
            worker_cursor = await connection.execute(
                "SELECT enabled, status, assigned_campaign_id FROM workers WHERE id = ?",
                (worker_id,),
            )
            worker = await worker_cursor.fetchone()
            await worker_cursor.close()
            if worker is None or not worker["enabled"]:
                raise ActionAlreadyHandled("Worker is no longer enabled")
            if (
                worker["status"] != WorkerStatus.PROCESSING.value
                or worker["assigned_campaign_id"] != campaign_id
            ):
                raise ActionAlreadyHandled(
                    "This worker is no longer assigned to that campaign"
                )

            existing_cursor = await connection.execute(
                "SELECT id, status FROM worker_actions WHERE event_key = ?",
                (event_key,),
            )
            existing = await existing_cursor.fetchone()
            await existing_cursor.close()
            if existing is not None:
                raise ActionAlreadyHandled(
                    f"This campaign distribution is already {existing['status']}"
                )

            used, oldest = await self._quota_usage_in_transaction(
                connection, worker_id, cutoff
            )
            if used >= quota_limit:
                next_eligible = None
                if oldest:
                    next_eligible = utc_iso(
                        datetime.fromisoformat(oldest)
                        + timedelta(seconds=window_seconds)
                    )
                raise QuotaExceeded(
                    f"Worker quota is exhausted; next eligible time: {next_eligible or 'unknown'}"
                )

            cursor = await connection.execute(
                "INSERT INTO worker_actions(worker_id, campaign_id, order_id, action_type, status, "
                "event_key, occurred_at) VALUES (?, ?, ?, 'source_announcement', 'reserved', ?, ?)",
                (worker_id, campaign_id, order_id, event_key, now),
            )
            action_id = int(cursor.lastrowid)
            await cursor.close()
            await connection.execute(
                "UPDATE workers SET last_activity_at = ?, last_updated = ? WHERE id = ?",
                (now, now, worker_id),
            )
            return action_id

    async def mark_distribution_success(
        self,
        action_id: int,
        worker_id: int,
        campaign_id: int,
        order_id: str,
        message_id: int,
        quota_limit: int,
        window_seconds: int,
    ) -> dict[str, Any] | None:
        now_dt = utc_now()
        now = utc_iso(now_dt)
        cutoff = utc_iso(now_dt - timedelta(seconds=window_seconds))
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE worker_actions SET status = 'success', completed_at = ? "
                "WHERE id = ? AND status = 'reserved'",
                (now, action_id),
            )
            updated = cursor.rowcount == 1
            await cursor.close()
            if not updated:
                return await self._get_order_in_transaction(connection, order_id)

            await connection.execute(
                "UPDATE campaigns SET distributed_count = distributed_count + 1, status = ?, last_updated = ? "
                "WHERE id = ?",
                (OrderStatus.IN_PROGRESS.value, now, campaign_id),
            )
            await connection.execute(
                "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, details, occurred_at) "
                "VALUES (?, ?, 'invitation_distributed', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
                (
                    campaign_id,
                    order_id,
                    f"campaign:{campaign_id}:announcement-distributed",
                    json.dumps({"source_message_id": message_id}),
                    now,
                ),
            )
            await connection.execute(
                "UPDATE orders SET status = ?, status_reason = NULL, last_updated = ? "
                "WHERE order_id = ? AND status NOT IN (?, ?)",
                (
                    OrderStatus.IN_PROGRESS.value,
                    now,
                    order_id,
                    OrderStatus.CANCELLED.value,
                    OrderStatus.COMPLETED.value,
                ),
            )
            usage, oldest = await self._quota_usage_in_transaction(
                connection, worker_id, cutoff
            )
            state_cursor = await connection.execute(
                "SELECT enabled, status FROM workers WHERE id = ?", (worker_id,)
            )
            worker_state = await state_cursor.fetchone()
            await state_cursor.close()
            next_eligible = None
            if worker_state is None or not worker_state["enabled"]:
                worker_status = WorkerStatus.DISABLED.value
            elif worker_state["status"] == WorkerStatus.PAUSED.value:
                worker_status = WorkerStatus.PAUSED.value
            elif usage >= quota_limit:
                worker_status = WorkerStatus.COOLDOWN.value
                if oldest:
                    next_eligible = utc_iso(
                        datetime.fromisoformat(oldest)
                        + timedelta(seconds=window_seconds)
                    )
            else:
                worker_status = WorkerStatus.IDLE.value
            await connection.execute(
                "UPDATE workers SET status = ?, assigned_campaign_id = NULL, last_activity_at = ?, "
                "daily_action_count = ?, next_eligible_at = ?, last_updated = ? WHERE id = ?",
                (worker_status, now, usage, next_eligible, now, worker_id),
            )
            await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = NULL, last_error = NULL, last_updated = ? "
                "WHERE job_type = 'run_campaign' AND order_id = ?",
                (TaskStatus.COMPLETED.value, now, order_id),
            )
            return await self._get_order_in_transaction(connection, order_id)

    async def mark_distribution_failed(
        self,
        action_id: int,
        worker_id: int,
        campaign_id: int,
        order_id: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE worker_actions SET status = 'failed', completed_at = ?, error_code = ?, error_message = ? "
                "WHERE id = ? AND status = 'reserved'",
                (now, error_code[:80], error_message[:500], action_id),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            if changed:
                await connection.execute(
                    "UPDATE campaigns SET failed_actions = failed_actions + 1, status = ?, last_updated = ? "
                    "WHERE id = ?",
                    (OrderStatus.NEEDS_REVIEW.value, now, campaign_id),
                )
                await connection.execute(
                    "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? WHERE order_id = ?",
                    (
                        OrderStatus.NEEDS_REVIEW.value,
                        error_message[:500],
                        now,
                        order_id,
                    ),
                )
                await connection.execute(
                    "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                    "WHERE job_type = 'run_campaign' AND order_id = ?",
                    (TaskStatus.NEEDS_REVIEW.value, error_message[:500], now, order_id),
                )
                await self._add_worker_error_in_transaction(
                    connection, worker_id, error_code, error_message, now
                )
                await connection.execute(
                    "UPDATE workers SET status = ?, last_activity_at = ?, last_updated = ? WHERE id = ?",
                    (WorkerStatus.NEEDS_ATTENTION.value, now, now, worker_id),
                )
            return await self._get_order_in_transaction(connection, order_id)

    async def mark_distribution_uncertain(
        self,
        action_id: int,
        worker_id: int,
        campaign_id: int,
        order_id: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        """Conservatively hold a quota slot when delivery may have succeeded."""
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE worker_actions SET status = 'needs_review', completed_at = ?, error_code = ?, "
                "error_message = ? WHERE id = ? AND status = 'reserved'",
                (now, "delivery_uncertain", error_message[:500], action_id),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            if changed:
                await connection.execute(
                    "UPDATE campaigns SET failed_actions = failed_actions + 1, status = ?, last_updated = ? "
                    "WHERE id = ?",
                    (OrderStatus.NEEDS_REVIEW.value, now, campaign_id),
                )
                await connection.execute(
                    "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? WHERE order_id = ?",
                    (
                        OrderStatus.NEEDS_REVIEW.value,
                        error_message[:500],
                        now,
                        order_id,
                    ),
                )
                await connection.execute(
                    "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                    "WHERE job_type = 'run_campaign' AND order_id = ?",
                    (TaskStatus.NEEDS_REVIEW.value, error_message[:500], now, order_id),
                )
                await self._add_worker_error_in_transaction(
                    connection, worker_id, "delivery_uncertain", error_message, now
                )
                await connection.execute(
                    "UPDATE workers SET status = ?, last_activity_at = ?, last_updated = ? WHERE id = ?",
                    (WorkerStatus.NEEDS_ATTENTION.value, now, now, worker_id),
                )
            return await self._get_order_in_transaction(connection, order_id)

    async def mark_campaign_for_review(
        self, worker_id: int, campaign_id: int, order_id: str, reason: str
    ) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            order_cursor = await connection.execute(
                "SELECT status FROM orders WHERE order_id = ?", (order_id,)
            )
            current_order = await order_cursor.fetchone()
            await order_cursor.close()
            if current_order is None or current_order["status"] in {
                OrderStatus.CANCELLED.value,
                OrderStatus.COMPLETED.value,
            }:
                return await self._get_order_in_transaction(connection, order_id)
            await connection.execute(
                "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? WHERE order_id = ?",
                (OrderStatus.NEEDS_REVIEW.value, reason[:500], now, order_id),
            )
            await connection.execute(
                "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                (OrderStatus.NEEDS_REVIEW.value, now, campaign_id),
            )
            await connection.execute(
                "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                "WHERE job_type = 'run_campaign' AND order_id = ?",
                (TaskStatus.NEEDS_REVIEW.value, reason[:500], now, order_id),
            )
            await self._add_worker_error_in_transaction(
                connection, worker_id, "worker_review", reason, now
            )
            await connection.execute(
                "INSERT INTO worker_actions(worker_id, campaign_id, order_id, action_type, status, "
                "event_key, occurred_at, completed_at, error_code, error_message) "
                "VALUES (?, ?, ?, 'worker_review', 'failed', ?, ?, ?, 'worker_review', ?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (
                    worker_id,
                    campaign_id,
                    order_id,
                    f"worker-review:{worker_id}:{campaign_id}:{now}",
                    now,
                    now,
                    reason[:500],
                ),
            )
            await connection.execute(
                "UPDATE workers SET status = ?, last_activity_at = ?, last_updated = ? WHERE id = ?",
                (WorkerStatus.NEEDS_ATTENTION.value, now, now, worker_id),
            )
            return await self._get_order_in_transaction(connection, order_id)

    async def _add_worker_error_in_transaction(
        self,
        connection: aiosqlite.Connection,
        worker_id: int,
        error_code: str,
        error_message: str,
        now: str,
    ) -> None:
        cursor = await connection.execute(
            "SELECT error_history FROM workers WHERE id = ?", (worker_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        history: list[dict[str, str]] = []
        if row is not None:
            try:
                history = json.loads(row["error_history"])
            except (json.JSONDecodeError, TypeError):
                history = []
        history.append(
            {"at": now, "code": error_code[:80], "message": error_message[:500]}
        )
        await connection.execute(
            "UPDATE workers SET error_history = ? WHERE id = ?",
            (json.dumps(history[-20:]), worker_id),
        )

    async def activate_worker(self, telegram_user_id: int) -> dict[str, Any] | None:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE workers SET last_activity_at = ?, status = CASE "
                "WHEN enabled = 1 AND status = ? THEN ? ELSE status END, last_updated = ? "
                "WHERE telegram_user_id = ?",
                (
                    now,
                    WorkerStatus.IDLE.value,
                    WorkerStatus.ACTIVE.value,
                    now,
                    telegram_user_id,
                ),
            )
            cursor = await connection.execute(
                "SELECT * FROM workers WHERE telegram_user_id = ?", (telegram_user_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            return _row_dict(row)

    async def get_worker(self, worker_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM workers WHERE id = ?", (worker_id,))

    async def get_worker_for_user(self, telegram_user_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM workers WHERE telegram_user_id = ?", (telegram_user_id,)
        )

    async def list_workers(
        self, quota_limit: int, window_seconds: int
    ) -> list[dict[str, Any]]:
        now = utc_now()
        cutoff = utc_iso(now - timedelta(seconds=window_seconds))
        async with self._lock:
            connection = self._conn()
            cursor = await connection.execute("SELECT * FROM workers ORDER BY id")
            workers = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()
            for worker in workers:
                used, oldest = await self._quota_usage_in_transaction(
                    connection, int(worker["id"]), cutoff
                )
                worker["quota_used"] = used
                worker["quota_remaining"] = max(0, quota_limit - used)
                worker["next_eligible_at"] = None
                if used >= quota_limit and oldest:
                    worker["next_eligible_at"] = utc_iso(
                        datetime.fromisoformat(oldest)
                        + timedelta(seconds=window_seconds)
                    )
                if not worker["enabled"]:
                    worker["status"] = WorkerStatus.DISABLED.value
                elif (
                    worker["status"] == WorkerStatus.COOLDOWN.value
                    and used < quota_limit
                ):
                    worker["status"] = WorkerStatus.IDLE.value
            return workers

    async def register_worker(
        self, telegram_user_id: int, display_name: str
    ) -> dict[str, Any]:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO workers(telegram_user_id, display_name, enabled, status, created_at, last_updated) "
                "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "display_name = excluded.display_name, enabled = 1, "
                "status = CASE WHEN workers.status = ? THEN ? ELSE workers.status END, last_updated = excluded.last_updated",
                (
                    telegram_user_id,
                    display_name,
                    WorkerStatus.IDLE.value,
                    now,
                    now,
                    WorkerStatus.DISABLED.value,
                    WorkerStatus.IDLE.value,
                ),
            )
            cursor = await connection.execute(
                "SELECT * FROM workers WHERE telegram_user_id = ?", (telegram_user_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row)

    async def set_worker_paused(
        self, worker_id: int, reason: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        now = utc_iso()
        async with self.transaction() as connection:
            worker_cursor = await connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            )
            worker_row = await worker_cursor.fetchone()
            await worker_cursor.close()
            if worker_row is None:
                return None, None
            worker = dict(worker_row)
            if (
                not worker["enabled"]
                or worker["status"] == WorkerStatus.NEEDS_ATTENTION.value
            ):
                return worker, None
            campaign_id = worker["assigned_campaign_id"]
            order = None
            if campaign_id is not None:
                in_flight = await connection.execute(
                    "SELECT 1 FROM worker_actions WHERE worker_id = ? AND status = 'reserved' LIMIT 1",
                    (worker_id,),
                )
                reserved = await in_flight.fetchone()
                await in_flight.close()
                if reserved is not None:
                    worker["action_in_flight"] = True
                    return worker, None
                campaign_cursor = await connection.execute(
                    "SELECT order_id FROM campaigns WHERE id = ?", (campaign_id,)
                )
                campaign_row = await campaign_cursor.fetchone()
                await campaign_cursor.close()
                if campaign_row:
                    order_id = campaign_row["order_id"]
                    await connection.execute(
                        "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? "
                        "WHERE order_id = ? AND status NOT IN (?, ?, ?)",
                        (
                            OrderStatus.PAUSED.value,
                            reason[:500],
                            now,
                            order_id,
                            OrderStatus.CANCELLED.value,
                            OrderStatus.COMPLETED.value,
                            OrderStatus.NEEDS_REVIEW.value,
                        ),
                    )
                    await connection.execute(
                        "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                        (OrderStatus.PAUSED.value, now, campaign_id),
                    )
                    await connection.execute(
                        "UPDATE task_queue SET status = ?, last_updated = ? WHERE order_id = ? "
                        "AND job_type = 'run_campaign' AND status = ?",
                        (
                            TaskStatus.PAUSED.value,
                            now,
                            order_id,
                            TaskStatus.ASSIGNED.value,
                        ),
                    )
                    order = await self._get_order_in_transaction(connection, order_id)
            await connection.execute(
                "UPDATE workers SET status = ?, pause_reason = ?, last_updated = ? WHERE id = ?",
                (WorkerStatus.PAUSED.value, reason[:500], now, worker_id),
            )
            cursor = await connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            )
            updated = await cursor.fetchone()
            await cursor.close()
            return dict(updated), order

    async def resume_worker(
        self, worker_id: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            )
            worker_row = await cursor.fetchone()
            await cursor.close()
            if worker_row is None:
                return None, None
            worker = dict(worker_row)
            if worker["status"] not in {
                WorkerStatus.PAUSED.value,
                WorkerStatus.COOLDOWN.value,
                WorkerStatus.NEEDS_ATTENTION.value,
            }:
                return worker, None
            if not worker["enabled"]:
                return worker, None
            if (
                worker["status"] == WorkerStatus.NEEDS_ATTENTION.value
                and worker["assigned_campaign_id"] is not None
            ):
                return worker, None
            order = None
            campaign_id = worker["assigned_campaign_id"]
            if campaign_id is not None:
                campaign_cursor = await connection.execute(
                    "SELECT order_id FROM campaigns WHERE id = ?", (campaign_id,)
                )
                campaign_row = await campaign_cursor.fetchone()
                await campaign_cursor.close()
                if campaign_row:
                    order_id = campaign_row["order_id"]
                    order_cursor = await connection.execute(
                        "SELECT status FROM orders WHERE order_id = ?", (order_id,)
                    )
                    order_row = await order_cursor.fetchone()
                    await order_cursor.close()
                    if (
                        order_row is None
                        or order_row["status"] != OrderStatus.PAUSED.value
                    ):
                        return worker, None
                    await connection.execute(
                        "UPDATE orders SET status = ?, status_reason = NULL, last_updated = ? WHERE order_id = ?",
                        (OrderStatus.IN_PROGRESS.value, now, order_id),
                    )
                    await connection.execute(
                        "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                        (OrderStatus.IN_PROGRESS.value, now, campaign_id),
                    )
                    await connection.execute(
                        "UPDATE task_queue SET status = ?, last_updated = ? WHERE order_id = ? "
                        "AND job_type = 'run_campaign' AND status = ?",
                        (
                            TaskStatus.ASSIGNED.value,
                            now,
                            order_id,
                            TaskStatus.PAUSED.value,
                        ),
                    )
                    order = await self._get_order_in_transaction(connection, order_id)
                    await connection.execute(
                        "UPDATE workers SET status = ?, pause_reason = NULL, last_activity_at = ?, last_updated = ? WHERE id = ?",
                        (WorkerStatus.PROCESSING.value, now, now, worker_id),
                    )
                    worker["status"] = WorkerStatus.PROCESSING.value
                    return worker, order
            await connection.execute(
                "UPDATE workers SET status = ?, pause_reason = NULL, next_eligible_at = NULL, last_updated = ? WHERE id = ?",
                (WorkerStatus.IDLE.value, now, worker_id),
            )
            worker["status"] = WorkerStatus.IDLE.value
            return worker, None

    async def disable_worker(
        self, worker_id: int, reason: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            )
            worker_row = await cursor.fetchone()
            await cursor.close()
            if worker_row is None:
                return None, None
            worker = dict(worker_row)
            order = None
            campaign_id = worker["assigned_campaign_id"]
            if campaign_id is not None:
                in_flight = await connection.execute(
                    "SELECT 1 FROM worker_actions WHERE worker_id = ? AND status = 'reserved' LIMIT 1",
                    (worker_id,),
                )
                reserved = await in_flight.fetchone()
                await in_flight.close()
                if reserved is not None:
                    worker["action_in_flight"] = True
                    return worker, None
                campaign_cursor = await connection.execute(
                    "SELECT order_id FROM campaigns WHERE id = ?", (campaign_id,)
                )
                campaign_row = await campaign_cursor.fetchone()
                await campaign_cursor.close()
                if campaign_row:
                    order_id = campaign_row["order_id"]
                    await connection.execute(
                        "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? "
                        "WHERE order_id = ? AND status NOT IN (?, ?)",
                        (
                            OrderStatus.NEEDS_REVIEW.value,
                            reason[:500],
                            now,
                            order_id,
                            OrderStatus.CANCELLED.value,
                            OrderStatus.COMPLETED.value,
                        ),
                    )
                    await connection.execute(
                        "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                        (OrderStatus.NEEDS_REVIEW.value, now, campaign_id),
                    )
                    await connection.execute(
                        "UPDATE task_queue SET status = ?, last_error = ?, last_updated = ? WHERE order_id = ? "
                        "AND job_type = 'run_campaign' AND status IN (?, ?)",
                        (
                            TaskStatus.NEEDS_REVIEW.value,
                            reason[:500],
                            now,
                            order_id,
                            TaskStatus.ASSIGNED.value,
                            TaskStatus.PAUSED.value,
                        ),
                    )
                    order = await self._get_order_in_transaction(connection, order_id)
            await connection.execute(
                "UPDATE workers SET enabled = 0, status = ?, pause_reason = ?, assigned_campaign_id = NULL, "
                "last_updated = ? WHERE id = ?",
                (WorkerStatus.DISABLED.value, reason[:500], now, worker_id),
            )
            worker["enabled"] = 0
            worker["status"] = WorkerStatus.DISABLED.value
            return worker, order

    async def update_worker_display_name(
        self, worker_id: int, display_name: str
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE workers SET display_name = ?, last_updated = ? WHERE id = ?",
                (display_name[:80], utc_iso(), worker_id),
            )

    async def list_assigned_tasks(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT t.id AS task_id, t.order_id, w.id AS worker_id, w.telegram_user_id, "
            "w.display_name, c.id AS campaign_id FROM task_queue t "
            "JOIN campaigns c ON c.order_id = t.order_id "
            "JOIN workers w ON w.assigned_campaign_id = c.id "
            "WHERE t.job_type = 'run_campaign' AND t.status = ? AND w.enabled = 1 "
            "AND w.status = ? ORDER BY t.id",
            (TaskStatus.ASSIGNED.value, WorkerStatus.PROCESSING.value),
        )

    async def get_campaign(self, campaign_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT c.*, o.customer_id, o.source_group_id, o.destination_group_id, "
            "o.source_ref, o.destination_ref, o.status AS order_status, o.order_id "
            "FROM campaigns c JOIN orders o ON o.order_id = c.order_id WHERE c.id = ?",
            (campaign_id,),
        )

    async def find_campaign_for_invite(
        self, destination_chat_id: int, invite_link: str
    ) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT c.id AS campaign_id, c.order_id, c.requested_size, c.status AS campaign_status, "
            "o.customer_id, o.status AS order_status FROM invite_links i "
            "JOIN campaigns c ON c.id = i.campaign_id JOIN orders o ON o.order_id = c.order_id "
            "WHERE i.destination_chat_id = ? AND i.invite_link = ? "
            "AND (i.revoked_at IS NULL OR o.status = ?)",
            (destination_chat_id, invite_link, OrderStatus.COMPLETED.value),
        )

    async def record_confirmed_join(
        self,
        campaign_id: int,
        order_id: str,
        subject_hash: str,
        requested_size: int,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        now = utc_iso()
        event_key = f"join:{campaign_id}:{subject_hash}"
        async with self.transaction() as connection:
            campaign_cursor = await connection.execute(
                "SELECT status, confirmed_join_count FROM campaigns WHERE id = ?",
                (campaign_id,),
            )
            campaign = await campaign_cursor.fetchone()
            await campaign_cursor.close()
            if campaign is None or campaign["status"] == OrderStatus.CANCELLED.value:
                return (
                    await self._get_order_in_transaction(connection, order_id),
                    False,
                    False,
                )
            cursor = await connection.execute(
                "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, subject_hash, occurred_at) "
                "VALUES (?, ?, 'join_confirmed', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
                (campaign_id, order_id, event_key, subject_hash, now),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
            if not inserted:
                return (
                    await self._get_order_in_transaction(connection, order_id),
                    False,
                    False,
                )

            already_completed = campaign["status"] == OrderStatus.COMPLETED.value
            new_count = int(campaign["confirmed_join_count"]) + 1
            completed_now = not already_completed and new_count >= requested_size
            if already_completed or completed_now:
                new_status = OrderStatus.COMPLETED.value
            elif campaign["status"] in {
                OrderStatus.PAUSED.value,
                OrderStatus.NEEDS_REVIEW.value,
            }:
                new_status = campaign["status"]
            else:
                new_status = OrderStatus.IN_PROGRESS.value
            await connection.execute(
                "UPDATE campaigns SET confirmed_join_count = ?, status = ?, last_updated = ? WHERE id = ?",
                (new_count, new_status, now, campaign_id),
            )
            await connection.execute(
                "UPDATE orders SET status = ?, status_reason = NULL, last_updated = ? WHERE order_id = ?",
                (new_status, now, order_id),
            )
            if completed_now:
                await connection.execute(
                    "UPDATE invite_links SET revoked_at = ? WHERE campaign_id = ? AND revoked_at IS NULL",
                    (now, campaign_id),
                )
                await connection.execute(
                    "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, details, occurred_at) "
                    "VALUES (?, ?, 'completed', ?, ?, ?) ON CONFLICT(event_key) DO NOTHING",
                    (
                        campaign_id,
                        order_id,
                        f"campaign:{campaign_id}:completed",
                        f"Verified target reached: {new_count} confirmed joins.",
                        now,
                    ),
                )
            return (
                await self._get_order_in_transaction(connection, order_id),
                True,
                completed_now,
            )

    async def cancel_order(
        self, order_id: str, requester_id: int, is_admin: bool = False
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        now = utc_iso()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT o.*, c.id AS campaign_id, c.invite_link FROM orders o "
                "JOIN campaigns c ON c.order_id = o.order_id WHERE o.order_id = ?",
                (order_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None, "not_found", None
            order = dict(row)
            if not is_admin and order["customer_id"] != requester_id:
                return None, "forbidden", None
            if order["status"] in {
                OrderStatus.CANCELLED.value,
                OrderStatus.COMPLETED.value,
            }:
                return order, "already_final", order["invite_link"]
            in_flight = await connection.execute(
                "SELECT 1 FROM worker_actions WHERE campaign_id = ? AND status = 'reserved'",
                (order["campaign_id"],),
            )
            reserved = await in_flight.fetchone()
            await in_flight.close()
            if reserved is not None:
                return order, "in_flight", order["invite_link"]

            await connection.execute(
                "UPDATE orders SET status = ?, status_reason = 'Cancelled by an authorized user', "
                "queue_reason = NULL, last_updated = ? WHERE order_id = ?",
                (OrderStatus.CANCELLED.value, now, order_id),
            )
            await connection.execute(
                "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                (OrderStatus.CANCELLED.value, now, order["campaign_id"]),
            )
            await connection.execute(
                "UPDATE invite_links SET revoked_at = ? WHERE campaign_id = ? AND revoked_at IS NULL",
                (now, order["campaign_id"]),
            )
            await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = NULL, last_updated = ? WHERE order_id = ? "
                "AND status IN (?, ?, ?, ?, ?)",
                (
                    TaskStatus.CANCELLED.value,
                    now,
                    order_id,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.ASSIGNED.value,
                    TaskStatus.PAUSED.value,
                    TaskStatus.NEEDS_REVIEW.value,
                ),
            )
            worker_cursor = await connection.execute(
                "SELECT id FROM workers WHERE assigned_campaign_id = ?",
                (order["campaign_id"],),
            )
            worker_rows = await worker_cursor.fetchall()
            await worker_cursor.close()
            for worker_row in worker_rows:
                await connection.execute(
                    "UPDATE workers SET assigned_campaign_id = NULL, status = CASE "
                    "WHEN enabled = 0 THEN ? WHEN status IN (?, ?, ?) THEN status ELSE ? END, "
                    "pause_reason = CASE WHEN status = ? THEN pause_reason ELSE NULL END, last_updated = ? "
                    "WHERE id = ?",
                    (
                        WorkerStatus.DISABLED.value,
                        WorkerStatus.PAUSED.value,
                        WorkerStatus.NEEDS_ATTENTION.value,
                        WorkerStatus.COOLDOWN.value,
                        WorkerStatus.IDLE.value,
                        WorkerStatus.PAUSED.value,
                        now,
                        worker_row["id"],
                    ),
                )
            await connection.execute(
                "INSERT INTO campaign_events(campaign_id, order_id, event_type, event_key, details, occurred_at) "
                "VALUES (?, ?, 'cancelled', ?, 'Order cancelled by an authorized user', ?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (
                    order["campaign_id"],
                    order_id,
                    f"campaign:{order['campaign_id']}:cancelled",
                    now,
                ),
            )
            return (
                await self._get_order_in_transaction(connection, order_id),
                "cancelled",
                order["invite_link"],
            )

    async def wake_queued_campaigns(self) -> None:
        now = utc_iso()
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE task_queue SET run_after = ?, last_updated = ? "
                "WHERE job_type = 'run_campaign' AND status = ?",
                (now, now, TaskStatus.QUEUED.value),
            )

    async def get_setting(self, key: str, default: str) -> str:
        row = await self._fetchone(
            "SELECT setting_value FROM application_settings WHERE setting_key = ?",
            (key,),
        )
        return str(row["setting_value"]) if row else default

    async def get_or_create_join_hash_key(self) -> str:
        """Keep join pseudonyms stable without persisting raw Telegram user IDs."""
        setting_key = "privacy_join_hash_key"
        current = await self.get_setting(setting_key, "")
        if current:
            return current
        await self.seed_setting(setting_key, secrets.token_hex(32))
        return await self.get_setting(setting_key, "")

    async def set_setting(self, key: str, value: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO application_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value, updated_at = excluded.updated_at",
                (key, value, utc_iso()),
            )

    async def seed_setting(self, key: str, value: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "INSERT INTO application_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(setting_key) DO NOTHING",
                (key, value, utc_iso()),
            )

    async def refresh_worker_cooldowns(
        self, quota_limit: int, window_seconds: int
    ) -> None:
        now_dt = utc_now()
        now = utc_iso(now_dt)
        cutoff = utc_iso(now_dt - timedelta(seconds=window_seconds))
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id, enabled, status FROM workers WHERE status IN (?, ?, ?)",
                (
                    WorkerStatus.COOLDOWN.value,
                    WorkerStatus.IDLE.value,
                    WorkerStatus.ACTIVE.value,
                ),
            )
            workers = await cursor.fetchall()
            await cursor.close()
            for worker in workers:
                used, oldest = await self._quota_usage_in_transaction(
                    connection, int(worker["id"]), cutoff
                )
                if not worker["enabled"]:
                    new_status = WorkerStatus.DISABLED.value
                    next_eligible = None
                elif used < quota_limit:
                    new_status = (
                        WorkerStatus.IDLE.value
                        if worker["status"] == WorkerStatus.COOLDOWN.value
                        else worker["status"]
                    )
                    next_eligible = None
                else:
                    new_status = WorkerStatus.COOLDOWN.value
                    next_eligible = None
                    if oldest:
                        next_eligible = utc_iso(
                            datetime.fromisoformat(oldest)
                            + timedelta(seconds=window_seconds)
                        )
                await connection.execute(
                    "UPDATE workers SET status = ?, next_eligible_at = ?, daily_action_count = ?, last_updated = ? "
                    "WHERE id = ?",
                    (new_status, next_eligible, used, now, worker["id"]),
                )

    async def recover_after_restart(self) -> list[str]:
        """Requeue side-effect-free validation jobs; flag ambiguous sends for review."""
        now = utc_iso()
        needs_review: list[str] = []
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id, order_id, job_type FROM task_queue WHERE status = ?",
                (TaskStatus.RUNNING.value,),
            )
            running = await cursor.fetchall()
            await cursor.close()
            # Claiming a task has no external side effect; replay it after restart.
            for task in running:
                await connection.execute(
                    "UPDATE task_queue SET status = ?, locked_at = NULL, run_after = ?, last_updated = ? "
                    "WHERE id = ?",
                    (TaskStatus.QUEUED.value, now, now, task["id"]),
                )

            # A reserved Bot API send may have succeeded before the process died.
            # Do not retry or rotate workers; retain the quota reservation and ask
            # an administrator to reconcile the result.
            stale_cursor = await connection.execute(
                "SELECT id, worker_id, campaign_id, order_id FROM worker_actions "
                "WHERE status = 'reserved'",
            )
            stale_actions = await stale_cursor.fetchall()
            await stale_cursor.close()
            for action in stale_actions:
                needs_review.append(action["order_id"])
                await connection.execute(
                    "UPDATE worker_actions SET status = 'needs_review', completed_at = ?, error_code = ?, "
                    "error_message = ? WHERE id = ? AND status = 'reserved'",
                    (
                        now,
                        "restart_uncertain",
                        "Distribution result is unknown after restart.",
                        action["id"],
                    ),
                )
                await connection.execute(
                    "UPDATE orders SET status = ?, status_reason = ?, last_updated = ? WHERE order_id = ? "
                    "AND status NOT IN (?, ?)",
                    (
                        OrderStatus.NEEDS_REVIEW.value,
                        "Distribution result is unknown after restart; administrator review is required.",
                        now,
                        action["order_id"],
                        OrderStatus.CANCELLED.value,
                        OrderStatus.COMPLETED.value,
                    ),
                )
                await connection.execute(
                    "UPDATE campaigns SET status = ?, last_updated = ? WHERE id = ?",
                    (OrderStatus.NEEDS_REVIEW.value, now, action["campaign_id"]),
                )
                await connection.execute(
                    "UPDATE task_queue SET status = ?, last_error = ?, locked_at = NULL, last_updated = ? "
                    "WHERE job_type = 'run_campaign' AND order_id = ?",
                    (
                        TaskStatus.NEEDS_REVIEW.value,
                        "Distribution result is unknown after restart.",
                        now,
                        action["order_id"],
                    ),
                )
                await connection.execute(
                    "UPDATE workers SET status = ?, last_updated = ? WHERE id = ?",
                    (WorkerStatus.NEEDS_ATTENTION.value, now, action["worker_id"]),
                )
            return sorted(set(needs_review))

    async def mark_invite_revoked(self, campaign_id: int) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE invite_links SET revoked_at = COALESCE(revoked_at, ?) WHERE campaign_id = ?",
                (utc_iso(), campaign_id),
            )

    async def statistics(self, customer_id: int | None = None) -> dict[str, int]:
        async with self._lock:
            connection = self._conn()
            if customer_id is None:
                order_filter = ""
                params: tuple = ()
            else:
                order_filter = "WHERE customer_id = ?"
                params = (customer_id,)
            orders_cursor = await connection.execute(
                f"SELECT status, COUNT(*) AS total FROM orders {order_filter} GROUP BY status",
                params,
            )
            order_counts = {
                row["status"]: int(row["total"])
                for row in await orders_cursor.fetchall()
            }
            await orders_cursor.close()
            if customer_id is None:
                campaign_query = (
                    "SELECT COALESCE(SUM(c.generated_count), 0) AS generated, "
                    "COALESCE(SUM(c.distributed_count), 0) AS distributed, "
                    "COALESCE(SUM(c.confirmed_join_count), 0) AS joins, "
                    "COALESCE(SUM(c.failed_actions), 0) AS failures FROM campaigns c"
                )
                campaign_params: tuple = ()
            else:
                campaign_query = (
                    "SELECT COALESCE(SUM(c.generated_count), 0) AS generated, "
                    "COALESCE(SUM(c.distributed_count), 0) AS distributed, "
                    "COALESCE(SUM(c.confirmed_join_count), 0) AS joins, "
                    "COALESCE(SUM(c.failed_actions), 0) AS failures FROM campaigns c "
                    "JOIN orders o ON o.order_id = c.order_id WHERE o.customer_id = ?"
                )
                campaign_params = (customer_id,)
            campaign_cursor = await connection.execute(campaign_query, campaign_params)
            campaign = await campaign_cursor.fetchone()
            await campaign_cursor.close()
            worker_total = 0
            queue_total = 0
            if customer_id is None:
                worker_cursor = await connection.execute(
                    "SELECT COUNT(*) AS total FROM workers WHERE enabled = 1"
                )
                worker_total = int((await worker_cursor.fetchone())["total"])
                await worker_cursor.close()
                task_cursor = await connection.execute(
                    "SELECT COUNT(*) AS total FROM task_queue WHERE status IN (?, ?, ?) ",
                    (
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                        TaskStatus.ASSIGNED.value,
                    ),
                )
                queue_total = int((await task_cursor.fetchone())["total"])
                await task_cursor.close()
            return {
                "orders_total": sum(order_counts.values()),
                "orders_pending": order_counts.get(OrderStatus.PENDING.value, 0)
                + order_counts.get(OrderStatus.VALIDATING.value, 0)
                + order_counts.get(OrderStatus.QUEUED.value, 0),
                "orders_in_progress": order_counts.get(
                    OrderStatus.IN_PROGRESS.value, 0
                ),
                "orders_completed": order_counts.get(OrderStatus.COMPLETED.value, 0),
                "orders_needs_review": order_counts.get(
                    OrderStatus.NEEDS_REVIEW.value, 0
                ),
                "generated": int(campaign["generated"]),
                "distributed": int(campaign["distributed"]),
                "joins": int(campaign["joins"]),
                "failures": int(campaign["failures"]),
                "workers_enabled": worker_total,
                "queue_active": queue_total,
            }

    async def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT a.id, a.worker_id, w.display_name, a.order_id, a.action_type, a.status, "
            "a.error_code, a.error_message, a.completed_at FROM worker_actions a "
            "JOIN workers w ON w.id = a.worker_id WHERE a.status IN ('failed', 'needs_review') "
            "ORDER BY a.completed_at DESC LIMIT ?",
            (limit,),
        )

    async def recover_pending_tasks_for_cancelled_orders(self) -> None:
        """Keep stale queued jobs from resurrecting cancelled orders."""
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE task_queue SET status = ?, locked_at = NULL, last_updated = ? "
                "WHERE status IN (?, ?, ?) AND order_id IN (SELECT order_id FROM orders WHERE status = ?)",
                (
                    TaskStatus.CANCELLED.value,
                    utc_iso(),
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.ASSIGNED.value,
                    OrderStatus.CANCELLED.value,
                ),
            )
