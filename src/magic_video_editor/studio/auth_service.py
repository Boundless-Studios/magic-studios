"""Sign-in orchestration: Auth0, IAP provisioning, and sessions.

Everything that decides *who someone is* lives here; `routes.py` only carries the
HTTP shape (cookies, redirects, status codes).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from magic_video_editor.studio.auth import (
    SESSION_LAST_SEEN_THROTTLE_SECONDS,
    SESSION_TOKEN_BYTES,
    SESSION_TTL,
    Auth0Client,
    Auth0Identity,
    Auth0IdentityProvider,
    EmailAllowlist,
    SignInMethod,
    StudioAuthStore,
    StudioUser,
    email_actor_id,
    generate_token,
    google_actor_id,
    google_sub_from_iap_subject,
    hash_token,
)

logger = logging.getLogger(__name__)


def build_studio_auth_service(
    store: StudioAuthStore,
    *,
    projects: ProjectOwnershipReassigner | None = None,
    auth0: Auth0IdentityProvider | None = None,
) -> StudioAuthService:
    """Assemble the sign-in service from the environment. With no Auth0 tenant
    configured the cookie half is simply unavailable (local dev signs in through
    the gateway header instead)."""
    return StudioAuthService(
        store,
        allowlist=EmailAllowlist.from_env(),
        auth0=auth0 if auth0 is not None else Auth0Client.from_env(),
        projects=projects,
    )


class ProjectOwnershipReassigner(Protocol):
    async def reassign_project_ownership(
        self, previous_owner_id: str, new_owner_id: str
    ) -> tuple[str, ...]: ...


class SignInRejected(Exception):
    """The identity is real but not allowed in (unverified email, for instance)."""


class EmailNotVerified(SignInRejected):
    """A new password account that has not followed its verification link yet --
    named on the sign-in page, because the person can act on it."""


class EmailNotAllowed(SignInRejected):
    """The address is not on the allowlist -- the one rejection the sign-in page
    names, because it is the one a person can act on."""


class StudioAuthService:
    def __init__(
        self,
        store: StudioAuthStore,
        *,
        allowlist: EmailAllowlist,
        auth0: Auth0IdentityProvider | None = None,
        projects: ProjectOwnershipReassigner | None = None,
    ) -> None:
        """Who may sign in comes from `allowlist` alone: its addresses and domains, or
        any verified address while public signup is on. An access request or an
        invitation made in the app never lets anyone in."""
        self._store = store
        self._allowlist = allowlist
        self._auth0 = auth0
        self._projects = projects

    @property
    def store(self) -> StudioAuthStore:
        return self._store

    @property
    def auth0(self) -> Auth0IdentityProvider | None:
        return self._auth0

    @property
    def allowlist(self) -> EmailAllowlist:
        return self._allowlist

    # -- sessions ---------------------------------------------------------

    async def issue_session(
        self,
        user: StudioUser,
        *,
        sign_in_method: SignInMethod,
        user_agent: str | None = None,
    ) -> str:
        """Create a session row and return the plaintext cookie value."""
        token = generate_token(SESSION_TOKEN_BYTES)
        await self._store.create_session(
            session_id=hash_token(token),
            user_id=user.user_id,
            sign_in_method=sign_in_method,
            expires_at=datetime.now(UTC) + SESSION_TTL,
            user_agent=user_agent,
        )
        return token

    async def resolve_session(
        self, token: str
    ) -> tuple[StudioUser, SignInMethod] | None:
        """The signed-in user behind a cookie value, renewing the session as it goes.

        An expired row is deleted rather than left to accumulate, and so is the
        session of someone no longer allowed in (taken off the allowlist, or public
        signup switched off, even while they were signing in)."""
        session_id = hash_token(token)
        session = await self._store.get_session(session_id)
        if session is None:
            return None
        now = datetime.now(UTC)
        if session.expires_at <= now:
            await self._store.delete_session(session_id)
            return None
        user = await self._store.get_user(session.user_id)
        if user is None or not await self.may_sign_in(user.email):
            await self._store.delete_session(session_id)
            return None
        if (now - session.last_seen_at).total_seconds() >= (
            SESSION_LAST_SEEN_THROTTLE_SECONDS
        ):
            await self._store.touch_session(
                session_id, seen_at=now, expires_at=now + SESSION_TTL
            )
        return user, session.sign_in_method

    async def sign_out(self, token: str) -> None:
        await self._store.delete_session(hash_token(token))

    # -- federated identities ---------------------------------------------

    async def may_sign_in(self, email: str) -> bool:
        """On the allowlist, or any verified address while public signup is on.
        Checked at sign-in and again whenever a session or API key is used."""
        return self._allowlist.permits(email)

    async def sign_in_with_auth0(
        self, identity: Auth0Identity
    ) -> tuple[StudioUser, SignInMethod]:
        if not identity.email_verified:
            raise EmailNotVerified("the identity has no verified email")
        if not await self.may_sign_in(identity.email):
            raise EmailNotAllowed("email is not allowlisted")
        google_sub = identity.google_sub
        if google_sub is None:
            user = await self._store.upsert_user(
                email=identity.email, display_name=identity.display_name
            )
            return user, SignInMethod.AUTH0
        user = await self._adopt_google_identity(
            email=identity.email,
            google_sub=google_sub,
            display_name=identity.display_name,
        )
        return user, SignInMethod.GOOGLE

    async def provision_from_iap(self, *, subject: str, email: str) -> StudioUser:
        """Auto-provision the user behind a still-live IAP assertion, so nobody is
        signed out on the day IAP comes off."""
        return await self._adopt_google_identity(
            email=email,
            google_sub=google_sub_from_iap_subject(subject),
            display_name=None,
        )

    async def _adopt_google_identity(
        self, *, email: str, google_sub: str, display_name: str | None
    ) -> StudioUser:
        existing = await self._store.get_user_by_email(email)
        needs_reassignment = existing is not None and existing.google_sub is None
        user = await self._store.upsert_user(
            email=email, google_sub=google_sub, display_name=display_name
        )
        if needs_reassignment and self._projects is not None:
            moved = await self._projects.reassign_project_ownership(
                email_actor_id(email), google_actor_id(google_sub)
            )
            if moved:
                logger.info(
                    "reassigned %s project(s) from the email identity to Google",
                    len(moved),
                )
        return user
