"""Invited versus public accounts, and what a public account may not use.

An *invited* account is one the operator chose: an address or domain on the
sign-in allowlist, or a cluster admin. Nothing else makes an account invited: an
access request or an invitation made in the app before they were closed grants
nothing. A *public* account got in only because public signup is switched on. The
classification is made per request from the current allowlist, so taking someone
off the list makes them public at once.

Public accounts never run on shared (house) compute or the house LLM key, and
never on MiniMax H3 unless the operator holds rights for it: the standard H3
Community License excludes the US, EU, UK and Republic of Korea, so
`MAGIC_VIDEO_EDITOR_PUBLIC_H3_ENABLED` stays off until account-scoped licence
acceptance exists. With no policy configured everyone is invited, which is how
desktop, local and test services behave.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from magic_video_editor.studio.auth import (
    EmailAllowlist,
    StudioAuthStore,
    user_for_actor,
)
from magic_video_editor.studio.limits import OwnerLimitExceeded

PUBLIC_H3_ENV = "MAGIC_VIDEO_EDITOR_PUBLIC_H3_ENABLED"

HOUSE_COMPUTE_UNAVAILABLE = "house_compute_unavailable"
MINIMAX_H3_UNAVAILABLE = "minimax_h3_unavailable"
DIRECTOR_UNAVAILABLE = "director_unavailable"
MACHINE_CANNOT_RUN = "machine_cannot_run"

OWN_COMPUTE_MESSAGE = (
    "Connect your own GPU on the Machines page or add a Higgsfield key to generate."
)
H3_MESSAGE = (
    "MiniMax H3 is not available for public accounts. Add a Higgsfield key on the "
    "Machines page to generate video with your own Higgsfield account."
)
DIRECTOR_MESSAGE = (
    "Automatic shot planning is not available for public accounts. "
    "Write the shots yourself."
)


class AccountTier(StrEnum):
    INVITED = "invited"
    PUBLIC = "public"


class AccountPolicyRefused(OwnerLimitExceeded):
    """New work this account may not start. Like the owner caps it refuses only
    new work, so routes that already map those caps map this one too (403)."""


def own_compute_required(message: str = OWN_COMPUTE_MESSAGE) -> AccountPolicyRefused:
    return AccountPolicyRefused(HOUSE_COMPUTE_UNAVAILABLE, message)


def machine_cannot_run(message: str) -> AccountPolicyRefused:
    """None of the owner's machines can run this work; refused before it queues."""
    return AccountPolicyRefused(MACHINE_CANNOT_RUN, message)


def h3_unavailable() -> AccountPolicyRefused:
    return AccountPolicyRefused(MINIMAX_H3_UNAVAILABLE, H3_MESSAGE)


def director_unavailable() -> AccountPolicyRefused:
    return AccountPolicyRefused(DIRECTOR_UNAVAILABLE, DIRECTOR_MESSAGE)


@dataclass(frozen=True)
class AccountPolicy:
    """`tier_for` classifies an owner id; `None` means everyone is invited."""

    tier_for: Callable[[str], Awaitable[AccountTier]] | None = None
    public_h3_enabled: bool = False

    async def tier(self, owner_id: str) -> AccountTier:
        if self.tier_for is None:
            return AccountTier.INVITED
        return await self.tier_for(owner_id)

    async def is_public(self, owner_id: str) -> bool:
        return await self.tier(owner_id) is AccountTier.PUBLIC

    async def h3_allowed(self, owner_id: str) -> bool:
        return self.public_h3_enabled or not await self.is_public(owner_id)

    async def require_h3(self, owner_id: str) -> None:
        if not await self.h3_allowed(owner_id):
            raise h3_unavailable()


class AllowlistAccounts:
    """Classify an owner from the sign-in allowlist as it stands now.

    A known user who is neither allowlisted nor an admin is public whether or not
    signup is open, so closing signup again never upgrades accounts that joined
    while it was open. Only an owner with no user row depends on the switch:
    public while signup is open (unknown means not invited), invited while it is
    closed, which keeps gateway and pre-switch actors exactly as before."""

    def __init__(
        self,
        users: StudioAuthStore,
        allowlist: EmailAllowlist,
        is_admin: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._users = users
        self._allowlist = allowlist
        self._is_admin = is_admin

    async def __call__(self, owner_id: str) -> AccountTier:
        user = await user_for_actor(self._users, owner_id)
        if user is None and not self._allowlist.public_signup:
            return AccountTier.INVITED
        if user is not None and self._allowlist.invites(user.email):
            return AccountTier.INVITED
        if self._is_admin is not None and await self._is_admin(owner_id):
            return AccountTier.INVITED
        return AccountTier.PUBLIC


def account_policy_from_env(
    values: Mapping[str, str],
    users: StudioAuthStore,
    is_admin: Callable[[str], Awaitable[bool]] | None = None,
) -> AccountPolicy:
    return AccountPolicy(
        tier_for=AllowlistAccounts(users, EmailAllowlist.from_env(values), is_admin),
        public_h3_enabled=values.get(PUBLIC_H3_ENV, "false").strip().lower() == "true",
    )
