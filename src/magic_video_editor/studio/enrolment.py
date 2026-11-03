"""Machine enrolment: how a person attaches their own GPU box to the studio.

Two credentials, deliberately different in kind.

A **pairing token** is a claim ticket. It proves "the person holding this asked
the studio, while signed in, to enrol a machine". It is single-use and expires in
an hour, because it travels through a human -- copied into a terminal, maybe
pasted into a chat window on the way. It authorizes exactly one thing: exchanging
itself for a machine credential, plus downloading the worker before that exchange.

A **machine credential** is durable and belongs to the machine, not the person.
It authenticates the agent to the pool on every request and carries the owner, so
the pool hands that machine only its owner's work.

Only sha256 digests are stored. Both plaintexts are returned exactly once.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, Protocol
from uuid import uuid4

import asyncpg

from magic_video_editor.studio.models import FrozenModel
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

PAIRING_TOKEN_PREFIX = "mvsp_"
WORKER_CREDENTIAL_PREFIX = "mvsw_"
TOKEN_RANDOM_LENGTH = 32
# An hour is long enough to walk to the other machine and paste it, short enough
# that a token left in a scrollback is dead by the time anyone finds it.
PAIRING_TOKEN_TTL = timedelta(hours=1)
MAX_WORKER_LABEL_LENGTH = 200
MAX_WORKERS_PER_OWNER = 20
# A machine credential is short-lived and the agent replaces it on its own, so a
# leaked one stops working on its own too. Thirty days is long enough that a
# machine off for a holiday still comes back, short enough to matter.
WORKER_CREDENTIAL_TTL = timedelta(days=30)
# Rotate well before expiry: a machine that is off, asleep, or without a network
# for days must still wake up inside its own window.
ROTATE_WHEN_REMAINING = timedelta(days=7)
# The old credential keeps working briefly after a rotation, so a reply lost in
# flight or a restart mid-rotation cannot lock a machine out of its own account.
PREVIOUS_CREDENTIAL_GRACE = timedelta(hours=24)

_ALPHABET = string.ascii_letters + string.digits + "-_"
# Every machine query returns the same shape, and a column added to the table
# has to reach `_row_to_worker`; naming the list once means it cannot be updated
# in three places and forgotten in the fourth.
_WORKER_COLUMNS = (
    "worker_id, owner_id, label, created_at, last_seen_at, revoked_at, "
    "credential_expires_at, rotated_at"
)


class PairingTokenState(FrozenModel):
    """A minted token's metadata. Never carries the plaintext or its hash."""

    token_id: str
    owner_id: str
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None = None
    redeemed_worker_id: str | None = None

    def usable_at(self, moment: datetime) -> bool:
        return self.redeemed_at is None and moment < self.expires_at


class EnrolledWorker(FrozenModel):
    """One machine someone has enrolled."""

    worker_id: str
    owner_id: str
    label: str
    created_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    credential_expires_at: datetime | None = None
    rotated_at: datetime | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class CredentialRotationRefused(Exception):
    """The credential offered for rotation is not one this machine may rotate.

    Raised for an unknown or revoked credential, and for the *previous*
    credential after a rotation has already happened: that second case is the
    interesting one, because it is what a stolen copy looks like when the real
    machine has already moved on.
    """


class PairingTokenUnusable(Exception):
    """The token is unknown, already redeemed, or past its hour.

    One exception for all three on purpose: the agent redeeming a token must not
    learn which, or a wrong token becomes an oracle for a right one.
    """


class WorkerLimitReached(Exception):
    def __init__(self, owner_id: str, limit: int) -> None:
        self.owner_id = owner_id
        self.limit = limit
        super().__init__(f"owner {owner_id!r} already has {limit} enrolled machines")


def _random_token(prefix: str) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_RANDOM_LENGTH))
    return f"{prefix}{body}"


def generate_pairing_token() -> str:
    return _random_token(PAIRING_TOKEN_PREFIX)


def generate_worker_credential() -> str:
    return _random_token(WORKER_CREDENTIAL_PREFIX)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def looks_like_pairing_token(candidate: str) -> bool:
    """Shape-only, so a malformed token is refused without a database read."""
    return _looks_like(candidate, PAIRING_TOKEN_PREFIX)


def looks_like_worker_credential(candidate: str) -> bool:
    return _looks_like(candidate, WORKER_CREDENTIAL_PREFIX)


def _looks_like(candidate: str, prefix: str) -> bool:
    if not candidate.startswith(prefix):
        return False
    body = candidate[len(prefix) :]
    return len(body) == TOKEN_RANDOM_LENGTH and all(
        character in _ALPHABET for character in body
    )


