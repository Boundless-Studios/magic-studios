"""The studio's sign-in: Auth0 as the identity provider, plus users and sessions.

Identity is the same string every `/studio/*` route already authorizes against, so
a project keeps its owner across the move off IAP:

* an Auth0 `google-oauth2|<id>` subject is `iap:accounts.google.com:<id>` --
  byte-for-byte what IAP produced, because IAP's `sub` claim is itself
  `accounts.google.com:<id>`;
* any other Auth0 subject is `email:<lowercased email>`, and is reassigned once to
  the Google actor when the same person later signs in with Google.

Only the SHA-256 of a session token is stored. The plaintext lives in the user's
cookie and nowhere else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib.resources import files
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

import asyncpg
import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from google.auth import jwt as google_jwt
from starlette.applications import Starlette
from starlette.responses import Response

from magic_video_editor.studio.models import FrozenModel
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK

SESSION_COOKIE_NAME = "mvs_session"
SESSION_TTL = timedelta(days=30)
SESSION_TOKEN_BYTES = 32
# `last_seen_at` is an activity hint, not an audit log, so one write per session
# per minute is enough and keeps a read burst from costing a write each.
SESSION_LAST_SEEN_THROTTLE_SECONDS = 60.0

OAUTH_STATE_COOKIE_NAME = "mvs_oauth"
OAUTH_STATE_TTL_SECONDS = 600
OAUTH_STATE_BYTES = 32
OAUTH_SCOPE = "openid profile email"

GOOGLE_ACTOR_PREFIX = "iap:accounts.google.com:"
EMAIL_ACTOR_PREFIX = "email:"
AUTH0_GOOGLE_SUB_PREFIX = "google-oauth2|"

JWKS_CACHE_SECONDS = 3600.0
HTTP_TIMEOUT_SECONDS = 10.0
ID_TOKEN_CLOCK_SKEW_SECONDS = 60

logger = logging.getLogger(__name__)


class SignInMethod(StrEnum):
    AUTH0 = "auth0"
    GOOGLE = "google"
    IAP = "iap"


class StudioUser(FrozenModel):
    user_id: str
    email: str
    google_sub: str | None = None
    display_name: str | None = None
    created_at: datetime
    last_sign_in_at: datetime | None = None

    @property
    def actor_id(self) -> str:
        if self.google_sub is not None:
            return google_actor_id(self.google_sub)
        return email_actor_id(self.email)


class StudioSession(FrozenModel):
    """A live session row. `session_id` is the SHA-256 of the cookie value."""

    session_id: str
    user_id: str
    sign_in_method: SignInMethod
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    user_agent: str | None = None


class ResolvedIdentity(FrozenModel):
    """Who the current request is, once the actor has been resolved."""

    actor_id: str
    email: str | None = None
    display_name: str | None = None
    sign_in_method: SignInMethod | None = None


class Auth0Identity(FrozenModel):
    """The verified claims behind an Auth0 id token."""

    subject: str
    email: str
    email_verified: bool
    display_name: str | None = None

    @property
    def google_sub(self) -> str | None:
        """The bare Google id when Auth0 federated to Google, else `None`."""
        if not self.subject.startswith(AUTH0_GOOGLE_SUB_PREFIX):
            return None
        return self.subject[len(AUTH0_GOOGLE_SUB_PREFIX) :] or None


def google_actor_id(google_sub: str) -> str:
    return f"{GOOGLE_ACTOR_PREFIX}{google_sub}"


def email_actor_id(email: str) -> str:
    return f"{EMAIL_ACTOR_PREFIX}{normalize_email(email)}"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def google_sub_from_iap_subject(subject: str) -> str:
    """IAP's `sub` is `accounts.google.com:<sub>`; the OIDC `sub` is the bare id."""
    return subject.rpartition(":")[2]


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token(size: int) -> str:
    return secrets.token_urlsafe(size)


def safe_return_path(return_to: str | None) -> str | None:
    """A same-origin absolute path, or `None` -- an open redirect out of a sign-in
    link is a phishing primitive, so anything else is discarded rather than fixed."""
    if not return_to:
        return None
    if (
        not return_to.startswith("/")
        or return_to.startswith("//")
        or "\\" in return_to
        or "\x00" in return_to
    ):
        return None
    return return_to


