"""Inactivity expiry: unreleased work nobody has opened for a while is deleted.

A project or movie (scene flights are movies) remembers when its owner last
opened or worked on it. Once that is `RetentionPolicy.days` ago it is deleted
through the same purge as a manual delete, unless it has a release (released
work is kept). Starting work counts as opening, so anything still marked as
running on an item idle this long is stale state; the purge cancels it. The studio warns during the last
`warning_days`. Anything not yet tracked starts a full period when the sweep
first sees it, so turning this on never deletes existing work early.

The sweep claims an item against its stale timestamp before deleting it, and
owner access to a claimed item is refused (`ItemExpiring`). An owner request
that recorded its open first keeps the item; one that arrives later cannot open
it underneath the deletion.

The sweep runs from an hourly Cloud Scheduler call to the API; it also purges
prompt-refusal records past their retention.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, Protocol

import asyncpg

from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

logger = logging.getLogger(__name__)

PROJECT = "project"
MOVIE = "movie"
DEFAULT_DAYS = 90
DEFAULT_WARNING_DAYS = 14
# Opening a project many times an hour records it once.
TOUCH_INTERVAL = timedelta(hours=1)
# One sweep tries at most this many items of each kind; the next hour continues.
SWEEP_LIMIT = 50
# A deletion that fails is retried after this long, behind the items still waiting.
FAILED_RETRY = timedelta(days=1)
# A claim older than this was left by a sweep that died: twice the API's
# 900-second request timeout, so the next hourly sweep retakes it, while a
# duplicate scheduler delivery running alongside never shares an item.
CLAIM_LEASE = timedelta(minutes=30)


@dataclass(frozen=True)
class RetentionPolicy:
    days: int = DEFAULT_DAYS
    warning_days: int = DEFAULT_WARNING_DAYS

    @property
    def enabled(self) -> bool:
        return self.days > 0

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> RetentionPolicy:
        """MAGIC_VIDEO_EDITOR_RETENTION_DAYS (0 turns expiry off) and
        MAGIC_VIDEO_EDITOR_RETENTION_WARNING_DAYS."""
        days = int(values.get("MAGIC_VIDEO_EDITOR_RETENTION_DAYS") or DEFAULT_DAYS)
        warning = int(values.get("MAGIC_VIDEO_EDITOR_RETENTION_WARNING_DAYS") or DEFAULT_WARNING_DAYS)
        if days < 0 or warning < 0 or (days and warning > days):
            raise RuntimeError("retention days must be >= 0 and the warning window within them")
        return cls(days, warning)


class ItemExpiring(Exception):
    """The owner opened an item the sweep is deleting for inactivity; a 410."""

    code = "item_expiring"

    def __init__(self, days: int) -> None:
        super().__init__(f"This is being deleted: nobody opened it for {days} days.")


class ActivityStore(Protocol):
    async def touch(self, kind: str, item_id: str, owner_id: str, at: datetime) -> bool:
        """Record an open; False if the item is claimed for expiry."""
        ...

    async def claim(self, kind: str, item_id: str, cutoff: datetime, at: datetime) -> bool:
        """Mark the item as being expired if it was last active before cutoff
        and no live claim (younger than CLAIM_LEASE) holds it."""
        ...

    async def release(self, kind: str, item_id: str, at: datetime) -> None:
        """Clear a claim, recording activity at `at` (or later, if newer)."""
        ...

    async def last_active(self, kind: str, item_ids: Iterable[str]) -> dict[str, datetime]: ...

    async def tracked(self, kind: str) -> dict[str, datetime]: ...

    async def enrol(self, kind: str, items: Iterable[tuple[str, str]], at: datetime) -> None: ...

    async def forget(self, kind: str, item_ids: Iterable[str]) -> None: ...


class MemoryActivityStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, datetime]] = {}
        self.claimed: dict[tuple[str, str], datetime] = {}

    async def touch(self, kind: str, item_id: str, owner_id: str, at: datetime) -> bool:
        if (kind, item_id) in self.claimed:
            return False
        previous = self.rows.get((kind, item_id))
        self.rows[(kind, item_id)] = (owner_id, max(at, previous[1]) if previous else at)
        return True

    async def claim(self, kind: str, item_id: str, cutoff: datetime, at: datetime) -> bool:
        row = self.rows.get((kind, item_id))
        if row is None or row[1] >= cutoff:
            return False
        held = self.claimed.get((kind, item_id))
        if held is not None and held >= at - CLAIM_LEASE:
            return False
        self.claimed[(kind, item_id)] = at
        return True

    async def release(self, kind: str, item_id: str, at: datetime) -> None:
        self.claimed.pop((kind, item_id), None)
        row = self.rows.get((kind, item_id))
        if row is not None:
            self.rows[(kind, item_id)] = (row[0], max(at, row[1]))

    async def last_active(self, kind: str, item_ids: Iterable[str]) -> dict[str, datetime]:
        return {i: self.rows[(kind, i)][1] for i in item_ids if (kind, i) in self.rows}

    async def tracked(self, kind: str) -> dict[str, datetime]:
        return {item_id: at for (k, item_id), (_, at) in self.rows.items() if k == kind}

    async def enrol(self, kind: str, items: Iterable[tuple[str, str]], at: datetime) -> None:
        for item_id, owner_id in items:
            self.rows.setdefault((kind, item_id), (owner_id, at))

    async def forget(self, kind: str, item_ids: Iterable[str]) -> None:
        for item_id in item_ids:
            self.rows.pop((kind, item_id), None)
            self.claimed.pop((kind, item_id), None)


class PostgresActivityStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._owns_pool = False

    async def start(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=2)
            self._owns_pool = True
        try:
            migration = files("magic_video_editor.migrations").joinpath("020_item_activity.sql").read_text()
            async with self._pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", STUDIO_SCHEMA_ADVISORY_LOCK)
                await connection.execute(migration)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        if self._owns_pool:
            self._pool = None
            self._owns_pool = False

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("activity store pool is not started")
        return self._pool

    async def touch(self, kind: str, item_id: str, owner_id: str, at: datetime) -> bool:
        async with self._require_pool().acquire() as connection:
            touched = await connection.fetchval(
                """
                INSERT INTO studio_item_activity (kind, item_id, owner_id, last_active_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (kind, item_id) DO UPDATE
                SET owner_id = EXCLUDED.owner_id,
                    last_active_at = GREATEST(studio_item_activity.last_active_at, EXCLUDED.last_active_at)
                WHERE studio_item_activity.expiring_since IS NULL
                RETURNING true
                """,
                kind, item_id, owner_id, at,
            )
        return bool(touched)

    async def claim(self, kind: str, item_id: str, cutoff: datetime, at: datetime) -> bool:
        # A claim left by a sweep that died mid-delete is still due, so a later
        # sweep claims it again once the lease lapses and finishes.
        async with self._require_pool().acquire() as connection:
            claimed = await connection.fetchval(
                """
                UPDATE studio_item_activity SET expiring_since = $4
                WHERE kind = $1 AND item_id = $2 AND last_active_at < $3
                  AND (expiring_since IS NULL OR expiring_since < $5)
                RETURNING true
                """,
                kind, item_id, cutoff, at, at - CLAIM_LEASE,
            )
        return bool(claimed)

    async def release(self, kind: str, item_id: str, at: datetime) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                """
                UPDATE studio_item_activity
                SET expiring_since = NULL, last_active_at = GREATEST(last_active_at, $3)
                WHERE kind = $1 AND item_id = $2
                """,
                kind, item_id, at,
            )

    async def last_active(self, kind: str, item_ids: Iterable[str]) -> dict[str, datetime]:
        ids = list(item_ids)
        if not ids:
            return {}
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                "SELECT item_id, last_active_at FROM studio_item_activity WHERE kind = $1 AND item_id = ANY($2)",
                kind, ids,
            )
        return {row["item_id"]: row["last_active_at"] for row in rows}

    async def tracked(self, kind: str) -> dict[str, datetime]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                "SELECT item_id, last_active_at FROM studio_item_activity WHERE kind = $1", kind
            )
        return {row["item_id"]: row["last_active_at"] for row in rows}

    async def enrol(self, kind: str, items: Iterable[tuple[str, str]], at: datetime) -> None:
        rows = [(kind, item_id, owner_id, at) for item_id, owner_id in items]
        if not rows:
            return
        async with self._require_pool().acquire() as connection:
            await connection.executemany(
                """
                INSERT INTO studio_item_activity (kind, item_id, owner_id, last_active_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (kind, item_id) DO NOTHING
                """,
                rows,
            )

    async def forget(self, kind: str, item_ids: Iterable[str]) -> None:
        ids = list(item_ids)
        if not ids:
            return
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "DELETE FROM studio_item_activity WHERE kind = $1 AND item_id = ANY($2)", kind, ids
            )


class Retention:
    """Records activity and answers when an item expires."""

    def __init__(
        self,
        policy: RetentionPolicy,
        activity: ActivityStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy = policy
        self.activity = activity
        self.clock = clock
        self._recent: dict[tuple[str, str], datetime] = {}

    async def touch(self, kind: str, item_id: str, owner_id: str) -> None:
        # Recorded even while expiry is off, so turning it back on never judges
        # work by timestamps that stopped moving while it was off.
        now = self.clock()
        last = self._recent.get((kind, item_id))
        if last is not None and now - last < TOUCH_INTERVAL:
            return
        # A failure propagates and fails the owner's request: an open that was
        # not recorded could let the next sweep delete work that is in use.
        # A cached open needs no claim check: it was recorded within the hour,
        # so the item is not due.
        if not await self.activity.touch(kind, item_id, owner_id, now):
            raise ItemExpiring(self.policy.days)
        self._recent[(kind, item_id)] = now

    async def forget(self, kind: str, item_id: str) -> None:
        """Drop a deleted item's activity row (its owner and last-open time)."""
        self._recent.pop((kind, item_id), None)
        await self.activity.forget(kind, [item_id])

    async def expiring(self, kind: str, item_ids: Iterable[str]) -> dict[str, datetime]:
        """When each item inside its warning window is deleted unless opened;
        items further off are omitted. An untracked item has not been swept yet
        and has a full period ahead of it."""
        if not self.policy.enabled:
            return {}
        ids = list(item_ids)
        now = self.clock()
        try:
            last = await self.activity.last_active(kind, ids)
        except Exception:  # a warning chip is not worth failing the list
            logger.warning("retention.lookup_failed", exc_info=True)
            return {}
        period = timedelta(days=self.policy.days)
        warning = timedelta(days=self.policy.warning_days)
        expiries = {item_id: last.get(item_id, now) + period for item_id in ids}
        return {item_id: at for item_id, at in expiries.items() if at - now <= warning}


@dataclass(frozen=True)
class SweepResult:
    expired: dict[str, list[str]]
    kept: int
    failed: int
    refusals_purged: int
    # Rows the extensions' purges removed, by the name each one reports under.
    purged: dict[str, int] = field(default_factory=dict)


class RetentionSweeper:
    """Deletes expired items. `listers` name every existing item of a kind with
    its owner; `expirers` delete one item and report whether they did (False
    means it is exempt or busy and stays)."""

    def __init__(
        self,
        retention: Retention,
        *,
        listers: Mapping[str, Callable[[], Awaitable[Iterable[tuple[str, str]]]]],
        expirers: Mapping[str, Callable[[str], Awaitable[bool]]],
        purge_refusals: Callable[[], Awaitable[int]] | None = None,
        purges: Mapping[str, Callable[[], Awaitable[int]]] | None = None,
    ) -> None:
        self.retention = retention
        self.listers = listers
        self.expirers = expirers
        self.purge_refusals = purge_refusals
        self.purges = dict(purges or {})

    async def sweep(self, limit: int = SWEEP_LIMIT) -> SweepResult:
        expired: dict[str, list[str]] = {}
        kept = failed = 0
        policy = self.retention.policy
        activity = self.retention.activity
        now = self.retention.clock()
        if policy.enabled:
            cutoff = now - timedelta(days=policy.days)
            for kind, lister in self.listers.items():
                existing = dict(await lister())
                tracked = await activity.tracked(kind)
                await activity.forget(kind, [i for i in tracked if i not in existing])
                await activity.enrol(kind, [(i, o) for i, o in existing.items() if i not in tracked], now)
                due = sorted((at, i) for i, at in tracked.items() if i in existing and at < cutoff)
                expired[kind] = []
                for _, item_id in due[:limit]:
                    # Claimed only if still idle: opened since the snapshot above
                    # (perhaps while an earlier item was being deleted), it stays.
                    # Once claimed, the owner cannot open it until it is settled.
                    if not await activity.claim(kind, item_id, cutoff, now):
                        continue
                    try:
                        deleted = await self.expirers[kind](item_id)
                    except Exception:  # one failure must not stop the sweep
                        failed += 1
                        logger.exception("retention.cleanup_failed", extra={"kind": kind, "item_id": item_id})
                        # Due again in a day: failures neither crowd out the items
                        # behind them nor stretch one sweep past its deadline.
                        await activity.release(kind, item_id, cutoff + FAILED_RETRY)
                        continue
                    if deleted:
                        await activity.forget(kind, [item_id])
                        expired[kind].append(item_id)
                    else:
                        # Released: it stays, and is not looked at again for a
                        # full period, so kept items never crowd the sweep.
                        await activity.release(kind, item_id, now)
                        kept += 1
        purged = await self.purge_refusals() if self.purge_refusals is not None else 0
        extra_purged = {name: await purge() for name, purge in self.purges.items()}
        logger.info(
            "retention.swept",
            extra={"expired": {k: len(v) for k, v in expired.items()}, "kept": kept,
                   "failed": failed, "refusals_purged": purged, **extra_purged},
        )
        return SweepResult(expired, kept, failed, purged, extra_purged)
