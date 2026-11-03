"""Who operates this Studio, and which compute their work may share.

The operators are the addresses in `MAGIC_VIDEO_EDITOR_ADMIN_EMAILS`. They may see
every machine and the pool's global status views, and submit untenanted house work.
Their own generation runs like anyone else's: on machines they enrolled, or on the
house machines. An extension may subclass `OperatorAccess` to give operators a
shared tenant that eligible machines also lease from (the hosted service's admin
cluster); every hook below defaults to sharing nothing.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from magic_video_editor.studio.auth import StudioAuthStore, user_for_actor
from magic_video_editor.studio.enrolment import StudioEnrolmentStore

ADMIN_EMAILS_ENV = "MAGIC_VIDEO_EDITOR_ADMIN_EMAILS"


def admin_emails_from_env(value: str | None) -> set[str]:
    return {email.strip().lower() for email in (value or "").split(",") if email.strip()}


class OperatorAccess:
    # Tenant ids that stand for shared operator work rather than one owner. Work
    # under one is house work for routing (see `machine_features.require_route`).
    shared_tenants: frozenset[str] = frozenset()

    def __init__(self, users: StudioAuthStore, admin_emails: Iterable[str], enrolment: StudioEnrolmentStore) -> None:
        self.users = users
        self.enrolment = enrolment
        self.admin_emails = {email.strip().lower() for email in admin_emails if email.strip()}

    async def is_admin(self, actor_id: str) -> bool:
        user = await user_for_actor(self.users, actor_id)
        return user is not None and user.email.lower() in self.admin_emails

    async def shared_tenant(self, actor_id: str) -> str | None:
        """The tenant this actor's work runs under instead of their own, if any."""
        return None

    async def serves_shared(self, worker_id: str, tenant_id: str | None) -> bool:
        """Whether a machine registered under `tenant_id` may run shared work."""
        return False

    async def shared_worker_views(self, pool: Any) -> list[Any]:
        """The pool's machines that can take shared work, for discovery."""
        return []
