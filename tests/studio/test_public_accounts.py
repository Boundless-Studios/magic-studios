"""Public self-registration: admission, classification, and what public accounts
may not use (house compute, MiniMax H3 without the operator flag, the larger
storage cap). Invited accounts must behave exactly as before."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from magic_video_editor.app import ServiceRole, create_app, runtime_from_env
from magic_video_editor.contracts import JobStatus
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.accounts import (
    HOUSE_COMPUTE_UNAVAILABLE,
    MINIMAX_H3_UNAVAILABLE,
    AccountPolicy,
    AccountPolicyRefused,
    AccountTier,
    AllowlistAccounts,
    account_policy_from_env,
    own_compute_required,
)
from magic_video_editor.studio.auth import (
    Auth0Identity,
    EmailAllowlist,
    MemoryStudioAuthStore,
)
from magic_video_editor.studio.auth_service import (
    EmailNotAllowed,
    SignInRejected,
    StudioAuthService,
)
from magic_video_editor.studio.enrolment import (
    MemoryStudioEnrolmentStore,
    generate_pairing_token,
    hash_token,
    pairing_token_expiry,
)
from magic_video_editor.studio.limits import (
    DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
    DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES,
    MemoryStudioOwnerUsageStore,
    OwnerLimits,
    OwnerStorageLimitExceeded,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    CompositionModel,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionSpecification,
)
from magic_video_editor.studio.provider_accounts import HIGGSFIELD
from magic_video_editor.studio.routes import _owner_limit_exception
from magic_video_editor.studio.service import (
    MotionGenerationUnavailable,
    StudioService,
    SubjectUploadError,
)

from .test_higgsfield_provider import KEY, _accounts, _hosted_settings
from .test_higgsfield_provider import _Pool as _NeverPool
from .test_higgsfield_provider import _service as _higgsfield_service
from .test_motion_pool import PROJECT, _Pool, _service_with_motion

OWNER = "user-1"


def _policy(*public: str, h3: bool = False) -> AccountPolicy:
    async def tier(owner_id: str) -> AccountTier:
        return AccountTier.PUBLIC if owner_id in public else AccountTier.INVITED

    return AccountPolicy(tier_for=tier, public_h3_enabled=h3)


async def _enrolled(owner_id: str = OWNER) -> MemoryStudioEnrolmentStore:
    enrolment = MemoryStudioEnrolmentStore()
    token = generate_pairing_token()
    await enrolment.mint_pairing_token(
        owner_id=owner_id, token_hash=hash_token(token), expires_at=pairing_token_expiry()
    )
    await enrolment.redeem_pairing_token(
        token_hash=hash_token(token),
        worker_id="their-box",
        label="Their box",
        credential_hash=hash_token("mvsw_" + "z" * 32),
        now=datetime.now(UTC),
    )
    return enrolment


async def _motion_service(*, pool=None, enrolment=None, policy=None):
    service, jobs, dispatcher, project = await _service_with_motion(
        pool if pool is not None else _Pool(), enrolment
    )
    if policy is not None:
        service.account_policy = policy
    project = await service.add_motion(
        PROJECT,
        project.revision,
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.PRESET,
            preset=MotionPreset.IDLE, loop_policy="loop", required=True,
        ),
        OWNER,
    )
    return service, jobs, dispatcher, project


_GPU_SETTINGS = MotionGenerationSettings(
    preset="high", steps=28, seed=123, width=768, height=768, duration_seconds=6
)


# --- admission ----------------------------------------------------------------


def test_public_signup_requires_an_explicit_switch_and_a_plain_address() -> None:
    closed = EmailAllowlist.from_env({})
    open_ = EmailAllowlist.from_env({"MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED": "true"})

    assert closed.public_signup is False
    assert not closed.permits("verified@example.com")
    assert open_.permits("verified@example.com")
    for bad in ("", "invalid", "a@b@c", "@example.com", "someone@", "a b@example.com"):
        assert not open_.permits(bad)
    # The invitation lists are unchanged by the switch.
    invited = EmailAllowlist.from_env({
        "MAGIC_VIDEO_EDITOR_ALLOWED_EMAILS": "invited@example.com",
        "MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED": "true",
    })
    assert invited.invites("invited@example.com")
    assert not invited.invites("stranger@example.com")
    assert invited.permits("stranger@example.com")


async def test_public_signup_still_requires_a_verified_email() -> None:
    allowlist = EmailAllowlist(frozenset(), frozenset(), public_signup=True)
    service = StudioAuthService(MemoryStudioAuthStore(), allowlist=allowlist)

    user, _ = await service.sign_in_with_auth0(
        Auth0Identity(subject="auth0|1", email="new@example.com", email_verified=True)
    )
    assert user.email == "new@example.com"
    with pytest.raises(SignInRejected):
        await service.sign_in_with_auth0(
            Auth0Identity(subject="auth0|2", email="other@example.com", email_verified=False)
        )
    closed = StudioAuthService(
        MemoryStudioAuthStore(), allowlist=EmailAllowlist(frozenset(), frozenset())
    )
    with pytest.raises(EmailNotAllowed):
        await closed.sign_in_with_auth0(
            Auth0Identity(subject="auth0|1", email="new@example.com", email_verified=True)
        )


# --- classification -------------------------------------------------------------


async def test_accounts_are_classified_from_the_allowlist_at_request_time() -> None:
    users = MemoryStudioAuthStore()
    invited = await users.upsert_user(email="invited@example.com")
    staff = await users.upsert_user(email="someone@studio.example", google_sub="g-1")
    stranger = await users.upsert_user(email="stranger@example.com")
    admin = await users.upsert_user(email="admin@example.com")

    async def is_admin(actor_id: str) -> bool:
        return actor_id == admin.actor_id

    open_list = EmailAllowlist(
        frozenset({"invited@example.com"}), frozenset({"studio.example"}), public_signup=True
    )
    classify = AllowlistAccounts(users, open_list, is_admin)

    assert await classify(invited.actor_id) is AccountTier.INVITED
    assert await classify(staff.actor_id) is AccountTier.INVITED
    assert await classify(admin.actor_id) is AccountTier.INVITED
    assert await classify(stranger.actor_id) is AccountTier.PUBLIC
    # Unknown owners are not invited.
    assert await classify("email:ghost@example.com") is AccountTier.PUBLIC
    # Taking someone off the list makes them public on the next request.
    removed = AllowlistAccounts(
        users, EmailAllowlist(frozenset(), frozenset({"studio.example"}), public_signup=True)
    )
    assert await removed(invited.actor_id) is AccountTier.PUBLIC
    # Closing signup again never upgrades accounts that joined while it was
    # open; only owners with no user row (gateway, pre-switch actors) are
    # treated as invited while it is closed.
    closed = AllowlistAccounts(users, EmailAllowlist(frozenset({"invited@example.com"}), frozenset()))
    assert await closed(stranger.actor_id) is AccountTier.PUBLIC
    assert await closed(invited.actor_id) is AccountTier.INVITED
    assert await closed("email:ghost@example.com") is AccountTier.INVITED


async def test_an_unconfigured_policy_treats_everyone_as_invited() -> None:
    policy = AccountPolicy()
    assert await policy.tier("anyone") is AccountTier.INVITED
    assert await policy.h3_allowed("anyone") is True

    users = MemoryStudioAuthStore()
    stranger = await users.upsert_user(email="stranger@example.com")
    configured = account_policy_from_env(
        {"MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED": "true"}, users
    )
    assert configured.public_h3_enabled is False
    assert await configured.is_public(stranger.actor_id)
    assert await configured.h3_allowed(stranger.actor_id) is False
    assert account_policy_from_env(
        {"MAGIC_VIDEO_EDITOR_PUBLIC_H3_ENABLED": "true"}, users
    ).public_h3_enabled is True


def test_production_runtime_shares_one_policy_between_studio_and_movies() -> None:
    runtime = runtime_from_env({
        "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
        "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
        "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
        "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
        "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
        "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
        "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
        "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
    })

    policy = runtime.studio_service.account_policy
    assert runtime.movie_service.account_policy is policy
    assert isinstance(policy.tier_for, AllowlistAccounts)
    assert policy.public_h3_enabled is False


# --- motions --------------------------------------------------------------------


async def test_invited_accounts_still_use_house_machines() -> None:
    pool = _Pool()
    service, _jobs, dispatcher, project = await _motion_service(
        pool=pool, policy=_policy("someone-else")
    )

    await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert pool.submissions[0].tenant_id is None
    assert dispatcher.job_ids == []


async def test_public_account_without_a_machine_is_refused_before_any_job() -> None:
    pool = _Pool()
    service, jobs, dispatcher, project = await _motion_service(
        pool=pool, policy=_policy(OWNER, h3=True)
    )

    with pytest.raises(AccountPolicyRefused) as refused:
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)
    assert refused.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert "Connect your own GPU on the Machines page or add a Higgsfield key" in refused.value.message
    with pytest.raises(AccountPolicyRefused):
        await service.generate_motions(
            PROJECT, project.revision, ("idle",), OWNER, "batch-1", settings=_GPU_SETTINGS
        )

    assert pool.submissions == [] and dispatcher.job_ids == []
    assert not await jobs.list_for_project(PROJECT)
    # The refusal touched nothing: no motion action was compiled into a new revision.
    assert (await service.get_project(PROJECT, OWNER)).revision == project.revision
    # Backstop: a public account is never untenanted.
    assert await service._tenant_for(OWNER) == OWNER


async def test_public_h3_is_refused_by_default_even_on_their_own_machine() -> None:
    pool = _Pool()
    service, jobs, _dispatcher, project = await _motion_service(
        pool=pool, enrolment=await _enrolled(), policy=_policy(OWNER)
    )

    with pytest.raises(AccountPolicyRefused) as refused:
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert refused.value.code == MINIMAX_H3_UNAVAILABLE
    assert "Higgsfield" in refused.value.message
    assert pool.submissions == [] and not await jobs.list_for_project(PROJECT)


async def test_public_h3_flag_allows_their_own_machine_only() -> None:
    pool = _Pool()
    service, _jobs, dispatcher, project = await _motion_service(
        pool=pool, enrolment=await _enrolled(), policy=_policy(OWNER, h3=True)
    )

    await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert pool.submissions[0].tenant_id == OWNER
    assert dispatcher.job_ids == []


async def test_public_pool_failure_never_falls_back_to_a_cloud_task() -> None:
    pool = _Pool()
    pool.fail_submit = True
    service, jobs, dispatcher, project = await _motion_service(
        pool=pool, enrolment=await _enrolled(), policy=_policy(OWNER, h3=True)
    )

    with pytest.raises(MotionGenerationUnavailable):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert dispatcher.job_ids == []
    (job,) = await jobs.list_for_project(PROJECT)
    assert job.status is JobStatus.FAILED


async def test_invited_pool_failure_keeps_the_cloud_task_fallback() -> None:
    pool = _Pool()
    pool.fail_submit = True
    service, _jobs, dispatcher, project = await _motion_service(pool=pool, policy=_policy())

    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert dispatcher.job_ids == [job.job_id]


async def test_without_a_pool_public_motions_are_refused_even_with_a_machine() -> None:
    service, _jobs, dispatcher, project = await _motion_service(enrolment=await _enrolled())
    service._motion_pool = None  # the Cloud Task path runs on the shared GPU endpoint
    service.account_policy = _policy(OWNER, h3=True)

    with pytest.raises(AccountPolicyRefused) as refused:
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_GPU_SETTINGS)

    assert refused.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert dispatcher.job_ids == []


async def test_public_higgsfield_motion_is_allowed_without_a_machine() -> None:
    pool = _NeverPool()
    service, jobs, dispatcher, fake, project = await _higgsfield_service(pool=pool)
    service.account_policy = _policy(OWNER)

    job = await service.generate_motion(
        PROJECT, project.revision, "idle", OWNER, request_id="request-1",
        settings=_hosted_settings(),
    )

    assert pool.submissions == [] and dispatcher.job_ids == []
    assert len(fake.submissions) == 1
    assert (await jobs.get(job.job_id)).payload["resolved_recipe"]["provider"] == HIGGSFIELD


# --- subjects and compositions ----------------------------------------------------


def _subject_service(*, on_pool: bool, enrolment=None, policy=None) -> StudioService:
    return StudioService(
        store=MemoryStudioStore(), assets=MemoryAssetStore(), jobs=MemoryJobStore(),
        dispatcher=_Recorder(), subject_generation_available=True,
        subject_generation_on_pool=on_pool, enrolment=enrolment, account_policy=policy,
    )


class _Recorder:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


async def _project(service: StudioService) -> tuple[str, int]:
    created = await service.create_project("Ember", OWNER)
    return created.manifest.project_id, created.revision


async def test_public_subject_generation_needs_their_own_pool_machine() -> None:
    refused_service = _subject_service(on_pool=True, policy=_policy(OWNER))
    project_id, revision = await _project(refused_service)
    with pytest.raises(AccountPolicyRefused) as refused:
        await refused_service.generate_subject(project_id, revision, ("x",), None, "r", OWNER)
    assert refused.value.code == HOUSE_COMPUTE_UNAVAILABLE

    # A shared HTTP subject endpoint is house compute even with a machine attached.
    http_service = _subject_service(on_pool=False, enrolment=await _enrolled(), policy=_policy(OWNER))
    project_id, revision = await _project(http_service)
    with pytest.raises(AccountPolicyRefused):
        await http_service.generate_subject(project_id, revision, ("x",), None, "r", OWNER)

    # FLUX.2 Klein on their own machine passes the account gate (and then
    # validates its references as usual); its tenant is the owner.
    own = _subject_service(on_pool=True, enrolment=await _enrolled(), policy=_policy(OWNER))
    project_id, revision = await _project(own)
    with pytest.raises(SubjectUploadError):
        await own.generate_subject(project_id, revision, ("x",), None, "r", OWNER)
    assert await own._tenant_for(OWNER) == OWNER


async def test_invited_subject_generation_is_unchanged() -> None:
    service = _subject_service(on_pool=False, policy=_policy("someone-else"))
    project_id, revision = await _project(service)
    with pytest.raises(SubjectUploadError):
        await service.generate_subject(project_id, revision, ("x",), None, "r", OWNER)
    assert await service._tenant_for(OWNER) is None


async def test_public_composition_is_refused_as_shared_compute() -> None:
    service, _jobs, dispatcher, project = await _motion_service(
        enrolment=await _enrolled(), policy=_policy(OWNER, h3=True)
    )
    with pytest.raises(AccountPolicyRefused) as refused:
        await service.compose_opening_frame(
            PROJECT, project.revision, "pose", next(iter(CompositionModel)), OWNER, "r"
        )
    assert refused.value.code == HOUSE_COMPUTE_UNAVAILABLE
    assert dispatcher.job_ids == []


def test_policy_refusals_map_to_403_with_a_machine_readable_code() -> None:
    response = _owner_limit_exception(own_compute_required())
    assert response.status_code == 403
    assert response.detail["code"] == HOUSE_COMPUTE_UNAVAILABLE


# --- storage ------------------------------------------------------------------------


def test_public_storage_cap_has_its_own_lower_default() -> None:
    assert DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES == 5 * 1024**3
    assert OwnerLimits().public_storage_limit_bytes == DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES
    assert OwnerLimits.from_env(
        {"MAGIC_VIDEO_EDITOR_PUBLIC_STORAGE_LIMIT_BYTES": "1024"}
    ).public_storage_limit_bytes == 1024


async def test_public_accounts_get_the_public_storage_cap() -> None:
    usage = MemoryStudioOwnerUsageStore()
    await usage.add_bytes(OWNER, 6 * 1024**3)
    await usage.add_bytes("invited", 6 * 1024**3)
    service = StudioService(
        store=MemoryStudioStore(), assets=MemoryAssetStore(), owner_usage=usage,
        account_policy=_policy(OWNER),
    )

    public = await service.owner_usage(OWNER)
    invited = await service.owner_usage("invited")

    assert public.storage_limit_bytes == DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES
    assert public.account_tier == "public"
    assert invited.storage_limit_bytes == DEFAULT_OWNER_STORAGE_LIMIT_BYTES
    assert invited.account_tier == "invited"
    with pytest.raises(OwnerStorageLimitExceeded):
        await service._require_storage_headroom(OWNER)
    await service._require_storage_headroom("invited")


# --- discovery ----------------------------------------------------------------------


def test_generation_config_and_session_report_the_account_policy() -> None:
    service = StudioService(
        store=MemoryStudioStore(), assets=MemoryAssetStore(), jobs=MemoryJobStore(),
        provider_accounts=_accounts(), account_policy=_policy(OWNER),
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    app = create_app(runtime, service_role=ServiceRole.API)
    public = TestClient(app, headers={"X-Studio-Actor-Id": OWNER})
    invited = TestClient(app, headers={"X-Studio-Actor-Id": "invited"})

    public.put("/studio/providers/higgsfield", json={"api_key": KEY})
    config = public.get("/studio/generation-config").json()

    assert config["account"] == {
        "tier": "public", "house_compute": False, "h3": False, "own_machine": False
    }
    assert config["selected"] is None and config["settings"] is None
    assert config["providers"]["higgsfield"]["configured"] is True
    assert invited.get("/studio/generation-config").json()["account"] == {
        "tier": "invited", "house_compute": True, "h3": True, "own_machine": False
    }
    session = public.get("/studio/auth/session").json()
    assert session["usage"]["account_tier"] == "public"
    assert session["usage"]["storage_limit_bytes"] == DEFAULT_PUBLIC_STORAGE_LIMIT_BYTES


async def test_subject_jobs_carry_their_tenant_to_the_worker_role() -> None:
    from magic_video_editor.app import _execute
    from magic_video_editor.local_studio import create_local_studio_runtime

    from .test_subject_generation import _jpeg

    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = StudioService(
        store=MemoryStudioStore(), assets=runtime.assets, jobs=runtime.jobs,
        dispatcher=_Recorder(), subject_generation_available=True,
        subject_generation_on_pool=True, enrolment=await _enrolled(), account_policy=_policy(OWNER),
    )
    project = await service.create_project("Ember", OWNER)
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()

    job = await service.generate_subject(
        project.project_id, project.revision, (target.key,), None, "r", OWNER
    )
    assert job.payload["tenant_id"] == OWNER

    # The worker role has no Studio service to resolve the owner itself.
    seen: list[str | None] = []
    deterministic = runtime.subject_generator

    class Capture:
        async def generate_subject(self, request):
            seen.append(request.owner_id)
            return await deterministic.generate_subject(request)

    runtime.subject_generator = Capture()
    runtime.studio_service = None
    result = await _execute(runtime, job.job_id)

    assert result.status.value == "completed"
    assert seen == [OWNER]

    # Untenanted work keeps its previous payload shape.
    invited = StudioService(
        store=MemoryStudioStore(), assets=runtime.assets, jobs=runtime.jobs,
        dispatcher=_Recorder(), subject_generation_available=True,
        subject_generation_on_pool=True, account_policy=_policy("someone-else"),
    )
    project = await invited.create_project("Ember", OWNER)
    target = await invited.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()
    job = await invited.generate_subject(
        project.project_id, project.revision, (target.key,), None, "r", OWNER
    )
    assert "tenant_id" not in job.payload


async def test_subject_retries_queued_before_tenants_were_recorded_stay_idempotent() -> None:
    from magic_video_editor.local_studio import create_local_studio_runtime

    from .test_subject_generation import _jpeg

    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = StudioService(
        store=MemoryStudioStore(), assets=runtime.assets, jobs=runtime.jobs,
        dispatcher=_Recorder(), subject_generation_available=True,
        subject_generation_on_pool=True, account_policy=_policy("someone-else"),
    )
    project = await service.create_project("Ember", OWNER)
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()
    # Queued before the owner had a machine, which is the shape of every
    # request queued before tenants were recorded.
    original = await service.generate_subject(
        project.project_id, project.revision, (target.key,), None, "r", OWNER
    )
    assert "tenant_id" not in original.payload

    service._enrolment = await _enrolled()
    replay = await service.generate_subject(
        project.project_id, project.revision, (target.key,), None, "r", OWNER
    )

    assert replay.job_id == original.job_id


async def test_public_upload_grants_carry_a_signed_size_ceiling() -> None:
    assets = MemoryAssetStore()
    public = StudioService(store=MemoryStudioStore(), assets=assets, account_policy=_policy(OWNER))
    project = await public.create_project("Ember", OWNER)

    target = await public.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )

    assert target.upload_headers == {"x-goog-content-length-range": "0,20000000"}
    assert assets.upload_limits[target.key] == 20_000_000

    # Invited grants keep their previous shape so existing clients keep working.
    invited = StudioService(
        store=MemoryStudioStore(), assets=assets, account_policy=_policy("someone-else")
    )
    project = await invited.create_project("Ember", OWNER)
    target = await invited.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    assert target.upload_headers == {}
    assert assets.upload_limits[target.key] is None


async def test_a_subject_no_attached_machine_can_run_is_refused_before_it_queues() -> None:
    from magic_video_editor.local_studio import create_local_studio_runtime
    from magic_video_editor.studio.accounts import MACHINE_CANNOT_RUN

    from .test_machine_features import CPU, _gpu, _Pool, _View
    from .test_subject_generation import _jpeg

    runtime = create_local_studio_runtime(inline_dispatch=False)
    pool = _Pool([_View("their-box", (CPU,), gpus=(_gpu(8192),))])
    dispatcher = _Recorder()
    service = StudioService(
        store=MemoryStudioStore(), assets=runtime.assets, jobs=runtime.jobs,
        dispatcher=dispatcher, subject_generation_available=True,
        subject_generation_on_pool=True, enrolment=await _enrolled(),
        account_policy=_policy(OWNER), motion_pool=pool,
    )
    project = await service.create_project("Ember", OWNER)
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()

    with pytest.raises(AccountPolicyRefused) as refused:
        await service.generate_subject(project.project_id, project.revision, (target.key,), None, "r", OWNER)
    assert refused.value.code == MACHINE_CANNOT_RUN
    assert refused.value.message == (
        "None of your machines can make subject images. "
        "Their box: subject images need a 12 GB card and this one has 8 GB."
    )
    assert _owner_limit_exception(refused.value).status_code == 403
    assert dispatcher.job_ids == []

    # Attached with the 12GB subject configuration, the same request queues.
    pool.views = [_View("their-box", (CPU, "image.flux2_klein.subject.12gb.v1"), vram_mb=12282)]
    job = await service.generate_subject(project.project_id, project.revision, (target.key,), None, "r", OWNER)
    assert dispatcher.job_ids == [job.job_id]


async def test_a_subject_whose_machine_went_away_fails_with_the_machine_reason() -> None:
    from magic_video_editor.app import _execute
    from magic_video_editor.local_studio import create_local_studio_runtime
    from magic_video_editor.plugins.subject_pool import SubjectMachineUnavailable

    from .test_subject_generation import _jpeg

    runtime = create_local_studio_runtime(inline_dispatch=False)
    service = StudioService(
        store=MemoryStudioStore(), assets=runtime.assets, jobs=runtime.jobs,
        dispatcher=_Recorder(), subject_generation_available=True,
        subject_generation_on_pool=True, account_policy=_policy("someone-else"),
    )
    project = await service.create_project("Ember", OWNER)
    target = await service.create_subject_reference_upload(
        project.project_id, "image/jpeg", OWNER, project.revision
    )
    runtime.assets.assets[target.key] = _jpeg()
    job = await service.generate_subject(project.project_id, project.revision, (target.key,), None, "r", OWNER)
    reason = "None of your machines can make subject images. Desk: it is shutting down."

    class Refusing:
        async def generate_subject(self, request):
            raise SubjectMachineUnavailable(reason)

    runtime.subject_generator = Refusing()
    result = await _execute(runtime, job.job_id)

    assert result.status.value == "failed"
    failed = await runtime.jobs.get(job.job_id)
    assert failed.failure_code.value == "provider_unavailable"
    assert failed.failure_message == reason