PUBLIC_SIGNUP_ENV = "MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED"


@dataclass(frozen=True)
class EmailAllowlist:
    """Who may sign in. Invitation lists are closed by default: empty on both axes
    means nobody. Public signup must be switched on explicitly, and the auth
    service still requires a verified provider email either way."""

    emails: frozenset[str]
    domains: frozenset[str]
    public_signup: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EmailAllowlist:
        values = os.environ if environ is None else environ
        return cls(
            emails=frozenset(
                _split_list(values.get("MAGIC_VIDEO_EDITOR_ALLOWED_EMAILS", ""))
            ),
            domains=frozenset(
                _split_list(values.get("MAGIC_VIDEO_EDITOR_ALLOWED_EMAIL_DOMAINS", ""))
            ),
            public_signup=values.get(PUBLIC_SIGNUP_ENV, "false").strip().lower()
            == "true",
        )

    def permits(self, email: str) -> bool:
        if self.invites(email):
            return True
        return self.public_signup and _is_plain_address(normalize_email(email))

    def invites(self, email: str) -> bool:
        """On the invitation lists themselves, regardless of public signup."""
        normalized = normalize_email(email)
        if not self.emails and not self.domains:
            return False
        if normalized in self.emails:
            return True
        domain = normalized.rpartition("@")[2]
        return bool(domain) and domain in self.domains


def _is_plain_address(normalized: str) -> bool:
    local, separator, domain = normalized.partition("@")
    return bool(
        local
        and separator
        and domain
        and "@" not in domain
        and not any(character.isspace() for character in normalized)
    )


def _split_list(raw: str) -> Iterable[str]:
    return (item.strip().lower() for item in raw.split(",") if item.strip())


async def user_for_actor(users: StudioAuthStore, actor_id: str) -> StudioUser | None:
    """The user row whose actor id is exactly `actor_id`, or `None`."""
    if actor_id.startswith(GOOGLE_ACTOR_PREFIX):
        user = await users.get_user_by_google_sub(actor_id.removeprefix(GOOGLE_ACTOR_PREFIX))
    elif actor_id.startswith(EMAIL_ACTOR_PREFIX):
        user = await users.get_user_by_email(actor_id.removeprefix(EMAIL_ACTOR_PREFIX))
    else:
        return None
    return user if user is not None and user.actor_id == actor_id else None


class StudioAuthStore(Protocol):
    async def get_user(self, user_id: str) -> StudioUser | None: ...

    async def get_user_by_email(self, email: str) -> StudioUser | None: ...

    async def get_user_by_google_sub(self, google_sub: str) -> StudioUser | None: ...

    async def upsert_user(
        self,
        *,
        email: str,
        google_sub: str | None = None,
        display_name: str | None = None,
    ) -> StudioUser:
        """Create or update the user for `email`, recording the sign-in moment."""
        ...

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        sign_in_method: SignInMethod,
        expires_at: datetime,
        user_agent: str | None,
    ) -> StudioSession: ...

    async def get_session(self, session_id: str) -> StudioSession | None: ...

    async def touch_session(
        self, session_id: str, *, seen_at: datetime, expires_at: datetime
    ) -> None: ...

    async def delete_user(self, user_id: str) -> None:
        """Delete the user with every session (account deletion)."""
        ...

    async def delete_session(self, session_id: str) -> None: ...

    async def delete_sessions_for_user(self, user_id: str) -> None:
        """Sign the user out everywhere (access taken back)."""
        ...


