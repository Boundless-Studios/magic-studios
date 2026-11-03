"""Movies for public accounts: own machines only, no MiniMax H3 without the
operator flag, no house director. Invited accounts are unchanged."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.movies.routes import create_movie_router
from magic_video_editor.movies.service import MovieError
from magic_video_editor.studio.accounts import (
    DIRECTOR_UNAVAILABLE,
    HOUSE_COMPUTE_UNAVAILABLE,
    MINIMAX_H3_UNAVAILABLE,
    AccountPolicy,
    AccountTier,
)
from magic_video_editor.studio.routes import studio_actor_id

from .test_service import setup


def _policy(*public: str, h3: bool = False) -> AccountPolicy:
    async def tier(owner_id: str) -> AccountTier:
        return AccountTier.PUBLIC if owner_id in public else AccountTier.INVITED

    return AccountPolicy(tier_for=tier, public_h3_enabled=h3)


async def _own_machine(owner: str) -> str:
    return owner


async def _no_machine(owner: str) -> None:
    return None


async def test_public_takes_need_their_own_machine_even_on_shared_compute() -> None:
    service, pool, movie = await setup()
    service.account_policy = _policy("owner", h3=True)
    service.tenant_for = _no_machine
    assert movie.compute_target == "shared"

    with pytest.raises(MovieError) as refused:
        await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r1")

    assert refused.value.status == 403 and refused.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert pool.calls == []
    assert (await service.get(movie.movie_id, "owner")).revision == movie.revision

    service.tenant_for = _own_machine
    await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r2")
    assert pool.calls[-1].tenant_id == "owner"


async def test_public_h3_takes_are_refused_without_the_operator_flag() -> None:
    service, pool, movie = await setup()
    service.account_policy = _policy("owner")
    service.tenant_for = _own_machine

    with pytest.raises(MovieError) as refused:
        await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r1")

    assert refused.value.code == MINIMAX_H3_UNAVAILABLE and "Higgsfield" in str(refused.value)
    assert pool.calls == []


async def test_public_assembly_and_portraits_need_their_own_machine() -> None:
    service, _pool, movie = await setup()
    service.account_policy = _policy("owner")
    service.tenant_for = _no_machine

    with pytest.raises(MovieError) as refused:
        await service.resolve_tenant(movie)
    assert refused.value.code == HOUSE_COMPUTE_UNAVAILABLE

    service.tenant_for = _own_machine
    # FLUX/CPU work is not H3: their own machine is enough.
    assert await service.resolve_tenant(movie) == "owner"
    # A shared HTTP provider is never theirs.
    with pytest.raises(MovieError):
        await service.resolve_tenant(movie, shared_only=True)


async def test_invited_movies_keep_house_compute() -> None:
    service, pool, movie = await setup()
    service.account_policy = _policy("someone-else")

    await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r1")

    assert pool.calls[-1].tenant_id is None


async def test_director_and_house_planning_are_refused_for_public_accounts() -> None:
    service, _pool, movie = await setup()

    class Director:
        async def plan(self, **_):  # pragma: no cover - must not be reached
            raise AssertionError("the house director must not run for a public account")

    service.director = Director()
    service.account_policy = _policy("owner")
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)

    response = client.post(
        f"/studio/movies/{movie.movie_id}/plan",
        json={"expected_revision": movie.revision, "brief": "A heist"},
    )
    capabilities = client.get("/studio/movies/capabilities").json()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == DIRECTOR_UNAVAILABLE
    assert capabilities["account_tier"] == "public"
    assert capabilities["director_available"] is False
    assert capabilities["planner_available"] is False
    assert capabilities["generation_available"] is False
    assert capabilities["dialogue_available"] is False

    generate = client.post(
        f"/studio/movies/{movie.movie_id}/generate",
        json={"expected_revision": movie.revision, "shot_ids": ["a"], "request_id": "r"},
    )
    assert generate.status_code == 403
    assert generate.json()["detail"]["code"] == MINIMAX_H3_UNAVAILABLE

    app.dependency_overrides[studio_actor_id] = lambda: "invited"
    invited = client.get("/studio/movies/capabilities").json()
    assert invited["account_tier"] == "invited" and invited["director_available"] is True


async def test_public_movie_processing_never_falls_back_to_the_service() -> None:
    from magic_video_editor.memory import MemoryAssetStore
    from magic_video_editor.movies.cpu_pool import PoolMovieMedia

    class NoWorkers:
        async def get(self, _job_id):
            return None

        async def worker_views(self):
            return []

    media = PoolMovieMedia(MemoryAssetStore(), NoWorkers())

    with pytest.raises(ValueError, match="own machine"):
        await media.scoped("owner", "take-1", local_fallback=False).final_frame("outputs/take.mp4")
    # Invited processing keeps the existing service fallback path.
    assert await media.scoped(None, "take-1")._process({"sources": []}, "outputs/x") is False


async def test_a_public_build_is_refused_up_front_with_its_code() -> None:
    service, pool, movie = await setup()
    service.account_policy = _policy("owner")
    service.tenant_for = _no_machine

    with pytest.raises(MovieError) as no_h3:
        await service.build_movie(movie.movie_id, "owner", movie.revision, "build-1")
    service.account_policy = _policy("owner", h3=True)
    with pytest.raises(MovieError) as no_machine:
        await service.build_movie(movie.movie_id, "owner", movie.revision, "build-2")

    # Missing takes need H3 first, then the owner's own machine.
    assert no_h3.value.status == 403 and no_h3.value.code == MINIMAX_H3_UNAVAILABLE
    assert no_machine.value.status == 403 and no_machine.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert (await service.get(movie.movie_id, "owner")).build is None
    assert pool.calls == []


async def test_capabilities_offer_public_accounts_only_what_they_can_start() -> None:
    service, _pool, _movie = await setup()
    service.account_policy = _policy("owner", h3=True)
    service.tenant_for = _no_machine
    app = FastAPI()
    app.include_router(create_movie_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    client = TestClient(app)

    without_machine = client.get("/studio/movies/capabilities").json()
    service.tenant_for = _own_machine
    with_machine = client.get("/studio/movies/capabilities").json()

    assert without_machine["generation_available"] is False
    assert without_machine["dialogue_available"] is False
    assert without_machine["composition_available"] is False
    assert with_machine["generation_available"] is True
    # Dialogue needs voices, which public accounts cannot add yet.
    assert with_machine["dialogue_available"] is False
    assert with_machine["composition_available"] is (service.composition is None)


async def test_public_clip_import_never_processes_on_the_service() -> None:
    service, _pool, movie = await setup()
    service.account_policy = _policy("owner")
    service.tenant_for = _own_machine
    grant = await service.upload(movie.movie_id, "owner", "video/mp4")

    with pytest.raises(MovieError) as refused:
        await service.import_clip(
            movie.movie_id, "owner", grant["revision"], "a", grant["asset_key"]
        )

    assert refused.value.status == 403 and refused.value.code == HOUSE_COMPUTE_UNAVAILABLE


async def test_public_voice_auditions_and_uploads_never_process_on_the_service() -> None:
    from types import SimpleNamespace

    from .test_audition import _wav
    from .test_audition import setup as audition_setup

    service, pool, movie = await audition_setup()
    service.account_policy = _policy("owner", h3=True)
    service.tenant_for = _own_machine

    with pytest.raises(MovieError) as audition:
        await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud1")
    assert audition.value.status == 403 and audition.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert pool.calls == []

    grant = await service.upload(movie.movie_id, "owner", "audio/wav")
    await service.assets.write_once(grant["asset_key"], _wav(5), "audio/wav")
    character = movie.characters[0].model_dump(exclude={"voice_seconds"})
    with pytest.raises(MovieError) as voice:
        await service.characters(
            movie.movie_id, "owner", grant["revision"],
            [{**character, "voice_asset_key": grant["asset_key"]}],
        )
    assert voice.value.code == HOUSE_COMPUTE_UNAVAILABLE

    # An audition started while invited fails rather than processing its audio here.
    service.account_policy = _policy()
    movie = await service.get(movie.movie_id, "owner")
    movie = await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud2")
    service.account_policy = _policy("owner", h3=True)
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.compositions["aud2"]["status"] == "failed"
    assert "public accounts" in movie.compositions["aud2"]["error"]

    class NoJoins:
        async def join_report(self, key, times):  # pragma: no cover - must not be reached
            raise AssertionError("public joins must not be measured on the service")

    service.media = NoJoins()
    public_movie = SimpleNamespace(owner_id="owner", assembly=SimpleNamespace())
    assert await service.measure_joins(public_movie) == []


async def test_no_fallback_media_refuses_every_service_only_step() -> None:
    from magic_video_editor.memory import MemoryAssetStore
    from magic_video_editor.movies.cpu_pool import PoolMovieMedia

    media = PoolMovieMedia(MemoryAssetStore(), pool=None).scoped("owner", "s", local_fallback=False)
    for step in (
        media.duration(b""), media.audio_duration(b""), media.extract_audio(b""),
        media.join_report("k", [1.0]),
    ):
        with pytest.raises(ValueError, match="public accounts"):
            await step


async def test_public_movie_upload_grants_carry_a_ceiling_per_kind() -> None:
    service, _pool, movie = await setup()
    service.account_policy = _policy("owner")

    video = await service.upload(movie.movie_id, "owner", "video/mp4")
    audio = await service.upload(movie.movie_id, "owner", "audio/wav")

    assert video["upload_headers"] == {"x-goog-content-length-range": f"0,{256 * 1024 * 1024}"}
    assert audio["upload_headers"] == {"x-goog-content-length-range": f"0,{50 * 1024 * 1024}"}

    service.account_policy = _policy()
    assert "upload_headers" not in await service.upload(movie.movie_id, "owner", "image/png")
