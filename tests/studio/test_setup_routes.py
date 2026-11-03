"""Public setup boundary: version negotiation, actor scope and revision fencing."""
from fastapi.testclient import TestClient
from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


def client(actor="owner", runtime=None, version=True):
    if runtime is None:
        assets = MemoryAssetStore()
        runtime = InMemoryRuntime(MemoryJobStore(), assets,
            studio_service=StudioService(store=MemoryStudioStore(), assets=assets))
    headers = {"X-Studio-Actor-Id": actor}
    if version:
        headers["X-Studio-Setup-Version"] = "1"
    return TestClient(create_app(runtime), headers=headers), runtime


def test_setup_routes_version_actor_partial_matrix_and_stale_plan():
    owner, runtime = client()
    project = owner.post("/studio/projects", json={"name": "Original"}).json()
    base = f'/studio/projects/{project["project_id"]}'
    desired = {"schema_version": 1, "sprite_layout": "single_view", "views": [
        {"view_id": "single", "name": "Original view", "heading": None, "motions": [
            {"motion_id": "idle", "name": "Idle", "preset": "idle"},
            {"motion_id": "death", "name": "Death", "preset": "death"}]}]}
    body = {"expected_revision": project["revision"], "desired_setup": desired}
    legacy, _ = client(runtime=runtime, version=False)
    unsupported = legacy.post(base + "/setup/preview", json=body)
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["code"] == "unsupported_setup_version"
    foreign, _ = client("foreign", runtime)
    assert foreign.post(base + "/setup/preview", json=body).status_code == 404
    capabilities = owner.get("/studio/setup-capabilities").json()
    assert capabilities["partial_matrix"] and "single_view" in capabilities["conventions"]
    preview = owner.post(base + "/setup/preview", json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert owner.get(base).json()["revision"] == project["revision"]
    applied = owner.post(base + "/setup/apply", json={"expected_revision": project["revision"],
        "plan_token": plan["plan_token"], "confirmed_impacts": []})
    assert applied.status_code == 200, applied.text
    current = owner.get(base + "/setup").json()
    assert [m["motion_id"] for m in current["setup"]["views"][0]["motions"]] == ["idle", "death"]
    assert owner.get(base).json()["active_operations"] == []
    assert legacy.get(base).status_code == 409
    assert legacy.put(base + "/sprite-layout", json={"expected_revision": current["revision"],
        "sprite_layout": "unassigned"}).status_code == 409
    assert owner.post(base + "/setup/apply", json={"expected_revision": project["revision"],
        "plan_token": plan["plan_token"], "confirmed_impacts": []}).status_code == 409


def test_setup_apply_reports_structured_confirmation_and_active_operation_conflicts():
    from unittest.mock import AsyncMock
    from magic_video_editor.studio.setup import SetupBlocked, SetupConfirmationRequired, SetupConflict
    owner, runtime = client()
    project = owner.post("/studio/projects", json={"name": "Scoped edit"}).json()
    base = f'/studio/projects/{project["project_id"]}/setup/apply'
    for error, code in [(SetupBlocked("active operation job-1"), "setup_blocked"),
                        (SetupConfirmationRequired("confirm remove-motion-id"), "setup_confirmation_required"),
                        (SetupConflict("stale plan"), "setup_conflict")]:
        runtime.studio_service.apply_project_setup = AsyncMock(side_effect=error)
        result = owner.post(base, json={"expected_revision": 1, "plan_token": "plan"})
        assert result.status_code == 409
        assert result.json()["detail"] == {"code": code, "message": str(error)}
        assert owner.get(f'/studio/projects/{project["project_id"]}').json()["revision"] == 1


def test_setup_rejects_unverified_reference_revision_and_unknown_version():
    owner, _ = client()
    project = owner.post("/studio/projects", json={"name": "References"}).json()
    path = f'/studio/projects/{project["project_id"]}/setup/preview'
    body = {"expected_revision": 1, "desired_setup": {"schema_version": 1,
        "sprite_layout": "single_view", "identity_reference": {"revision_id": "foreign-revision"},
        "views": [{"view_id": "single", "heading": None, "name": "Original", "motions": []}]}}
    rejected = owner.post(path, json=body)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "setup_conflict"
    assert owner.get(f'/studio/projects/{project["project_id"]}').json()["revision"] == 1
    unknown = owner.post(path, json=body, headers={"X-Studio-Setup-Version": "99"})
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "unsupported_setup_version"


def test_setup_reference_staging_validates_and_does_not_mutate_project():
    import base64
    from io import BytesIO
    from PIL import Image
    owner, runtime = client()
    project = owner.post("/studio/projects", json={"name": "Art"}).json()
    base = f'/studio/projects/{project["project_id"]}'
    output = BytesIO()
    Image.new("RGB", (128, 128), "white").save(output, format="PNG")
    body = {"content_base64": base64.b64encode(output.getvalue()).decode(), "content_type": "image/png"}
    foreign, _ = client("foreign", runtime)
    assert foreign.post(base + "/setup/references", json=body).status_code == 404
    staged = owner.post(base + "/setup/references", json=body)
    assert staged.status_code == 200, staged.text
    reference = staged.json()
    assert reference["revision_id"] and reference["sha256"]
    assert owner.get(base).json()["revision"] == 1
    assert owner.get(base).json()["manifest"]["subject"]["current_revision"] is None
    desired = {"schema_version": 1, "sprite_layout": "single_view",
        "identity_reference": {"revision_id": reference["revision_id"]},
        "views": [{"view_id": "single", "heading": None, "name": "Original", "motions": [],
                   "reference": {"revision_id": reference["revision_id"]}}]}
    plan = owner.post(base + "/setup/preview", json={"expected_revision": 1, "desired_setup": desired})
    assert plan.status_code == 200, plan.text
    applied = owner.post(base + "/setup/apply", json={"expected_revision": 1,
        "plan_token": plan.json()["plan_token"], "confirmed_impacts": [
            impact["impact_id"] for impact in plan.json()["impacts"] if impact["requires_confirmation"]]})
    assert applied.status_code == 200, applied.text
    assert applied.json()["manifest"]["subject"]["approval"]["state"] == "approved"
    assert applied.json()["active_operations"] == []
    invalid = owner.post(base + "/setup/references", json={"content_base64": "???", "content_type": "image/png"})
    assert invalid.status_code == 422


def test_dispatch_recovery_requires_confirmation_and_preserves_active_job_fence():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from magic_video_editor.contracts import JobStatus
    from magic_video_editor.studio.models import MotionDispatchReservation
    owner, runtime = client()
    project = owner.post("/studio/projects", json={"name": "Recovery"}).json()
    service = runtime.studio_service
    current = asyncio.run(service.get_project(project["project_id"], "owner"))
    reserved = asyncio.run(service._store.append(project["project_id"], current.revision,
        current.manifest.model_copy(update={"dispatch_reservations": (
            MotionDispatchReservation(operation_id="stranded", motion_id="idle", requested_revision=1),)})))
    path = f'/studio/projects/{project["project_id"]}/setup/dispatches/stranded/abandon'
    body = {"expected_revision": reserved.revision}
    assert owner.post(path, json=body).json()["detail"]["code"] == "setup_confirmation_required"
    service._jobs = runtime.jobs
    service._find_dispatch_job = AsyncMock(return_value=SimpleNamespace(status=JobStatus.QUEUED))
    body["confirm_dispatch_stopped"] = True
    active = owner.post(path, json=body)
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "setup_blocked"
    service._find_dispatch_job = AsyncMock(return_value=None)
    recovered = owner.post(path, json=body)
    assert recovered.status_code == 200, recovered.text
    latest = asyncio.run(service.get_project(project["project_id"], "owner"))
    assert latest.manifest.dispatch_reservations == ()
    assert "stranded" in latest.manifest.abandoned_dispatches
    assert owner.post(path, json=body).status_code == 409


def test_versioned_isometric_heading_requires_client_version_but_legacy_does_not():
    owner, runtime = client()
    project = owner.post("/studio/projects", json={"name": "Heading"}).json()
    base = f'/studio/projects/{project["project_id"]}'
    legacy, _ = client(runtime=runtime, version=False)
    assert legacy.get(base).status_code == 200
    configured = legacy.put(base + "/sprite-layout", json={"expected_revision": 1, "sprite_layout": "isometric"})
    assert configured.status_code == 200
    assert legacy.get(base).status_code == 200
    desired = {"schema_version": 1, "sprite_layout": "isometric", "views": [
        {"view_id": "N", "heading": "E", "name": "East artwork", "motions": []}]}
    preview = owner.post(base + "/setup/preview", json={"expected_revision": configured.json()["revision"],
        "desired_setup": desired})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    applied = owner.post(base + "/setup/apply", json={"expected_revision": plan["expected_revision"],
        "plan_token": plan["plan_token"], "confirmed_impacts": [
            impact["impact_id"] for impact in plan["impacts"] if impact["requires_confirmation"]]})
    assert applied.status_code == 200, applied.text
    assert owner.get(base).status_code == 200
    unsupported = legacy.get(base)
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["code"] == "unsupported_setup_version"
    mutation = {"expected_revision": applied.json()["revision"], "name": "Renamed"}
    assert legacy.put(base + "/views/N", json=mutation).status_code == 409
    assert owner.put(base + "/views/N", json=mutation).status_code == 200
