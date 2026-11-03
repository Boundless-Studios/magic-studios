"""Studio API keys: bearer credentials that let a non-browser client act as a studio user.

A key is minted by an already-authenticated (IAP) actor and carries that actor's
identity and nothing more -- every `/studio/*` route continues to authorize against
the resolved actor id, so a key can never reach a project its owner cannot.

Only the SHA-256 of a key is stored; the plaintext is returned exactly once at
creation and is unrecoverable afterwards.
"""

from __future__ import annotations

import hashlib
import secrets
import string
import threading
import time
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Protocol
from uuid import uuid4

import asyncpg
from starlette.applications import Starlette

from magic_video_editor.studio.models import FrozenModel
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

API_KEY_PREFIX = "mvs_"
API_KEY_RANDOM_LENGTH = 32
MAX_API_KEY_NAME_LENGTH = 200
MAX_API_KEYS_PER_ACTOR = 50
BEARER_SCHEME = "bearer"
# One `last_used_at` write per key per minute: the column is an activity hint, not
# an audit log, and every request would otherwise cost a write.
LAST_USED_THROTTLE_SECONDS = 60.0

_API_KEY_ALPHABET = string.ascii_letters + string.digits + "-_"
_last_used_writes: dict[str, float] = {}
_last_used_lock = threading.Lock()


class StudioApiKey(FrozenModel):
    """A minted key's metadata. Never carries the plaintext token or its hash."""

    key_id: str
    actor_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class ApiKeyLimitReached(Exception):
    """The actor already holds the maximum number of live keys."""

    def __init__(self, actor_id: str, limit: int) -> None:
        self.actor_id = actor_id
        self.limit = limit
        super().__init__(f"actor {actor_id!r} already holds {limit} live API keys")


def generate_api_key() -> str:
    """A fresh plaintext key: `mvs_` plus 32 url-safe random characters."""
    random_part = "".join(
        secrets.choice(_API_KEY_ALPHABET) for _ in range(API_KEY_RANDOM_LENGTH)
    )
    return f"{API_KEY_PREFIX}{random_part}"


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def looks_like_api_key(candidate: str) -> bool:
    """Shape-only check, so a malformed token is rejected without a database read."""
    if not candidate.startswith(API_KEY_PREFIX):
        return False
    random_part = candidate[len(API_KEY_PREFIX) :]
    return len(random_part) == API_KEY_RANDOM_LENGTH and all(
        character in _API_KEY_ALPHABET for character in random_part
    )


def parse_bearer_token(authorization: str | None) -> str | None:
    """The bearer token from an `Authorization` header, or `None` when the header
    is absent or is not a bearer credential (a non-bearer scheme is left for the
    IAP path to answer)."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != BEARER_SCHEME:
        return None
    return token.strip() or None


def api_key_store_from_app(app: Starlette) -> StudioApiKeyStore | None:
    """`app.state` is Starlette's untyped namespace, so the miss has to be tolerated:
    a router mounted without a key store simply has no bearer authentication."""
    return getattr(app.state, "studio_api_key_store", None)


def should_write_last_used(key_id: str, *, now: float | None = None) -> bool:
    """True at most once per `LAST_USED_THROTTLE_SECONDS` per key, per process."""
    moment = time.monotonic() if now is None else now
    with _last_used_lock:
        previous = _last_used_writes.get(key_id)
        if previous is not None and moment - previous < LAST_USED_THROTTLE_SECONDS:
            return False
        _last_used_writes[key_id] = moment
        return True


def reset_last_used_throttle() -> None:
    """Test hook: forget every throttled write so a case can assert on the next one."""
    with _last_used_lock:
        _last_used_writes.clear()


class StudioApiKeyStore(Protocol):
    async def create(
        self, *, actor_id: str, name: str, key_hash: str
    ) -> StudioApiKey: ...

    async def get_by_hash(self, key_hash: str) -> StudioApiKey | None: ...

    async def list_for_actor(self, actor_id: str) -> tuple[StudioApiKey, ...]:
        """Every key the actor has minted, newest first, revoked ones included."""
        ...

    async def forget_owner(self, actor_id: str) -> None:
        """Delete every key the actor ever made (account deletion)."""
        ...

    async def revoke(self, *, actor_id: str, key_id: str) -> StudioApiKey | None:
        """Revoke the actor's key, or return `None` when it is not theirs or absent
        -- a stranger's key id must not be distinguishable from a missing one."""
        ...

    async def touch(self, key_id: str, used_at: datetime) -> None: ...


class MemoryStudioApiKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, StudioApiKey] = {}
        self._by_hash: dict[str, str] = {}

    async def create(self, *, actor_id: str, name: str, key_hash: str) -> StudioApiKey:
        live = [
            key
            for key in self._keys.values()
            if key.actor_id == actor_id and key.revoked_at is None
        ]
        if len(live) >= MAX_API_KEYS_PER_ACTOR:
            raise ApiKeyLimitReached(actor_id, MAX_API_KEYS_PER_ACTOR)
        record = StudioApiKey(
            key_id=str(uuid4()),
            actor_id=actor_id,
            name=name,
            created_at=datetime.now(UTC),
        )
        self._keys[record.key_id] = record
        self._by_hash[key_hash] = record.key_id
        return record

    async def get_by_hash(self, key_hash: str) -> StudioApiKey | None:
        key_id = self._by_hash.get(key_hash)
        return None if key_id is None else self._keys.get(key_id)

    async def list_for_actor(self, actor_id: str) -> tuple[StudioApiKey, ...]:
        owned = [key for key in self._keys.values() if key.actor_id == actor_id]
        owned.sort(key=lambda key: key.created_at, reverse=True)
        return tuple(owned)

    async def forget_owner(self, actor_id: str) -> None:
        for key_id in [key_id for key_id, key in self._keys.items() if key.actor_id == actor_id]:
            del self._keys[key_id]
        for key_hash in [h for h, key_id in self._by_hash.items() if key_id not in self._keys]:
            del self._by_hash[key_hash]

    async def revoke(self, *, actor_id: str, key_id: str) -> StudioApiKey | None:
        record = self._keys.get(key_id)
        if record is None or record.actor_id != actor_id:
            return None
        if record.revoked_at is not None:
            return record
        revoked = record.model_copy(update={"revoked_at": datetime.now(UTC)})
        self._keys[key_id] = revoked
        return revoked

    async def touch(self, key_id: str, used_at: datetime) -> None:
        record = self._keys.get(key_id)
        if record is not None:
            self._keys[key_id] = record.model_copy(update={"last_used_at": used_at})


class PostgresStudioApiKeyStore:
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
            migration = (
                files("magic_video_editor.migrations")
                .joinpath("012_studio_api_keys.sql")
                .read_text()
            )
            async with (
                self._pool.acquire() as connection,
                connection.transaction(),
            ):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    STUDIO_SCHEMA_ADVISORY_LOCK,
                )
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
            raise RuntimeError("api key store pool is not started")
        return self._pool

    async def create(self, *, actor_id: str, name: str, key_hash: str) -> StudioApiKey:
        async with self._require_pool().acquire() as connection:
            live = await connection.fetchval(
                "SELECT count(*) FROM studio_api_keys "
                "WHERE actor_id = $1 AND revoked_at IS NULL",
                actor_id,
            )
            if int(live) >= MAX_API_KEYS_PER_ACTOR:
                raise ApiKeyLimitReached(actor_id, MAX_API_KEYS_PER_ACTOR)
            row = await connection.fetchrow(
                """
                INSERT INTO studio_api_keys (key_id, actor_id, name, key_hash)
                VALUES ($1, $2, $3, $4)
                RETURNING key_id, actor_id, name, created_at, last_used_at, revoked_at
                """,
                uuid4(),
                actor_id,
                name,
                key_hash,
            )
        return _row_to_key(row)

    async def get_by_hash(self, key_hash: str) -> StudioApiKey | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT key_id, actor_id, name, created_at, last_used_at, revoked_at
                FROM studio_api_keys WHERE key_hash = $1
                """,
                key_hash,
            )
        return None if row is None else _row_to_key(row)

    async def list_for_actor(self, actor_id: str) -> tuple[StudioApiKey, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT key_id, actor_id, name, created_at, last_used_at, revoked_at
                FROM studio_api_keys WHERE actor_id = $1 ORDER BY created_at DESC
                """,
                actor_id,
            )
        return tuple(_row_to_key(row) for row in rows)

    async def forget_owner(self, actor_id: str) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute("DELETE FROM studio_api_keys WHERE actor_id = $1", actor_id)

    async def revoke(self, *, actor_id: str, key_id: str) -> StudioApiKey | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE studio_api_keys
                SET revoked_at = coalesce(revoked_at, now())
                WHERE key_id = $1::uuid AND actor_id = $2
                RETURNING key_id, actor_id, name, created_at, last_used_at, revoked_at
                """,
                key_id,
                actor_id,
            )
        return None if row is None else _row_to_key(row)

    async def touch(self, key_id: str, used_at: datetime) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE studio_api_keys SET last_used_at = $2 WHERE key_id = $1::uuid",
                key_id,
                used_at,
            )


def _row_to_key(row: Any) -> StudioApiKey:
    return StudioApiKey(
        key_id=str(row["key_id"]),
        actor_id=row["actor_id"],
        name=row["name"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


class ApiKeyRejected(Exception):
    """The presented bearer token is not a usable studio API key."""


async def resolve_api_key_actor(store: StudioApiKeyStore, token: str) -> str:
    """The actor id a presented bearer token acts as, or `ApiKeyRejected`."""
    if not looks_like_api_key(token):
        raise ApiKeyRejected("malformed API key")
    record = await store.get_by_hash(hash_api_key(token))
    if record is None:
        raise ApiKeyRejected("unknown API key")
    if record.revoked_at is not None:
        raise ApiKeyRejected("revoked API key")
    if should_write_last_used(record.key_id):
        await store.touch(record.key_id, datetime.now(UTC))
    return record.actor_id
