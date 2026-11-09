"""3D engine stages (mesh, clean) on a fake pool: availability, pinning, journal,
reconciliation into candidates, idempotency, tiers and refusals.

The pinned pool package ships the engine profile qualified (engine-v0.2.0). Tests
that run stages inject a qualified copy with test pins, so a later engine release
does not change them; the unqualified refusal is proven against a test-only
unqualified copy."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from outbound_gpu_worker_pool import machine_profiles
from outbound_gpu_worker_pool.contracts import (
    JobRecord,
    JobStatus,
    PoolWorkerStatus,
    PoolWorkerView,
    ProfileNotQualified,
)
from outbound_gpu_worker_pool.engine_recipes import (
    MESH3D,
    MESH3D_CLEAN_CAPABILITY,
    MESH3D_MESH_CAPABILITY,
    MESH3D_RECIPE,
)
from outbound_gpu_worker_pool.machine_profiles import CONFIG_KEY, RECIPE_KEY, STANDARD, describe_profile

from magic_video_editor.assets3d.routes import create_assets3d_router
from magic_video_editor.assets3d.service import Assets3DService
from magic_video_editor.assets3d.stages import (
    DISABLED,
    NO_WORKER,
    PUBLIC_NEEDS_OWN_MACHINE,
    UNQUALIFIED,
    StageEngine,
)
from magic_video_editor.assets3d.store import MemoryAssets3DStore
from magic_video_editor.machine_defaults import HostedMachineDefaults
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.accounts import AccountPolicy, AccountTier
from magic_video_editor.studio.routes import studio_actor_id

sys.path.insert(0, str(Path(__file__).parent))
from test_assets3d_media import glb, triangle  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"front picture"


@pytest.fixture
def qualified(monkeypatch):
    """A qualified copy of the engine profile, with pins set: tests only."""
    recipe = replace(MESH3D_RECIPE, engine_tag="engine-v0.0.0-test", engine_commit="c" * 40,
                     sources_lock_sha256="s" * 64, weights_lock_sha256="w" * 64)
    spec = replace(machine_profiles.PROFILES[MESH3D], engine=recipe, qualified=True)
    monkeypatch.setitem(machine_profiles.PROFILES, MESH3D, spec)
    return describe_profile(MESH3D)["recipe_version"]


@pytest.fixture
def unqualified(monkeypatch):
    """An unqualified copy of the engine profile, pins unset: tests only."""
    recipe = replace(MESH3D_RECIPE, engine_tag=None, engine_commit=None,
                     sources_lock_sha256=None, weights_lock_sha256=None)
    spec = replace(machine_profiles.PROFILES[MESH3D], engine=recipe, qualified=False)
    monkeypatch.setitem(machine_profiles.PROFILES, MESH3D, spec)


def engine_worker(worker_id="house-4090", tenant=None, *, namespaced=False, **overrides):
    recipe = describe_profile(MESH3D)
    if namespaced:
        # A runtime whose first profile is the H3 standard one: the engine's keys
        # are namespaced.
        versions = {RECIPE_KEY: describe_profile(STANDARD)["recipe_version"], CONFIG_KEY: "h3-config",
                    f"{RECIPE_KEY}@{MESH3D}": recipe["recipe_version"], f"{CONFIG_KEY}@{MESH3D}": "engine-config"}
    else:
        versions = {RECIPE_KEY: recipe["recipe_version"], CONFIG_KEY: "engine-config"}
    row = PoolWorkerView(worker_id, PoolWorkerStatus.ONLINE, datetime.now(UTC),
                         (MESH3D_MESH_CAPABILITY, MESH3D_CLEAN_CAPABILITY), (), None, False, tenant,
                         vram_mb=24_564, runtime_versions=versions)
    return replace(row, **overrides)


class FakePool:
    """The pool's host surface: views, idempotent submit, get, cancel. Refuses an
    unqualified profile's capability the way the real coordinator does."""

    def __init__(self, assets, *rows):
        self.assets = assets
        self.rows = list(rows)
        self.submissions = []
        self.records: dict[str, JobRecord] = {}
        self.cancelled: list[str] = []
        self.fail_submit = 0

    async def worker_views(self, tenant_id=None):
        return list(self.rows)

    async def submit(self, submission):
        if machine_profiles.unusable_reason(MESH3D) is not None:
            raise ProfileNotQualified("mesh3d-24gb-standard is not qualified yet")
        if self.fail_submit:
            self.fail_submit -= 1
            raise ConnectionError("pool unreachable")
        self.submissions.append(submission)
        if submission.job_id not in self.records:
            now = datetime.now(UTC)
            self.records[submission.job_id] = JobRecord(
                job_id=submission.job_id, idempotency_key=submission.idempotency_key,
                capability_id=submission.capability_id, status=JobStatus.QUEUED,
                input_keys=submission.input_keys, output_key=submission.output_key, attempts=0,
                created_at=now, updated_at=now, payload=submission.payload,
                tenant_id=submission.tenant_id, requirements=submission.requirements,
                execution_deadline_seconds=submission.execution_deadline_seconds)
        return self.records[submission.job_id]

    async def get(self, job_id):
        return self.records.get(job_id)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        return True

    def complete(self, job_id, content, diagnostics, worker="house-4090"):
        record = self.records[job_id]
        asyncio.run(self.assets.write_once(record.output_key, content, "application/octet-stream"))
        self.records[job_id] = replace(
            record, status=JobStatus.COMPLETED, executed_by=worker,
            output_sha256=sha256(content).hexdigest(), output_byte_length=len(content),
            execution_metadata={"plugin_id": "aismith-engine", "plugin_version": "1",
                                "model_id": "aismith-engine/mesh", "model_version": "c" * 40,
                                "diagnostics": diagnostics})

    def fail(self, job_id, message):
        self.records[job_id] = replace(self.records[job_id], status=JobStatus.FAILED,
                                       failure_message=message)


