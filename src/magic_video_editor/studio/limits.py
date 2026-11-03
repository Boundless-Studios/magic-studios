"""Per-owner storage and concurrency caps for internal multi-user use.

Both caps refuse to *start new work* and nothing else: approving, releasing,
exporting and downloading what already exists always go through, so a cap can
never strand an operator's finished project behind a quota.

Stored bytes are tracked incrementally in `studio_owner_usage` -- listing a
shared bucket per request would be both slow and a cross-tenant read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Protocol

import asyncpg

from magic_video_editor.studio.models import FrozenModel
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

OWNER_STORAGE_LIMIT_ENV = "MAGIC_VIDEO_EDITOR_OWNER_STORAGE_LIMIT_BYTES"
OWNER_CONCURRENT_JOB_LIMIT_ENV = "MAGIC_VIDEO_EDITOR_OWNER_CONCURRENT_JOB_LIMIT"
PUBLIC_STORAGE_LIMIT_ENV = "MAGIC_VIDEO_EDITOR_PUBLIC_STORAGE_LIMIT_BYTES"

# Generous for internal use: an operator has to be genuinely prolific to notice.
DEFAULT_OWNER_STORAGE_LIMIT_BYTES = 50 * 1024**3
# Self-registered (public) accounts get a smaller default ceiling.
DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES = 5 * 1024**3
DEFAULT_OWNER_CONCURRENT_JOB_LIMIT = 8

UNLIMITED = 0


class OwnerLimits(FrozenModel):
    """`0` means unlimited for any cap. `public_storage_limit_bytes` applies to
    public accounts instead of `storage_limit_bytes`."""

    storage_limit_bytes: int = DEFAULT_OWNER_STORAGE_LIMIT_BYTES
    concurrent_job_limit: int = DEFAULT_OWNER_CONCURRENT_JOB_LIMIT
    public_storage_limit_bytes: int = DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> OwnerLimits:
        return cls(
            storage_limit_bytes=_non_negative_int(
                values.get(OWNER_STORAGE_LIMIT_ENV),
                DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
            ),
            concurrent_job_limit=_non_negative_int(
                values.get(OWNER_CONCURRENT_JOB_LIMIT_ENV),
                DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
            ),
            public_storage_limit_bytes=_non_negative_int(
                values.get(PUBLIC_STORAGE_LIMIT_ENV),
                DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES,
            ),
        )


class OwnerUsage(FrozenModel):
    """What an owner is using right now, and against what ceiling."""

    owner_id: str
    stored_bytes: int
    storage_limit_bytes: int
    running_jobs: int
    concurrent_job_limit: int
    account_tier: str = "invited"


class OwnerLimitExceeded(Exception):
    """Base for a refusal to start new work. Carries plain-language copy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class OwnerStorageLimitExceeded(OwnerLimitExceeded):
    def __init__(self, stored_bytes: int, limit_bytes: int) -> None:
        self.stored_bytes = stored_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            "owner_storage_limit_reached",
            f"You are using {_gib(stored_bytes)} of your {_gib(limit_bytes)} of "
            "storage. Delete a project you no longer need before starting new work.",
        )


class OwnerConcurrentJobLimitExceeded(OwnerLimitExceeded):
    def __init__(self, running_jobs: int, limit: int) -> None:
        self.running_jobs = running_jobs
        self.limit = limit
        super().__init__(
            "owner_concurrent_job_limit_reached",
            f"You have {running_jobs} generations running. "
            "Wait for one to finish.",
        )


class StudioOwnerUsageStore(Protocol):
    async def stored_bytes(self, owner_id: str) -> int: ...

    async def forget_owner(self, owner_id: str) -> None:
        """Drop the owner's usage row (account deletion)."""
        ...

    async def add_bytes(self, owner_id: str, delta: int) -> int:
        """Apply a signed delta and return the owner's new total, floored at 0.
        A release (negative delta) never creates a row: one that lands after
        account deletion must not bring the deleted owner's ID back."""
        ...

    async def record_artifact(self, owner_id: str, artifact_key: str, size: int) -> bool:
        """Count one artifact's bytes once: True when this call added them, False
        when the key was already counted. Retries and races never double-count."""
        ...

    async def release_artifact(self, owner_id: str, artifact_key: str) -> int:
        """Give back exactly what `record_artifact` counted for the key (0 if it
        never was), and forget the key."""
        ...

    async def release_artifacts(self, owner_id: str, key_prefix: str) -> int:
        """`release_artifact` for every counted key under `key_prefix` (a deleted
        project's), whatever any manifest snapshot listed."""
        ...


class MemoryStudioOwnerUsageStore:
    def __init__(self) -> None:
        self._stored: dict[str, int] = {}
        self._artifacts: dict[tuple[str, str], int] = {}

    async def stored_bytes(self, owner_id: str) -> int:
        return self._stored.get(owner_id, 0)

    async def forget_owner(self, owner_id: str) -> None:
        self._stored.pop(owner_id, None)
        for owner, key in [k for k in self._artifacts if k[0] == owner_id]:
            del self._artifacts[(owner, key)]

    async def record_artifact(self, owner_id: str, artifact_key: str, size: int) -> bool:
        if (owner_id, artifact_key) in self._artifacts:
            return False
        self._artifacts[(owner_id, artifact_key)] = size
        await self.add_bytes(owner_id, size)
        return True

    async def release_artifact(self, owner_id: str, artifact_key: str) -> int:
        size = self._artifacts.pop((owner_id, artifact_key), 0)
        if size:
            await self.add_bytes(owner_id, -size)
        return size

    async def release_artifacts(self, owner_id: str, key_prefix: str) -> int:
        released = 0
        for owner, key in [k for k in self._artifacts if k[0] == owner_id and k[1].startswith(key_prefix)]:
            released += await self.release_artifact(owner, key)
        return released

    async def add_bytes(self, owner_id: str, delta: int) -> int:
        if delta < 0 and owner_id not in self._stored:
            return 0
        total = max(0, self._stored.get(owner_id, 0) + delta)
        self._stored[owner_id] = total
        return total


class PostgresStudioOwnerUsageStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._owns_pool = False

    async def start(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._database_url, min_size=1, max_size=2
            )
            self._owns_pool = True
        try:
            migrations = [
                files("magic_video_editor.migrations").joinpath(name).read_text()
                for name in ("014_studio_owner_usage.sql", "023_studio_owner_usage_artifacts.sql")
            ]
            async with (
                self._pool.acquire() as connection,
                connection.transaction(),
            ):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    STUDIO_SCHEMA_ADVISORY_LOCK,
                )
                for migration in migrations:
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
            raise RuntimeError("owner usage store pool is not started")
        return self._pool

    async def stored_bytes(self, owner_id: str) -> int:
        async with self._require_pool().acquire() as connection:
            total = await connection.fetchval(
                "SELECT stored_bytes FROM studio_owner_usage WHERE owner_id = $1",
                owner_id,
            )
        return int(total or 0)

    async def forget_owner(self, owner_id: str) -> None:
        async with self._require_pool().acquire() as connection, connection.transaction():
            await connection.execute("DELETE FROM studio_owner_usage_artifacts WHERE owner_id = $1", owner_id)
            await connection.execute("DELETE FROM studio_owner_usage WHERE owner_id = $1", owner_id)

    async def record_artifact(self, owner_id: str, artifact_key: str, size: int) -> bool:
        # One transaction: the key and the total move together or not at all, so
        # a lost response followed by a retry finds the key and adds nothing.
        async with self._require_pool().acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                """
                INSERT INTO studio_owner_usage_artifacts (owner_id, artifact_key, size_bytes)
                VALUES ($1, $2, $3)
                ON CONFLICT (owner_id, artifact_key) DO NOTHING
                RETURNING size_bytes
                """,
                owner_id, artifact_key, size,
            )
            if inserted is None:
                return False
            await connection.execute(
                """
                INSERT INTO studio_owner_usage (owner_id, stored_bytes, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (owner_id) DO UPDATE
                SET stored_bytes = studio_owner_usage.stored_bytes + $2, updated_at = $3
                """,
                owner_id, size, datetime.now(UTC),
            )
        return True

    async def release_artifact(self, owner_id: str, artifact_key: str) -> int:
        async with self._require_pool().acquire() as connection, connection.transaction():
            released = await connection.fetchval(
                "DELETE FROM studio_owner_usage_artifacts WHERE owner_id = $1 AND artifact_key = $2 "
                "RETURNING size_bytes",
                owner_id, artifact_key,
            )
            if released is None:
                return 0
            await connection.execute(
                """
                UPDATE studio_owner_usage
                SET stored_bytes = GREATEST(stored_bytes - $2, 0), updated_at = $3
                WHERE owner_id = $1
                """,
                owner_id, released, datetime.now(UTC),
            )
        return int(released)

    async def release_artifacts(self, owner_id: str, key_prefix: str) -> int:
        async with self._require_pool().acquire() as connection, connection.transaction():
            # left()/length() rather than LIKE, so `%` or `_` in a key is literal.
            released = await connection.fetchval(
                """
                WITH gone AS (
                    DELETE FROM studio_owner_usage_artifacts
                    WHERE owner_id = $1 AND left(artifact_key, length($2)) = $2
                    RETURNING size_bytes
                )
                SELECT coalesce(sum(size_bytes), 0) FROM gone
                """,
                owner_id, key_prefix,
            )
            if released:
                await connection.execute(
                    """
                    UPDATE studio_owner_usage
                    SET stored_bytes = GREATEST(stored_bytes - $2, 0), updated_at = $3
                    WHERE owner_id = $1
                    """,
                    owner_id, released, datetime.now(UTC),
                )
        return int(released)

    async def add_bytes(self, owner_id: str, delta: int) -> int:
        async with self._require_pool().acquire() as connection:
            if delta < 0:
                total = await connection.fetchval(
                    """
                    UPDATE studio_owner_usage
                    SET stored_bytes = GREATEST(stored_bytes + $2, 0), updated_at = $3
                    WHERE owner_id = $1
                    RETURNING stored_bytes
                    """,
                    owner_id,
                    delta,
                    datetime.now(UTC),
                )
                return int(total or 0)
            total = await connection.fetchval(
                """
                INSERT INTO studio_owner_usage (owner_id, stored_bytes, updated_at)
                VALUES ($1, GREATEST($2, 0), $3)
                ON CONFLICT (owner_id) DO UPDATE
                SET stored_bytes = GREATEST(
                        studio_owner_usage.stored_bytes + $2, 0
                    ),
                    updated_at = $3
                RETURNING stored_bytes
                """,
                owner_id,
                delta,
                datetime.now(UTC),
            )
        return int(total or 0)


def _non_negative_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"