class MemoryStudioAuthStore:
    def __init__(self) -> None:
        self._users: dict[str, StudioUser] = {}
        self._sessions: dict[str, StudioSession] = {}

    async def get_user(self, user_id: str) -> StudioUser | None:
        return self._users.get(user_id)

    async def get_user_by_email(self, email: str) -> StudioUser | None:
        normalized = normalize_email(email)
        return next(
            (user for user in self._users.values() if user.email == normalized), None
        )

    async def get_user_by_google_sub(self, google_sub: str) -> StudioUser | None:
        return next(
            (user for user in self._users.values() if user.google_sub == google_sub),
            None,
        )

    async def upsert_user(
        self,
        *,
        email: str,
        google_sub: str | None = None,
        display_name: str | None = None,
    ) -> StudioUser:
        now = datetime.now(UTC)
        existing = await self.get_user_by_email(email)
        if existing is None:
            user = StudioUser(
                user_id=str(uuid4()),
                email=normalize_email(email),
                google_sub=google_sub,
                display_name=display_name,
                created_at=now,
                last_sign_in_at=now,
            )
        else:
            user = existing.model_copy(
                update={
                    "google_sub": google_sub or existing.google_sub,
                    "display_name": display_name or existing.display_name,
                    "last_sign_in_at": now,
                }
            )
        self._users[user.user_id] = user
        return user

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        sign_in_method: SignInMethod,
        expires_at: datetime,
        user_agent: str | None,
    ) -> StudioSession:
        now = datetime.now(UTC)
        session = StudioSession(
            session_id=session_id,
            user_id=user_id,
            sign_in_method=sign_in_method,
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
            user_agent=user_agent,
        )
        self._sessions[session_id] = session
        return session

    async def get_session(self, session_id: str) -> StudioSession | None:
        return self._sessions.get(session_id)

    async def touch_session(
        self, session_id: str, *, seen_at: datetime, expires_at: datetime
    ) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            self._sessions[session_id] = session.model_copy(
                update={"last_seen_at": seen_at, "expires_at": expires_at}
            )

    async def delete_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)
        for session_id in [s for s, session in self._sessions.items() if session.user_id == user_id]:
            del self._sessions[session_id]

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def delete_sessions_for_user(self, user_id: str) -> None:
        for session_id in [s for s, session in self._sessions.items() if session.user_id == user_id]:
            del self._sessions[session_id]


_USER_COLUMNS = "user_id, email, google_sub, display_name, created_at, last_sign_in_at"
_SESSION_COLUMNS = (
    "session_id, user_id, sign_in_method, created_at, expires_at, "
    "last_seen_at, user_agent"
)