def build(*rows, public=False, enabled=True, own_machine=False, tenant="house"):
    assets = MemoryAssetStore()
    pool = FakePool(assets, *rows)

    async def tier(owner):
        return AccountTier.PUBLIC if public else AccountTier.INVITED

    async def tenant_for(owner):
        # As `_tenant_for`: a public account is always its own tenant.
        return owner if (public or own_machine) else None

    async def own_tenant_for(owner):
        return owner if own_machine else None

    stages = StageEngine(enabled=enabled, pool=pool, machines=HostedMachineDefaults(pool, tenant_for),
                         own_tenant_for=own_tenant_for)
    service = Assets3DService(MemoryAssets3DStore(), assets, account_policy=AccountPolicy(tier_for=tier),
                              stages=stages)
    app = FastAPI()
    app.include_router(create_assets3d_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    return TestClient(app), service, assets, pool


def with_front(client, assets) -> str:
    id = client.post("/studio/3d", json={"name": "Orc", "kind": "character"}).json()["asset_id"]
    grant = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).json()
    asyncio.run(assets.write_once(grant["asset_key"], PNG, "image/png"))
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": grant["revision"], "role": "front", "asset_key": grant["asset_key"],
    }).status_code == 200
    return id


def run(client, id, stage="mesh", request_id="r1", **body):
    return client.post(f"/studio/3d/{id}/stages/{stage}", json={"request_id": request_id, **body})


def diagnostics(stage="mesh", **overrides):
    return {
        "runtime_versions": {RECIPE_KEY: describe_profile(MESH3D)["recipe_version"]},
        "effective_inputs": {"resolution": 1024, "seed": 7} if stage == "mesh" else {"quality_tier": "standard"},
        "engine_effective_inputs": {"resolution": 1024, "seed": 7, "texture_size": 2048},
        "stage": stage,
        "engine": {"version": "0.0.0", "commit": "c" * 40},
        "gate": {"name": stage, "passed": True, "checks": [{"id": "watertight", "passed": True}]},
        "measured": {"triangles": 30_000},
        "timings_s": {"total": 41.5},
        **overrides,
    }


# -- off, unqualified or without a machine: refused everywhere, nothing sent ---