def normalize_label(label: str | None) -> str:
    """A machine's display name. Blank is allowed and becomes a plain default,
    so enrolment never fails on a cosmetic field."""
    cleaned = (label or "").strip()
    if not cleaned:
        return "Unnamed machine"
    return cleaned[:MAX_WORKER_LABEL_LENGTH]


class StudioEnrolmentStore(Protocol):
    async def usable_pairing_token(
        self, *, token_hash: str, now: datetime
    ) -> bool:
        """Authorize a package download without spending the pairing code."""
        ...

    async def mint_pairing_token(
        self, *, owner_id: str, token_hash: str, expires_at: datetime
    ) -> PairingTokenState: ...

    async def redeem_pairing_token(
        self,
        *,
        token_hash: str,
        worker_id: str,
        label: str,
        credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        """Exchange a token for a machine, atomically.

        Raises `PairingTokenUnusable` when the token is unknown, spent or expired,
        and `WorkerLimitReached` when the owner is at their machine limit.
        """
        ...

    async def worker_by_credential(self, credential_hash: str) -> EnrolledWorker | None:
        ...

    async def rotate_credential(
        self,
        *,
        credential_hash: str,
        new_credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        """Replace a machine's credential with a fresh one.

        Only the *current* credential may rotate. The previous one still
        authenticates during its grace window, but presenting it here is refused:
        the machine that owns the account has already moved on, so this is either
        a stale copy or a stolen one, and either way it must not take the account.
        """
        ...

    async def worker_by_id(self, worker_id: str) -> EnrolledWorker | None: ...

    async def list_workers(self, owner_id: str) -> tuple[EnrolledWorker, ...]:
        """Every machine this owner enrolled, newest first, revoked included."""
        ...

    async def rename_worker(
        self, *, owner_id: str, worker_id: str, label: str
    ) -> EnrolledWorker | None: ...

    async def forget_owner(self, owner_id: str) -> None:
        """Delete the owner's machines and pairing codes (account deletion). A
        deleted machine's credential no longer authenticates."""
        ...

    async def revoke_worker(
        self, *, owner_id: str, worker_id: str
    ) -> EnrolledWorker | None:
        """Revoke the owner's machine, or `None` when it is not theirs or absent --
        a stranger's machine id must not be distinguishable from a missing one."""
        ...

    async def touch_worker(self, worker_id: str, seen_at: datetime) -> None: ...


class MemoryStudioEnrolmentStore:
    def __init__(self) -> None:
        self._tokens: dict[str, PairingTokenState] = {}
        self._token_hashes: dict[str, str] = {}
        self._workers: dict[str, EnrolledWorker] = {}
        self._credentials: dict[str, str] = {}
        # hash -> (worker_id, valid_until): the superseded credential still
        # authenticates until its grace window closes.
        self._previous: dict[str, tuple[str, datetime]] = {}

    async def usable_pairing_token(self, *, token_hash: str, now: datetime) -> bool:
        token_id = self._token_hashes.get(token_hash)
        token = self._tokens.get(token_id) if token_id is not None else None
        return token is not None and token.usable_at(now)

    async def mint_pairing_token(
        self, *, owner_id: str, token_hash: str, expires_at: datetime
    ) -> PairingTokenState:
        state = PairingTokenState(
            token_id=str(uuid4()),
            owner_id=owner_id,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self._tokens[state.token_id] = state
        self._token_hashes[token_hash] = state.token_id
        return state

    async def redeem_pairing_token(
        self,
        *,
        token_hash: str,
        worker_id: str,
        label: str,
        credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        token_id = self._token_hashes.get(token_hash)
        state = None if token_id is None else self._tokens.get(token_id)
        if state is None or not state.usable_at(now):
            raise PairingTokenUnusable
        live = [
            worker
            for worker in self._workers.values()
            if worker.owner_id == state.owner_id and worker.revoked_at is None
        ]
        if len(live) >= MAX_WORKERS_PER_OWNER:
            raise WorkerLimitReached(state.owner_id, MAX_WORKERS_PER_OWNER)
        worker = EnrolledWorker(
            worker_id=worker_id,
            owner_id=state.owner_id,
            label=label,
            created_at=now,
            credential_expires_at=now + WORKER_CREDENTIAL_TTL,
        )
        self._workers[worker_id] = worker
        self._credentials[credential_hash] = worker_id
        self._tokens[state.token_id] = state.model_copy(
            update={"redeemed_at": now, "redeemed_worker_id": worker_id}
        )
        return worker

    async def worker_by_credential(self, credential_hash: str) -> EnrolledWorker | None:
        worker_id = self._credentials.get(credential_hash)
        if worker_id is None:
            previous = self._previous.get(credential_hash)
            if previous is None or datetime.now(UTC) >= previous[1]:
                return None
            worker_id = previous[0]
        worker = self._workers.get(worker_id)
        if worker is None:
            return None
        expiry = worker.credential_expires_at
        # A credential minted before rotation existed has no expiry and stays
        # valid; it gains one the first time it rotates.
        if expiry is not None and datetime.now(UTC) >= expiry:
            return None
        return worker

    async def rotate_credential(
        self,
        *,
        credential_hash: str,
        new_credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        worker_id = self._credentials.get(credential_hash)
        if worker_id is None:
            raise CredentialRotationRefused
        worker = self._workers.get(worker_id)
        if worker is None or worker.revoked:
            raise CredentialRotationRefused
        del self._credentials[credential_hash]
        self._credentials[new_credential_hash] = worker_id
        self._previous[credential_hash] = (
            worker_id,
            now + PREVIOUS_CREDENTIAL_GRACE,
        )
        rotated = worker.model_copy(
            update={
                "credential_expires_at": now + WORKER_CREDENTIAL_TTL,
                "rotated_at": now,
            }
        )
        self._workers[worker_id] = rotated
        return rotated

    async def worker_by_id(self, worker_id: str) -> EnrolledWorker | None:
        return self._workers.get(worker_id)

    async def list_workers(self, owner_id: str) -> tuple[EnrolledWorker, ...]:
        owned = [
            worker for worker in self._workers.values() if worker.owner_id == owner_id
        ]
        owned.sort(key=lambda worker: worker.created_at, reverse=True)
        return tuple(owned)

    async def rename_worker(
        self, *, owner_id: str, worker_id: str, label: str
    ) -> EnrolledWorker | None:
        worker = self._workers.get(worker_id)
        if worker is None or worker.owner_id != owner_id:
            return None
        renamed = worker.model_copy(update={"label": label})
        self._workers[worker_id] = renamed
        return renamed

    async def forget_owner(self, owner_id: str) -> None:
        workers = {wid for wid, worker in self._workers.items() if worker.owner_id == owner_id}
        for worker_id in workers:
            del self._workers[worker_id]
        for credential in [c for c, wid in self._credentials.items() if wid in workers]:
            del self._credentials[credential]
        for credential in [c for c, (wid, _) in self._previous.items() if wid in workers]:
            del self._previous[credential]
        tokens = {tid for tid, token in self._tokens.items() if token.owner_id == owner_id}
        for token_id in tokens:
            del self._tokens[token_id]
        for token_hash in [h for h, tid in self._token_hashes.items() if tid in tokens]:
            del self._token_hashes[token_hash]

    async def revoke_worker(
        self, *, owner_id: str, worker_id: str
    ) -> EnrolledWorker | None:
        worker = self._workers.get(worker_id)
        if worker is None or worker.owner_id != owner_id:
            return None
        if worker.revoked_at is not None:
            return worker
        revoked = worker.model_copy(update={"revoked_at": datetime.now(UTC)})
        self._workers[worker_id] = revoked
        return revoked

    async def touch_worker(self, worker_id: str, seen_at: datetime) -> None:
        worker = self._workers.get(worker_id)
        if worker is not None:
            self._workers[worker_id] = worker.model_copy(
                update={"last_seen_at": seen_at}
            )


class PostgresStudioEnrolmentStore:
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
            migration = "\n".join(
                files("magic_video_editor.migrations").joinpath(name).read_text()
                for name in (
                    "015_worker_enrolment.sql",
                    "016_worker_credential_rotation.sql",
                )
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
            raise RuntimeError("enrolment store pool is not started")
        return self._pool

    async def usable_pairing_token(self, *, token_hash: str, now: datetime) -> bool:
        async with self._require_pool().acquire() as connection:
            return bool(await connection.fetchval(
                """SELECT EXISTS (
                    SELECT 1 FROM studio_pairing_tokens
                    WHERE token_hash = $1 AND redeemed_at IS NULL AND expires_at > $2
                )""",
                token_hash,
                now,
            ))

    async def mint_pairing_token(
        self, *, owner_id: str, token_hash: str, expires_at: datetime
    ) -> PairingTokenState:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO studio_pairing_tokens
                    (token_id, token_hash, owner_id, expires_at)
                VALUES ($1, $2, $3, $4)
                RETURNING token_id, owner_id, created_at, expires_at,
                          redeemed_at, redeemed_worker_id
                """,
                uuid4(),
                token_hash,
                owner_id,
                expires_at,
            )
        return _row_to_token(row)

    async def redeem_pairing_token(
        self,
        *,
        token_hash: str,
        worker_id: str,
        label: str,
        credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            # Spend the token and read its owner in one statement: two agents
            # racing the same token must not both get a machine, and the loser
            # must be indistinguishable from someone presenting a bad token.
            token = await connection.fetchrow(
                """
                UPDATE studio_pairing_tokens
                SET redeemed_at = $2, redeemed_worker_id = $3
                WHERE token_hash = $1
                  AND redeemed_at IS NULL
                  AND expires_at > $2
                RETURNING owner_id
                """,
                token_hash,
                now,
                worker_id,
            )
            if token is None:
                raise PairingTokenUnusable
            owner_id = str(token["owner_id"])
            live = await connection.fetchval(
                "SELECT count(*) FROM studio_workers "
                "WHERE owner_id = $1 AND revoked_at IS NULL",
                owner_id,
            )
            if int(live) >= MAX_WORKERS_PER_OWNER:
                raise WorkerLimitReached(owner_id, MAX_WORKERS_PER_OWNER)
            row = await connection.fetchrow(
                f"""
                INSERT INTO studio_workers
                    (worker_id, owner_id, label, credential_hash, created_at,
                     credential_expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING {_WORKER_COLUMNS}
                """,
                worker_id,
                owner_id,
                label,
                credential_hash,
                now,
                now + WORKER_CREDENTIAL_TTL,
            )
        return _row_to_worker(row)

    async def worker_by_credential(self, credential_hash: str) -> EnrolledWorker | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_WORKER_COLUMNS}
                FROM studio_workers
                WHERE (
                        credential_hash = $1
                        -- A credential with no expiry predates rotation and stays
                        -- valid until it rotates for the first time.
                        AND (credential_expires_at IS NULL
                             OR credential_expires_at > now())
                      )
                   OR (
                        previous_credential_hash = $1
                        AND previous_credential_expires_at > now()
                      )
                """,
                credential_hash,
            )
        return None if row is None else _row_to_worker(row)

    async def rotate_credential(
        self,
        *,
        credential_hash: str,
        new_credential_hash: str,
        now: datetime,
    ) -> EnrolledWorker:
        async with self._require_pool().acquire() as connection:
            # Matching on credential_hash alone is what refuses the previous
            # credential: once a rotation has happened the old hash has moved to
            # previous_credential_hash and can no longer rotate anything.
            row = await connection.fetchrow(
                f"""
                UPDATE studio_workers
                SET previous_credential_hash = credential_hash,
                    previous_credential_expires_at = $3,
                    credential_hash = $2,
                    credential_expires_at = $4,
                    rotated_at = $5
                WHERE credential_hash = $1 AND revoked_at IS NULL
                RETURNING {_WORKER_COLUMNS}
                """,
                credential_hash,
                new_credential_hash,
                now + PREVIOUS_CREDENTIAL_GRACE,
                now + WORKER_CREDENTIAL_TTL,
                now,
            )
        if row is None:
            raise CredentialRotationRefused
        return _row_to_worker(row)

    async def worker_by_id(self, worker_id: str) -> EnrolledWorker | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(f"SELECT {_WORKER_COLUMNS} FROM studio_workers WHERE worker_id = $1", worker_id)
        return _row_to_worker(row) if row is not None else None

    async def list_workers(self, owner_id: str) -> tuple[EnrolledWorker, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_WORKER_COLUMNS}
                FROM studio_workers WHERE owner_id = $1 ORDER BY created_at DESC
                """,
                owner_id,
            )
        return tuple(_row_to_worker(row) for row in rows)

    async def rename_worker(
        self, *, owner_id: str, worker_id: str, label: str
    ) -> EnrolledWorker | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE studio_workers SET label = $3
                WHERE worker_id = $1 AND owner_id = $2
                RETURNING {_WORKER_COLUMNS}
                """,
                worker_id,
                owner_id,
                label,
            )
        return None if row is None else _row_to_worker(row)

    async def forget_owner(self, owner_id: str) -> None:
        async with self._require_pool().acquire() as connection, connection.transaction():
            await connection.execute("DELETE FROM studio_pairing_tokens WHERE owner_id = $1", owner_id)
            await connection.execute("DELETE FROM studio_workers WHERE owner_id = $1", owner_id)

    async def revoke_worker(
        self, *, owner_id: str, worker_id: str
    ) -> EnrolledWorker | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE studio_workers
                SET revoked_at = coalesce(revoked_at, now())
                WHERE worker_id = $1 AND owner_id = $2
                RETURNING {_WORKER_COLUMNS}
                """,
                worker_id,
                owner_id,
            )
        return None if row is None else _row_to_worker(row)

    async def touch_worker(self, worker_id: str, seen_at: datetime) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE studio_workers SET last_seen_at = $2 WHERE worker_id = $1",
                worker_id,
                seen_at,
            )


def _row_to_token(row: Any) -> PairingTokenState:
    return PairingTokenState(
        token_id=str(row["token_id"]),
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        redeemed_at=row["redeemed_at"],
        redeemed_worker_id=row["redeemed_worker_id"],
    )


def _row_to_worker(row: Any) -> EnrolledWorker:
    return EnrolledWorker(
        worker_id=row["worker_id"],
        owner_id=row["owner_id"],
        label=row["label"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        revoked_at=row["revoked_at"],
        credential_expires_at=row["credential_expires_at"],
        rotated_at=row["rotated_at"],
    )


ENROLLED_SUBJECT_PREFIX = "studio-machine:"


def enrolled_worker_subject(worker_id: str) -> str:
    """The pool identity of one enrolled machine.

    The pool keeps one worker per identity subject, so the subject names the
    machine, not its owner: an owner with two machines has two identities. The
    owner is carried as the worker's tenant instead, which is what binds the
    machine's leases to that person's jobs.
    """
    return f"{ENROLLED_SUBJECT_PREFIX}{worker_id}"


class PoolEnroller(Protocol):
    async def enroll_worker(self, worker_id: str, enrollment: Any) -> Any: ...


async def enroll_in_pool(pool: PoolEnroller, worker: EnrolledWorker) -> None:
    """Approve a newly redeemed machine in the pool, server-side.

    The pool admits a worker only with an existing registry binding or an
    explicit trusted enrollment; nothing the machine sends can choose its
    identity or tenant. Both come from the studio's own record here: the
    machine's own subject, and the owner who minted the pairing code.
    """
    from outbound_gpu_worker_pool import WorkerEnrollment

    await pool.enroll_worker(
        worker.worker_id,
        WorkerEnrollment(
            identity_subject=enrolled_worker_subject(worker.worker_id),
            tenant_id=worker.owner_id,
        ),
    )


class EnrolledWorkerAuthenticator:
    """Resolves a machine credential to a pool worker identity.

    Satisfies the pool's `WorkerAuthenticator` protocol, so an enrolled machine
    authenticates the same way a statically-configured one does. The identity's
    subject is the machine's own (`enrolled_worker_subject`); the owner is the
    tenant the studio enrolled it under.

    Machines that first heartbeated before server-approved enrollment wrote a
    registry row under their owner as the subject. That row is server state and
    stays authoritative, so given the `registry` such a machine keeps
    authenticating under the subject it was bound to.
    """

    def __init__(
        self,
        store: StudioEnrolmentStore,
        *,
        fallback: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        self._store = store
        self._fallback = fallback
        self._registry = registry

    async def authenticate(self, authorization: str | None) -> Any:
        from outbound_gpu_worker_pool.contracts import (
            WorkerAuthError,
            WorkerIdentity,
        )

        credential = _bearer(authorization)
        # Shape decides which authenticator owns the credential, so an enrolled
        # machine and a statically-configured one can coexist: anything not
        # shaped like a machine credential belongs to the configured method.
        if credential is None or not looks_like_worker_credential(credential):
            if self._fallback is not None:
                return await self._fallback.authenticate(authorization)
            raise WorkerAuthError("unknown worker credential")
        worker = await self._store.worker_by_credential(hash_token(credential))
        if worker is None or worker.revoked:
            raise WorkerAuthError("unknown worker credential")
        subject = enrolled_worker_subject(worker.worker_id)
        if self._registry is not None:
            bound = await self._registry.get(worker.worker_id)
            if bound is not None and bound.identity_subject == worker.owner_id:
                subject = worker.owner_id
        return WorkerIdentity(
            worker_id=worker.worker_id,
            subject=subject,
            method="enrolment",
        )


def _bearer(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def pairing_token_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + PAIRING_TOKEN_TTL


def enrolment_store_from_app(app: Any) -> StudioEnrolmentStore | None:
    """`app.state` is Starlette's untyped namespace, so a miss is tolerated: a
    router mounted without an enrolment store simply cannot enrol machines."""
    return getattr(app.state, "studio_enrolment_store", None)
