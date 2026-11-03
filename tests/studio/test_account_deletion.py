"""Deleting an account removes everything held for it -- projects, movies and
their files, saved provider keys, API keys, machines, sessions, usage and the
user row -- and nothing that belongs to anyone else."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.extensions import Extension
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.api_keys import MemoryStudioApiKeyStore
from magic_video_editor.studio.auth import MemoryStudioAuthStore, email_actor_id
from magic_video_editor.studio.limits import MemoryStudioOwnerUsageStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.provider_accounts import (
    HIGGSFIELD,
    MemoryProviderAccountStore,
    ProviderAccounts,
)
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher

from .test_cross_account_isolation import _png_bytes
from .test_public_accounts import _enrolled

EMAIL = "owner@example.com"
ACTOR = email_actor_id(EMAIL)
OTHER = "email:other@example.com"


class _Account:
    def __init__(self) -> None:
        self.assets = MemoryAssetStore()
        self.usage = MemoryStudioOwnerUsageStore()
        self.providers = MemoryProviderAccountStore()
        self.enrolment = asyncio.run(_enrolled(ACTOR))
        self.service = StudioService(
            store=MemoryStudioStore(), assets=self.assets, jobs=MemoryJobStore(),
            dispatcher=NullTaskDispatcher(), owner_usage=self.usage, enrolment=self.enrolment,
            provider_accounts=ProviderAccounts(self.providers, None),
        )
        self.movies = MovieService(MemoryMovieStore(), self.assets)
        self.users = MemoryStudioAuthStore()
        self.keys = MemoryStudioApiKeyStore()
        # What an extension holds for an account (the hosted service's tips) is
        # erased through the extension's own eraser.
        self.erased: list[str] = []

        async def erase(owner_id: str) -> None:
            self.erased.append(owner_id)

        runtime = InMemoryRuntime(
            MemoryJobStore(), self.assets, studio_service=self.service,
            extensions=(Extension(name="test", owner_erasers=(erase,)),),
        )
        runtime.movie_service = self.movies
        runtime.studio_auth_store = self.users
        runtime.studio_api_keys = self.keys
        runtime.studio_enrolment = self.enrolment
        app = create_app(runtime, service_role=ServiceRole.API)
        self.owner = TestClient(app, headers={"X-Studio-Actor-Id": ACTOR})
        self.other = TestClient(app, headers={"X-Studio-Actor-Id": OTHER})
        self.app = app

    def project(self, client: TestClient, name: str) -> tuple[str, str]:
        project_id = client.post("/studio/projects", json={"name": name}).json()["project_id"]
        upload = client.post(
            f"/studio/projects/{project_id}/subject/uploads",
            json={"content_type": "image/png", "expected_revision": 1},
        ).json()
        self.assets.assets[upload["key"]] = _png_bytes()
        client.post(
            f"/studio/projects/{project_id}/subject/settle",
            json={"expected_revision": 1, "asset_key": upload["key"]},
        )
        return project_id, upload["key"]


def test_deleting_an_account_removes_everything_held_for_it_and_nothing_else() -> None:
    account = _Account()
    user = asyncio.run(account.users.upsert_user(email=EMAIL))
    project_id, _ = account.project(account.owner, "Ember Knight")
    _, other_key = account.project(account.other, "Theirs")

    async def seed() -> str:
        movie = await account.movies.create(ACTOR, "Heist")
        await account.assets.write_once(f"outputs/movies/{movie.movie_id}/takes/t.mp4", b"m", "video/mp4")
        await account.providers.put_credential(ACTOR, HIGGSFIELD, b"cipher", "…1234")
        await account.keys.create(actor_id=ACTOR, name="agent", key_hash="h" * 64)
        return movie.movie_id

    movie_id = asyncio.run(seed())

    refused = account.owner.request("DELETE", "/studio/account", json={"confirm_email": "wrong@example.com"})
    assert refused.status_code == 422
    assert account.owner.get(f"/studio/projects/{project_id}").status_code == 200

    deleted = account.owner.request("DELETE", "/studio/account", json={"confirm_email": "Owner@Example.com"})

    assert deleted.status_code == 200, deleted.text
    assert [key for key in account.assets.assets if project_id in key or movie_id in key] == []
    assert account.owner.get("/studio/projects").json() == []
    assert asyncio.run(account.movies.store.list(ACTOR)) == []
    assert asyncio.run(account.providers.list_credentials(ACTOR)) == ()
    assert asyncio.run(account.keys.list_for_actor(ACTOR)) == ()
    assert asyncio.run(account.enrolment.list_workers(ACTOR)) == ()
    assert asyncio.run(account.usage.stored_bytes(ACTOR)) == 0
    assert asyncio.run(account.users.get_user(user.user_id)) is None
    assert account.erased == [ACTOR]
    # Someone else's work is untouched.
    assert other_key in account.assets.assets
    assert len(account.other.get("/studio/projects").json()) == 1


def test_an_api_key_cannot_delete_an_account() -> None:
    account = _Account()
    asyncio.run(account.users.upsert_user(email=EMAIL))
    keyed = TestClient(account.app, headers={"Authorization": "Bearer mvs_not_a_browser_session"})

    response = keyed.request("DELETE", "/studio/account", json={"confirm_email": EMAIL})

    assert response.status_code in {401, 422}
    assert asyncio.run(account.users.get_user_by_email(EMAIL)) is not None