@pytest.mark.parametrize(("enabled", "reason"), [(False, DISABLED), (True, UNQUALIFIED)])
def test_disabled_or_unqualified_stages_are_refused_and_never_reach_the_pool(enabled, reason, request):
    if enabled:
        request.getfixturevalue("unqualified")
    client, service, assets, pool = build(engine_worker(), enabled=enabled)
    caps = client.get("/studio/3d/capabilities").json()
    assert caps["stages"] == {"mesh": {"available": False, "reason": reason},
                              "clean": {"available": False, "reason": reason}}
    assert reason.startswith("not available yet")
    id = with_front(client, assets)
    refused = run(client, id, settings={"resolution": 1024, "seed": 1})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "stage_unavailable", "message": reason}
    assert run(client, id, "clean", "r2", parent_id="x").json()["detail"]["message"] == reason
    assert pool.submissions == [] and client.get(f"/studio/3d/{id}").json()["operations"] == []


def test_the_default_engine_is_off():
    service = Assets3DService(MemoryAssets3DStore(), MemoryAssetStore())
    caps = asyncio.run(service.capabilities("owner"))
    assert caps["stages"]["mesh"] == {"available": False, "reason": DISABLED}


def test_the_pinned_pool_ships_the_profile_qualified_and_generation_stays_off():
    """The pool pin qualifies the profile; only the setting keeps generation off."""
    shipped = machine_profiles.PROFILES[MESH3D]
    assert shipped.qualified and shipped.engine.engine_tag == "engine-v0.2.0"
    client, service, assets, pool = build(engine_worker(), enabled=False)
    caps = client.get("/studio/3d/capabilities").json()
    assert caps["stages"] == {"mesh": {"available": False, "reason": DISABLED},
                              "clean": {"available": False, "reason": DISABLED}}
    assert DISABLED.startswith("not available yet")
    assert pool.submissions == []


def test_no_eligible_worker_is_refused_with_its_reason(qualified):
    stale = engine_worker(runtime_versions={RECIPE_KEY: "an-older-recipe", CONFIG_KEY: "x"})
    client, service, assets, pool = build(stale)
    assert client.get("/studio/3d/capabilities").json()["stages"]["mesh"] == {
        "available": False, "reason": NO_WORKER}
    id = with_front(client, assets)
    refused = run(client, id)
    assert refused.status_code == 409 and "does not serve the mesh3d-24gb-standard recipe" in refused.json()["detail"]["message"]
    assert pool.submissions == []


def test_another_profile_is_never_a_fallback(qualified):
    h3 = engine_worker(runtime_versions={RECIPE_KEY: describe_profile(STANDARD)["recipe_version"],
                                         CONFIG_KEY: "h3"})
    client, service, assets, pool = build(h3)
    id = with_front(client, assets)
    assert run(client, id).status_code == 409 and pool.submissions == []


# -- running stages -----------------------------------------------------------


def test_mesh_is_pinned_journalled_submitted_and_idempotent(qualified):
    client, service, assets, pool = build(engine_worker())
    assert client.get("/studio/3d/capabilities").json()["stages"]["mesh"] == {
        "available": True, "reason": None, "worker_id": "house-4090"}
    id = client.post("/studio/3d", json={"name": "Orc"}).json()["asset_id"]
    assert run(client, id).status_code == 409  # no front picture yet
    id = with_front(client, assets)
    sent = run(client, id, settings={"seed": 7})
    assert sent.status_code == 200, sent.text
    [operation] = sent.json()["operations"]
    assert operation["status"] == "running" and operation["worker_id"] == "house-4090"
    # Defaults are resolved and recorded before submission.
    assert operation["settings"] == {"resolution": 1024, "seed": 7}
    [job] = pool.submissions
    front = sent.json()["sources"][0]["asset_key"]
    assert job.capability_id == MESH3D_MESH_CAPABILITY
    assert job.payload == {"inputs": {"front": front}, "resolution": 1024, "seed": 7}
    assert job.input_keys == (front,)
    assert job.output_key == f"outputs/assets3d/{id}/candidates/{operation['operation_id']}.glb"
    assert job.requirements.worker_ids == ("house-4090",)
    assert job.requirements.min_vram_mb == 24_000
    assert job.requirements.runtime_versions == {RECIPE_KEY: qualified, CONFIG_KEY: "engine-config"}
    assert job.execution_deadline_seconds == MESH3D_RECIPE.stage_for(MESH3D_MESH_CAPABILITY).deadline_seconds
    assert job.execution_deadline_seconds == 1_800 and job.tenant_id is None
    assert job.idempotency_key == f"asset3d-stage:{id}:r1"
    # The same request_id is the same run; a different request under it is refused.
    again = run(client, id, settings={"seed": 7})
    assert again.status_code == 200 and len(again.json()["operations"]) == 1 and len(pool.submissions) == 1
    reused = run(client, id, settings={"seed": 8})
    assert reused.status_code == 409 and reused.json()["detail"]["code"] == "request_id_reused"
    assert asyncio.run(service.running_jobs("owner")) == 1