class PostgresStudioAuthStore:
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
                .joinpath("013_studio_auth.sql")
                .read_text()
            )
            async with self._pool.acquire() as connection, connection.transaction():
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
            raise RuntimeError("studio auth store pool is not started")
        return self._pool

    async def _fetch_user(self, where: str, value: str) -> StudioUser | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_USER_COLUMNS} FROM studio_users WHERE {where}", value
            )
        return None if row is None else _row_to_user(row)

    async def get_user(self, user_id: str) -> StudioUser | None:
        return await self._fetch_user("user_id = $1::uuid", user_id)

    async def get_user_by_email(self, email: str) -> StudioUser | None:
        return await self._fetch_user("email = $1", normalize_email(email))

    async def get_user_by_google_sub(self, google_sub: str) -> StudioUser | None:
        return await self._fetch_user("google_sub = $1", google_sub)

    async def upsert_user(
        self,
        *,
        email: str,
        google_sub: str | None = None,
        display_name: str | None = None,
    ) -> StudioUser:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                INSERT INTO studio_users
                    (user_id, email, google_sub, display_name, last_sign_in_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (email) DO UPDATE SET
                    google_sub = coalesce(
                        EXCLUDED.google_sub, studio_users.google_sub
                    ),
                    display_name = coalesce(
                        EXCLUDED.display_name, studio_users.display_name
                    ),
                    last_sign_in_at = now()
                RETURNING {_USER_COLUMNS}
                """,
                uuid4(),
                normalize_email(email),
                google_sub,
                display_name,
            )
        return _row_to_user(row)

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        sign_in_method: SignInMethod,
        expires_at: datetime,
        user_agent: str | None,
    ) -> StudioSession:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"""
                INSERT INTO studio_sessions
                    (session_id, user_id, sign_in_method, expires_at, user_agent)
                VALUES ($1, $2::uuid, $3, $4, $5)
                RETURNING {_SESSION_COLUMNS}
                """,
                session_id,
                user_id,
                str(sign_in_method),
                expires_at,
                user_agent,
            )
        return _row_to_session(row)

    async def get_session(self, session_id: str) -> StudioSession | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_SESSION_COLUMNS} FROM studio_sessions WHERE session_id = $1",
                session_id,
            )
        return None if row is None else _row_to_session(row)

    async def touch_session(
        self, session_id: str, *, seen_at: datetime, expires_at: datetime
    ) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE studio_sessions SET last_seen_at = $2, expires_at = $3 "
                "WHERE session_id = $1",
                session_id,
                seen_at,
                expires_at,
            )

    async def delete_user(self, user_id: str) -> None:
        # Sessions cascade. Login tokens predate Auth0 but are keyed by email.
        async with self._require_pool().acquire() as connection, connection.transaction():
            email = await connection.fetchval(
                "DELETE FROM studio_users WHERE user_id = $1::uuid RETURNING email", user_id
            )
            if email is not None:
                await connection.execute("DELETE FROM studio_login_tokens WHERE email = $1", email)

    async def delete_session(self, session_id: str) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "DELETE FROM studio_sessions WHERE session_id = $1", session_id
            )

    async def delete_sessions_for_user(self, user_id: str) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "DELETE FROM studio_sessions WHERE user_id = $1::uuid", user_id
            )


def _row_to_user(row: Any) -> StudioUser:
    return StudioUser(
        user_id=str(row["user_id"]),
        email=row["email"],
        google_sub=row["google_sub"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_sign_in_at=row["last_sign_in_at"],
    )


def _row_to_session(row: Any) -> StudioSession:
    return StudioSession(
        session_id=row["session_id"],
        user_id=str(row["user_id"]),
        sign_in_method=SignInMethod(row["sign_in_method"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        user_agent=row["user_agent"],
    )


class Auth0Settings(FrozenModel):
    """The tenant this deployment signs users in against."""

    domain: str
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Auth0Settings | None:
        values = os.environ if environ is None else environ
        domain = values.get("MAGIC_VIDEO_EDITOR_AUTH0_DOMAIN", "").strip()
        client_id = values.get("MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_ID", "").strip()
        client_secret = values.get("MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_SECRET", "").strip()
        if not domain or not client_id or not client_secret:
            return None
        return cls(domain=domain, client_id=client_id, client_secret=client_secret)

    @property
    def issuer(self) -> str:
        return f"https://{self.domain}/"

    @property
    def authorization_endpoint(self) -> str:
        return f"https://{self.domain}/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"https://{self.domain}/oauth/token"

    @property
    def jwks_uri(self) -> str:
        return f"https://{self.domain}/.well-known/jwks.json"

    def logout_url(self, return_to: str) -> str:
        query = urlencode({"client_id": self.client_id, "returnTo": return_to})
        return f"https://{self.domain}/v2/logout?{query}"


class IdTokenVerifier(Protocol):
    async def verify(self, raw_id_token: str) -> Auth0Identity:
        """The verified identity in an id token, or `ValueError`."""
        ...


class Auth0JwksVerifier:
    """Verifies an Auth0 id token against the tenant's published signing keys.

    The key set is cached: a sign-in must not cost a JWKS fetch, and a rotated key
    is picked up by refetching once when an unknown `kid` shows up.
    """

    def __init__(
        self,
        settings: Auth0Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = JWKS_CACHE_SECONDS,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache_seconds = cache_seconds
        self._keys: dict[str, str] = {}
        self._fetched_at = 0.0

    async def verify(self, raw_id_token: str) -> Auth0Identity:
        keys = await self._signing_keys(_unverified_key_id(raw_id_token))
        claims = google_jwt.decode(
            raw_id_token,
            certs=keys,
            audience=self._settings.client_id,
            clock_skew_in_seconds=ID_TOKEN_CLOCK_SKEW_SECONDS,
        )
        return identity_from_claims(claims, issuer=self._settings.issuer)

    async def _signing_keys(self, key_id: str | None) -> dict[str, str]:
        stale = time.monotonic() - self._fetched_at >= self._cache_seconds
        if not self._keys or stale or (key_id is not None and key_id not in self._keys):
            self._keys = jwks_to_public_keys(await self._fetch_jwks())
            self._fetched_at = time.monotonic()
        return self._keys

    async def _fetch_jwks(self) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.get(self._settings.jwks_uri)
            response.raise_for_status()
            return dict(response.json())
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(self._settings.jwks_uri)
            response.raise_for_status()
            return dict(response.json())


def identity_from_claims(claims: dict[str, Any], *, issuer: str) -> Auth0Identity:
    if claims.get("iss") != issuer:
        raise ValueError("unexpected id token issuer")
    subject = claims.get("sub")
    email = claims.get("email")
    if not isinstance(subject, str) or not isinstance(email, str):
        raise ValueError("id token is missing sub or email")  # noqa: TRY004
    display_name = claims.get("name")
    return Auth0Identity(
        subject=subject,
        email=email,
        email_verified=bool(claims.get("email_verified")),
        display_name=display_name if isinstance(display_name, str) else None,
    )


def jwks_to_public_keys(document: dict[str, Any]) -> dict[str, str]:
    """`kid` -> PEM public key, for the RSA signing keys in a JWKS document."""
    keys: dict[str, str] = {}
    for key in document.get("keys", []):
        kid = key.get("kid")
        if not isinstance(kid, str) or key.get("kty") != "RSA":
            continue
        modulus = _decode_base64url(key.get("n"))
        exponent = _decode_base64url(key.get("e"))
        if modulus is None or exponent is None:
            continue
        public_key = RSAPublicNumbers(
            e=int.from_bytes(exponent, "big"), n=int.from_bytes(modulus, "big")
        ).public_key()
        keys[kid] = public_key.public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")
    return keys


def _decode_base64url(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return None


def _unverified_key_id(raw_id_token: str) -> str | None:
    """The `kid` in the token header, read before any verification purely to decide
    whether the cached key set can answer or has to be refetched."""
    encoded_header = raw_id_token.partition(".")[0]
    raw = _decode_base64url(encoded_header)
    if raw is None:
        return None
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    key_id = header.get("kid") if isinstance(header, dict) else None
    return key_id if isinstance(key_id, str) else None


class Auth0IdentityProvider(Protocol):
    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str: ...

    def logout_url(self, return_to: str) -> str: ...

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> Auth0Identity:
        """The verified identity behind an authorization code, or `ValueError`."""
        ...


class Auth0Client:
    def __init__(
        self,
        settings: Auth0Settings,
        *,
        client: httpx.AsyncClient | None = None,
        verifier: IdTokenVerifier | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._verifier = (
            verifier
            if verifier is not None
            else Auth0JwksVerifier(settings, client=client)
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Auth0Client | None:
        settings = Auth0Settings.from_env(environ)
        return None if settings is None else cls(settings)

    def authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        signup: bool = False,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        # Universal Login opens on its sign-up form; signing in offers the account picker.
        params |= {"screen_hint": "signup"} if signup else {"prompt": "select_account"}
        return f"{self._settings.authorization_endpoint}?{urlencode(params)}"

    def logout_url(self, return_to: str) -> str:
        return self._settings.logout_url(return_to)

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> Auth0Identity:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        token_response = await self._post_token(payload)
        raw_id_token = token_response.get("id_token")
        if not isinstance(raw_id_token, str):
            # A malformed provider response is a failed sign-in, not a programming
            # error, so the caller's `ValueError` handler owns it.
            raise ValueError("Auth0 token response carried no id_token")  # noqa: TRY004
        return await self._verifier.verify(raw_id_token)

    async def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.post(
                self._settings.token_endpoint, data=payload
            )
            response.raise_for_status()
            return dict(response.json())
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(self._settings.token_endpoint, data=payload)
            response.raise_for_status()
            return dict(response.json())


def pkce_pair() -> tuple[str, str]:
    """A PKCE `(verifier, S256 challenge)` pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class OAuthHandoff(FrozenModel):
    """The state carried across the Auth0 round trip, in a short-lived cookie."""

    state: str
    code_verifier: str
    return_to: str | None = None


def encode_oauth_handoff(handoff: OAuthHandoff) -> str:
    raw = handoff.model_dump_json().encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_oauth_handoff(encoded: str) -> OAuthHandoff | None:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return OAuthHandoff.model_validate_json(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def set_oauth_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        value,
        max_age=OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_oauth_cookie(response: Response) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        httponly=True,
        secure=session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def auth_service_from_app(app: Starlette) -> Any | None:
    """`app.state` is Starlette's untyped namespace, so the miss has to be tolerated:
    a router mounted without an auth service simply has no cookie sign-in. Typed
    `Any` because `StudioAuthService` imports from this module."""
    return getattr(app.state, "studio_auth", None)


def session_cookie_secure(environ: dict[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get("MAGIC_VIDEO_EDITOR_AUTH_COOKIE_SECURE", "true").lower() != "false"


def is_valid_user_id(candidate: str) -> bool:
    try:
        UUID(candidate)
    except ValueError:
        return False
    return True
