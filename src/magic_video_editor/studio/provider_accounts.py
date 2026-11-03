"""Bring-your-own-key generation providers.

An owner may save a hosted provider's API key (today: Higgsfield) so their own
provider account runs and pays for generation. The key is encrypted with the
service's provider-credential secret before it is stored, is never returned to
any caller, and never enters a job payload, provenance record or log line.

The request ledger maps a Studio job to the provider's request ID. A row is
reserved before the provider is called and its request ID recorded after the
provider answers, so a submission whose outcome is unknown stays visible and is
failed instead of being silently resubmitted (which could charge the owner twice).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from typing import Any, Protocol

import asyncpg
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from magic_video_editor.studio.models import FrozenModel
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

HIGGSFIELD = "higgsfield"
PROVIDERS = frozenset({HIGGSFIELD})
CREDENTIAL_SECRET_VARIABLE = "MAGIC_VIDEO_EDITOR_PROVIDER_CREDENTIAL_SECRET"
MAX_PROVIDER_KEY_LENGTH = 512


class ProviderCredentialRejected(ValueError):
    pass


class ProviderCredential(FrozenModel):
    owner_id: str
    provider: str
    key_hint: str
    created_at: datetime
    updated_at: datetime


class ProviderRequest(FrozenModel):
    job_id: str
    owner_id: str
    provider: str
    model: str
    request_id: str | None = None
    reserved_at: datetime
    submitted_at: datetime | None = None


class CredentialCipher:
    """Fernet encryption keyed from one or more operator secrets.

    The first secret encrypts; every secret decrypts, so a secret can be
    rotated by prepending a new one and re-saving keys before removing the old.
    """

    def __init__(self, secrets: tuple[str, ...]) -> None:
        if not secrets or any(len(secret) < 32 for secret in secrets):
            raise ValueError("provider credential secrets must contain at least 32 characters")
        self._fernet = MultiFernet(
            [Fernet(base64.urlsafe_b64encode(sha256(secret.encode()).digest())) for secret in secrets]
        )

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> CredentialCipher | None:
        raw = values.get(CREDENTIAL_SECRET_VARIABLE, "")
        secrets = tuple(part.strip() for part in raw.split(",") if part.strip())
        return cls(secrets) if secrets else None

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise ProviderCredentialRejected("stored provider key cannot be decrypted") from exc


def normalize_provider_key(provider: str, api_key: str) -> str:
    """Validate the documented credential shape without contacting the provider."""
    if provider not in PROVIDERS:
        raise ProviderCredentialRejected("unknown provider")
    # Opaque: Higgsfield issues both KEY_ID:KEY_SECRET pairs and single keys,
    # and the Authorization header carries whichever was copied.
    key = api_key.strip()
    if not key or len(key) > MAX_PROVIDER_KEY_LENGTH or any(c.isspace() for c in key):
        raise ProviderCredentialRejected("enter the key exactly as Higgsfield shows it")
    return key


def key_hint(api_key: str) -> str:
    return "…" + api_key[-4:]


class ProviderAccountStore(Protocol):
    async def put_credential(
        self, owner_id: str, provider: str, ciphertext: bytes, hint: str
    ) -> ProviderCredential: ...

    async def get_credential(
        self, owner_id: str, provider: str
    ) -> tuple[ProviderCredential, bytes] | None: ...

    async def list_credentials(self, owner_id: str) -> tuple[ProviderCredential, ...]: ...

    async def forget_owner(self, owner_id: str) -> None:
        """Delete every saved key and request record of an owner (account deletion)."""
        ...

    async def delete_credential(self, owner_id: str, provider: str) -> bool: ...

    async def reserve_request(
        self, job_id: str, owner_id: str, provider: str, model: str
    ) -> ProviderRequest | None:
        """Insert the job's row; None when it already exists, so only one
        caller ever submits a job to the provider."""
        ...

    async def record_request_id(self, job_id: str, request_id: str) -> ProviderRequest | None:
        """Set the provider ID once; a second, different ID is never written."""
        ...

    async def get_request(self, job_id: str) -> ProviderRequest | None: ...


class ProviderAccounts:
    """The owner-facing surface: save, describe, read and forget provider keys."""

    def __init__(self, store: ProviderAccountStore, cipher: CredentialCipher | None) -> None:
        self.store = store
        self._cipher = cipher

    @property
    def available(self) -> bool:
        return self._cipher is not None

    async def save_key(self, owner_id: str, provider: str, api_key: str) -> ProviderCredential:
        if self._cipher is None:
            raise ProviderCredentialRejected("provider keys are not enabled on this server")
        key = normalize_provider_key(provider, api_key)
        return await self.store.put_credential(
            owner_id, provider, self._cipher.encrypt(key), key_hint(key)
        )

    async def describe(self, owner_id: str) -> tuple[ProviderCredential, ...]:
        return await self.store.list_credentials(owner_id)

    async def delete_key(self, owner_id: str, provider: str) -> bool:
        return await self.store.delete_credential(owner_id, provider)

    async def key_for(self, owner_id: str, provider: str) -> str | None:
        if self._cipher is None:
            return None
        stored = await self.store.get_credential(owner_id, provider)
        return None if stored is None else self._cipher.decrypt(stored[1])


class MemoryProviderAccountStore:
    def __init__(self) -> None:
        self._credentials: dict[tuple[str, str], tuple[ProviderCredential, bytes]] = {}
        self._requests: dict[str, ProviderRequest] = {}

    async def put_credential(
        self, owner_id: str, provider: str, ciphertext: bytes, hint: str
    ) -> ProviderCredential:
        now = datetime.now(UTC)
        existing = self._credentials.get((owner_id, provider))
        record = ProviderCredential(
            owner_id=owner_id,
            provider=provider,
            key_hint=hint,
            created_at=existing[0].created_at if existing else now,
            updated_at=now,
        )
        self._credentials[(owner_id, provider)] = (record, ciphertext)
        return record

    async def get_credential(
        self, owner_id: str, provider: str
    ) -> tuple[ProviderCredential, bytes] | None:
        return self._credentials.get((owner_id, provider))

    async def list_credentials(self, owner_id: str) -> tuple[ProviderCredential, ...]:
        return tuple(
            record for (owner, _), (record, _) in sorted(self._credentials.items()) if owner == owner_id
        )

    async def forget_owner(self, owner_id: str) -> None:
        for key in [key for key in self._credentials if key[0] == owner_id]:
            del self._credentials[key]
        for job_id in [j for j, request in self._requests.items() if request.owner_id == owner_id]:
            del self._requests[job_id]

    async def delete_credential(self, owner_id: str, provider: str) -> bool:
        return self._credentials.pop((owner_id, provider), None) is not None

    async def reserve_request(
        self, job_id: str, owner_id: str, provider: str, model: str
    ) -> ProviderRequest | None:
        if job_id in self._requests:
            return None
        record = ProviderRequest(
            job_id=job_id, owner_id=owner_id, provider=provider, model=model,
            reserved_at=datetime.now(UTC),
        )
        self._requests[job_id] = record
        return record

    async def record_request_id(self, job_id: str, request_id: str) -> ProviderRequest | None:
        existing = self._requests.get(job_id)
        if existing is None:
            return None
        if existing.request_id is None:
            existing = existing.model_copy(
                update={"request_id": request_id, "submitted_at": datetime.now(UTC)}
            )
            self._requests[job_id] = existing
        return existing

    async def get_request(self, job_id: str) -> ProviderRequest | None:
        return self._requests.get(job_id)


_CREDENTIAL_COLUMNS = "owner_id, provider, key_hint, created_at, updated_at"
_REQUEST_COLUMNS = "job_id, owner_id, provider, model, request_id, reserved_at, submitted_at"


class PostgresProviderAccountStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._owns_pool = False

    async def start(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=2)
            self._owns_pool = True
        try:
            migration = (
                files("magic_video_editor.migrations")
                .joinpath("018_provider_accounts.sql")
                .read_text()
            )
            async with self._pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", STUDIO_SCHEMA_ADVISORY_LOCK
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
            raise RuntimeError("provider account store pool is not started")
        return self._pool

    async def put_credential(
        self, owner_id: str, provider: str, ciphertext: bytes, hint: str
    ) -> ProviderCredential:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                INSERT INTO studio_provider_credentials (owner_id, provider, ciphertext, key_hint)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (owner_id, provider) DO UPDATE
                SET ciphertext = EXCLUDED.ciphertext, key_hint = EXCLUDED.key_hint,
                    updated_at = now()
                RETURNING {_CREDENTIAL_COLUMNS}
                """,
                owner_id, provider, ciphertext, hint,
            )
        return ProviderCredential(**dict(row))

    async def get_credential(
        self, owner_id: str, provider: str
    ) -> tuple[ProviderCredential, bytes] | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_CREDENTIAL_COLUMNS}, ciphertext FROM studio_provider_credentials "
                "WHERE owner_id = $1 AND provider = $2",
                owner_id, provider,
            )
        if row is None:
            return None
        values = dict(row)
        ciphertext = bytes(values.pop("ciphertext"))
        return ProviderCredential(**values), ciphertext

    async def list_credentials(self, owner_id: str) -> tuple[ProviderCredential, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {_CREDENTIAL_COLUMNS} FROM studio_provider_credentials "
                "WHERE owner_id = $1 ORDER BY provider",
                owner_id,
            )
        return tuple(ProviderCredential(**dict(row)) for row in rows)

    async def forget_owner(self, owner_id: str) -> None:
        async with self._require_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM studio_provider_credentials WHERE owner_id = $1", owner_id
            )
            await connection.execute("DELETE FROM studio_provider_requests WHERE owner_id = $1", owner_id)

    async def delete_credential(self, owner_id: str, provider: str) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "DELETE FROM studio_provider_credentials WHERE owner_id = $1 AND provider = $2",
                owner_id, provider,
            )
        return result.endswith(" 1")

    async def reserve_request(
        self, job_id: str, owner_id: str, provider: str, model: str
    ) -> ProviderRequest | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO studio_provider_requests (job_id, owner_id, provider, model) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (job_id) DO NOTHING "
                f"RETURNING {_REQUEST_COLUMNS}",
                job_id, owner_id, provider, model,
            )
        return None if row is None else ProviderRequest(**dict(row))

    async def record_request_id(self, job_id: str, request_id: str) -> ProviderRequest | None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE studio_provider_requests SET request_id = $2, submitted_at = now() "
                "WHERE job_id = $1 AND request_id IS NULL",
                job_id, request_id,
            )
            row = await connection.fetchrow(
                f"SELECT {_REQUEST_COLUMNS} FROM studio_provider_requests WHERE job_id = $1",
                job_id,
            )
        return None if row is None else ProviderRequest(**dict(row))

    async def get_request(self, job_id: str) -> ProviderRequest | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_REQUEST_COLUMNS} FROM studio_provider_requests WHERE job_id = $1",
                job_id,
            )
        return None if row is None else ProviderRequest(**dict(row))