def test_namespaced_recipe_keys_are_pinned_as_the_worker_registered_them(qualified):
    client, service, assets, pool = build(engine_worker(namespaced=True))
    id = with_front(client, assets)
    assert run(client, id).status_code == 200
    assert pool.submissions[0].requirements.runtime_versions == {
        f"{RECIPE_KEY}@{MESH3D}": qualified, f"{CONFIG_KEY}@{MESH3D}": "engine-config"}


@pytest.mark.parametrize(("settings", "message"), [
    ({"resolution": 768}, "resolution must be one of 512, 1024"),
    ({"seed": -1}, "seed must be a whole number"),
    ({"steps": 20}, "takes no setting steps"),
])
def test_mesh_settings_follow_the_recipe(qualified, settings, message):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    refused = run(client, id, settings=settings)
    assert refused.status_code == 422 and message in refused.json()["detail"]
    assert pool.submissions == []


def test_a_completed_job_becomes_a_candidate_with_attested_provenance(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    operation = run(client, id, settings={"seed": 7}).json()["operations"][0]
    content = glb(triangle(), binary=b"\4" * 8)
    pool.complete(operation["job_id"], content, diagnostics())
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    [candidate] = asset["candidates"]
    assert candidate["origin"] == "engine" and candidate["stage"] == "mesh"
    assert candidate["candidate_id"] == operation["operation_id"] and candidate["parent_id"] is None
    assert candidate["sha256"] == sha256(content).hexdigest()
    assert candidate["measured"]["triangles"] == 6  # Studio's own cross-check
    assert candidate["gate"] == {"name": "mesh", "passed": True,
                                 "checks": [{"id": "watertight", "passed": True, "value": None, "threshold": None}]}
    provenance = candidate["provenance"]
    assert provenance["worker_id"] == "house-4090" and provenance["recipe_version"] == qualified
    assert provenance["engine"] == {"version": "0.0.0", "commit": "c" * 40}
    assert provenance["effective_inputs"] == {"resolution": 1024, "seed": 7}
    assert provenance["engine_effective_inputs"]["texture_size"] == 2048
    assert provenance["engine_measured"] == {"triangles": 30_000}
    assert asset["operations"][0]["status"] == "succeeded"
    assert operation["output_key"] in asyncio.run(service.store.accounted_keys(id))
    assert asyncio.run(service.running_jobs("owner")) == 0
    # Reconciling again changes nothing.
    assert client.post(f"/studio/3d/{id}/reconcile").json()["revision"] == asset["revision"]


def test_an_omitted_gate_counts_as_failed_and_blocks_cleaning(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    operation = run(client, id).json()["operations"][0]
    pool.complete(operation["job_id"], glb(), diagnostics(gate={"omitted": "exceeds the diagnostics bound"}))
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    candidate = asset["candidates"][0]
    assert candidate["gate"]["passed"] is False
    asset = client.post(f"/studio/3d/{id}/candidates/{candidate['candidate_id']}/review",
                        json={"expected_revision": asset["revision"], "outcome": "keep"}).json()
    refused = run(client, id, "clean", "c1", parent_id=candidate["candidate_id"])
    assert refused.status_code == 409 and "failed its mesh gate" in refused.json()["detail"]


def test_clean_needs_a_kept_mesh_and_exactly_one_budget(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    operation = run(client, id).json()["operations"][0]
    pool.complete(operation["job_id"], glb(), diagnostics())
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    mesh = asset["candidates"][0]["candidate_id"]
    assert run(client, id, "clean", "c1").status_code == 422  # no parent
    assert run(client, id, "clean", "c1", parent_id="nope").status_code == 404
    not_kept = run(client, id, "clean", "c1", parent_id=mesh)
    assert not_kept.status_code == 409 and "Keep or select" in not_kept.json()["detail"]
    client.post(f"/studio/3d/{id}/candidates/{mesh}/review",
                json={"expected_revision": asset["revision"], "outcome": "keep"})
    both = run(client, id, "clean", "c1", parent_id=mesh,
               settings={"quality_tier": "high", "target_triangles": 20_000})
    assert both.status_code == 422 and "exactly one" in both.json()["detail"]
    assert run(client, id, "mesh", "m2", parent_id=mesh).status_code == 422
    sent = run(client, id, "clean", "c1", parent_id=mesh)
    assert sent.status_code == 200, sent.text
    job = pool.submissions[-1]
    assert job.capability_id == MESH3D_CLEAN_CAPABILITY
    assert job.payload == {"inputs": {"mesh": operation["output_key"]}, "quality_tier": "standard"}
    assert job.execution_deadline_seconds == 600
    clean = sent.json()["operations"][-1]
    pool.complete(clean["job_id"], glb(binary=b"\5" * 8), diagnostics("clean"))
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    made = next(c for c in asset["candidates"] if c["stage"] == "clean")
    assert made["parent_id"] == mesh and made["origin"] == "engine"


def test_failed_jobs_and_interrupted_submissions(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    pool.fail_submit = 1
    sent = run(client, id).json()
    operation = sent["operations"][0]
    # The journal survives a submission that did not get through; reconcile retries
    # the same job.
    assert operation["status"] == "submitting" and "reconcile" in operation["error"]
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    assert asset["operations"][0]["status"] == "running"
    assert {s.job_id for s in pool.submissions} == {operation["job_id"]}
    pool.fail(operation["job_id"], "the engine exited 1: out of memory")
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    assert asset["operations"][0]["status"] == "failed"
    assert asset["operations"][0]["error"] == "the engine exited 1: out of memory"
    assert asset["candidates"] == []


def test_public_accounts_use_only_their_own_machine(qualified):
    # A public account with no machine of its own is refused, even with a house worker online.
    client, service, assets, pool = build(engine_worker("house-4090", None), public=True)
    assert client.get("/studio/3d/capabilities").json()["stages"]["mesh"]["reason"] == PUBLIC_NEEDS_OWN_MACHINE
    id = with_front(client, assets)
    refused = run(client, id)
    assert refused.status_code == 409 and refused.json()["detail"]["message"] == PUBLIC_NEEDS_OWN_MACHINE
    assert pool.submissions == []
    # With its own machine, only that machine is pinned, in its own tenant.
    client, service, assets, pool = build(engine_worker("house-4090", None), engine_worker("theirs", "owner"),
                                          public=True, own_machine=True)
    id = with_front(client, assets)
    assert run(client, id).status_code == 200
    job = pool.submissions[0]
    assert job.requirements.worker_ids == ("theirs",) and job.tenant_id == "owner"


def test_deleting_an_asset_cancels_its_running_stages(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    operation = run(client, id).json()["operations"][0]
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Orc"}).status_code == 204
    assert pool.cancelled == [operation["job_id"]]


def test_the_concurrency_guard_is_asked_for_one_more_job(qualified):
    from magic_video_editor.assets3d.service import Asset3DError

    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    asked = []

    async def guard(owner, requested):
        asked.append(requested)
        if requested:
            raise Asset3DError("Concurrent generation limit reached. Wait for work to finish.", 429)

    service.work_guard = guard
    assert run(client, id).status_code == 429 and pool.submissions == [] and 1 in asked


# -- races and reservations on the generation path ----------------------------


def test_concurrent_calls_reusing_a_request_id_for_different_settings(qualified, monkeypatch):
    """Both pass the first look; the one that loses the journal race must still be
    compared, not handed the winner's run."""
    from magic_video_editor.assets3d.service import Asset3DError

    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    pin = service.stages.pin

    async def slow_pin(*args, **kwargs):
        await asyncio.sleep(0)
        return await pin(*args, **kwargs)

    monkeypatch.setattr(service.stages, "pin", slow_pin)

    async def race():
        return await asyncio.gather(*(
            service.submit_stage(id, "owner", "mesh", "r1", None, {"seed": seed}) for seed in (7, 8)
        ), return_exceptions=True)

    results = asyncio.run(race())
    refused = [r for r in results if isinstance(r, Asset3DError)]
    assert len(refused) == 1 and refused[0].code == "request_id_reused"
    [operation] = asyncio.run(service.store.get(id)).operations
    assert len(pool.submissions) == 1
    assert pool.submissions[0].payload["seed"] == operation.settings["seed"]
    # The same request racing itself is still one run, with no refusal.
    other = with_front(client, assets)

    async def same():
        return await asyncio.gather(*(
            service.submit_stage(other, "owner", "mesh", "r1", None, {"seed": 7}) for _ in range(2)
        ))

    first, second = asyncio.run(same())
    assert len(second.operations) == 1


def test_expired_grants_are_given_back_before_a_stage_is_refused_for_storage(qualified):
    from magic_video_editor.assets3d.service import Asset3DError

    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    stale = client.post("/studio/3d", json={"name": "Stale"}).json()["asset_id"]
    for _ in range(6):
        assert client.post(f"/studio/3d/{stale}/uploads", json={"content_type": "model/gltf-binary"}).status_code == 200

    async def age():
        asset = await service.store.get(stale)
        asset.pending_uploads = {key: "2026-01-01T00:00:00+00:00" for key in asset.pending_uploads}
        await service.store.append(asset, asset.revision)

    asyncio.run(age())
    cap = 1024 * 1024 * 1024
    asked = []

    async def guard(owner, requested):
        asked.append(requested)
        if await service.store.stored_bytes(owner) >= cap:
            raise Asset3DError("Storage limit reached. Free storage before starting new work.", 507)

    service.work_guard = guard
    sent = run(client, id)
    assert sent.status_code == 200, sent.text
    assert client.get(f"/studio/3d/{stale}").json()["pending_uploads"] == {}
    # The additional job is what the guard was asked about, before and after.
    assert asked == [1, 1]


def test_a_stage_journalled_while_the_delete_marks_the_asset_is_cancelled(qualified, monkeypatch):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    mark = service.store.mark_deleting
    journalled = []

    async def journal_then_mark(asset_id):
        # A concurrent stage request lands its journal after the delete read the
        # asset, before the mark.
        asset = await service.submit_stage(asset_id, "owner", "mesh", "late", None, {})
        journalled.append(asset.operations[-1].job_id)
        await mark(asset_id)

    monkeypatch.setattr(service.store, "mark_deleting", journal_then_mark)
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Orc"}).status_code == 204
    assert journalled and pool.cancelled == journalled


def test_a_submission_that_crosses_a_delete_is_cancelled_by_reconcile(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    submit = pool.submit

    async def submit_as_the_asset_is_marked(submission):
        record = await submit(submission)
        await service.store.mark_deleting(id)
        return record

    pool.submit = submit_as_the_asset_is_marked
    refused = run(client, id)
    assert refused.status_code == 404
    assert pool.cancelled == [pool.submissions[0].job_id]


def test_reconciling_an_asset_being_deleted_removes_finished_output(qualified):
    client, service, assets, pool = build(engine_worker())
    id = with_front(client, assets)
    operation = run(client, id).json()["operations"][0]
    pool.complete(operation["job_id"], glb(), diagnostics())
    asyncio.run(service.store.mark_deleting(id))
    asset = client.post(f"/studio/3d/{id}/reconcile").json()
    assert asset["candidates"] == []
    assert not any(key.startswith(f"outputs/assets3d/{id}/") for key in assets.assets)


@pytest.mark.parametrize(("value", "on"), [
    (None, False), ("", False), ("false", False), ("False", False), ("0", False), ("1", False),
    ("yes", False), ("no", False), ("true", True), ("TRUE", True), (" true ", True),
])
def test_only_an_explicit_true_turns_generation_on(value, on):
    from magic_video_editor.assets3d.stages import GENERATION_ENV, generation_enabled

    assert generation_enabled({} if value is None else {GENERATION_ENV: value}) is on
