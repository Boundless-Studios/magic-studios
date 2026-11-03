"""Direction drafts from picked turnaround frames, their checks, and binding through setup."""

import io
import json
import shutil

import pytest
from PIL import Image

from magic_video_editor.studio.manifest import set_digest
from magic_video_editor.studio.models import ViewId
from magic_video_editor.studio.setup import SetupConflict, current_setup
from magic_video_editor.studio.view_drafts import DraftUnavailable

from .test_turnarounds import PROJECT, _picks_from, _project, _settled, _start

pytestmark = pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffmpeg is required to decode the turnaround")


async def _real_identity_anchor(service, assets):
    """The shared fixture's subject is a placeholder; setup revalidates the anchor's bytes."""
    from hashlib import sha256

    from .test_turnarounds import _png
    current = await service.get_project(PROJECT, "user-1")
    subject = current.manifest.subject
    revision = subject.revisions[0]
    content = _png()
    await assets.write_once(revision.artifact_key, content, "image/png")
    revisions = tuple(r.model_copy(update={"sha256": sha256(content).hexdigest(), "width": 80, "height": 100})
                      if r.revision_id == revision.revision_id else r for r in subject.revisions)
    await service._store.append(PROJECT, current.revision, current.manifest.model_copy(
        update={"subject": subject.model_copy(update={"revisions": revisions})}))


async def _picked():
    service, pool, assets, project = await _project()
    await _real_identity_anchor(service, assets)
    project = await service.get_project(PROJECT, "user-1")
    take, record = await _settled(service, pool, project)
    suggested = (await service.get_turnaround(PROJECT, take.turnaround_id, "user-1"))["suggested_picks"]
    await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, _picks_from(suggested), "user-1")
    return service, pool, assets, take, suggested


async def _draft(service, take, heading=ViewId.N, request_id="d-1", pick_revision=1):
    current = await service.get_project(PROJECT, "user-1")
    return await service.create_view_draft(PROJECT, current.revision, take.turnaround_id, pick_revision,
                                           heading, False, request_id, "user-1")


async def _check(service, draft_id, facing=True, equipment=True, identity=True):
    current = await service.get_project(PROJECT, "user-1")
    return await service.check_view_draft(PROJECT, draft_id, current.revision, facing, equipment, identity, "user-1")


async def _bind(service, draft_id, heading="N", view_id="N", confirm=()):
    current = await service.get_project(PROJECT, "user-1")
    desired = current_setup(current.manifest).model_dump(mode="json")
    views = [v for v in desired["views"] if v["view_id"] != view_id]
    views.append({"view_id": view_id, "heading": heading, "name": view_id, "reference": {"revision_id": draft_id},
                  "motions": []})
    desired["views"] = views
    plan = await service.preview_project_setup(PROJECT, current.revision, desired, "user-1")
    return plan, current.revision


async def test_a_frame_draft_is_the_picked_frame_keyed_and_framed_with_its_card():
    service, _, assets, take, suggested = await _picked()
    draft = await _draft(service, take)
    assert draft["draft_id"].startswith("draft-") and draft["heading"] == "N"
    assert draft["frame_index"] == suggested["N"]["frame"] and draft["pick_revision"] == 1
    image = Image.open(io.BytesIO(assets.assets[draft["image"]["key"]]))
    assert image.size == (1024, 1024) and image.mode == "RGBA" and image.getpixel((0, 0))[3] == 0
    assert draft["placement_card"][0] == "Back to you."
    assert draft["synthesized_parts"] and draft["processing"].startswith("key_green_screen")
    assert not draft["checked"] and not draft["bindable"] and draft["check"] is None
    # A repeated request is the same draft, and nothing is added twice.
    again = await _draft(service, take)
    assert again["draft_id"] == draft["draft_id"]
    assert len((await service.get_project(PROJECT, "user-1")).manifest.view_drafts) == 1


async def test_drafts_never_change_the_set_or_its_completeness():
    service, _, _, take, _ = await _picked()
    before = await service.get_project(PROJECT, "user-1")
    await _draft(service, take)
    after = await service.get_project(PROJECT, "user-1")
    assert set_digest(after.manifest) == set_digest(before.manifest)
    assert after.manifest.views == before.manifest.views


@pytest.mark.parametrize("change,message", [
    ("standing", "standing pass is not available"),
    ("source", "is the source"),
    ("stale_picks", "current saved pick set"),
])
async def test_drafts_are_refused_for_the_standing_pass_the_source_and_a_stale_pick_set(change, message):
    service, _, _, take, _ = await _picked()
    current = await service.get_project(PROJECT, "user-1")
    heading = ViewId.S if change == "source" else ViewId.N
    with pytest.raises(DraftUnavailable, match=message):
        await service.create_view_draft(PROJECT, current.revision, take.turnaround_id,
                                        2 if change == "stale_picks" else 1, heading, change == "standing",
                                        "d-x", "user-1")


async def test_a_draft_needs_a_settled_take():
    from magic_video_editor.studio.service import MotionOperationNotReady
    service, _, _, project = await _project()
    take, _ = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    with pytest.raises(MotionOperationNotReady):
        await service.create_view_draft(PROJECT, current.revision, take.turnaround_id, 1, ViewId.N, False,
                                        "d-1", "user-1")


async def test_only_a_full_check_makes_a_draft_bindable_and_each_check_is_a_receipt():
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    partial = await _check(service, draft["draft_id"], equipment=False)
    assert not partial["checked"] and partial["check"]["number"] == 1
    full = await _check(service, draft["draft_id"])
    assert full["checked"] and full["bindable"] and full["check"]["number"] == 2
    receipts = sorted(k for k in assets.assets if ".check-" in k)
    assert len(receipts) == 2
    first = json.loads(assets.assets[receipts[0]])
    assert first["placement_card"] == draft["placement_card"] and first["card_digest"] == draft["card_digest"]


async def test_a_checked_draft_binds_to_its_heading_through_setup_and_leaves_a_receipt():
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    plan, revision = await _bind(service, draft["draft_id"])
    applied = await service.apply_project_setup(
        PROJECT, revision, plan.plan_token, [i.impact_id for i in plan.impacts if i.requires_confirmation], "user-1")
    view = next(v for v in applied.manifest.views if str(v.view_id) == "N")
    assert view.reference.revision_id == draft["draft_id"]
    assert view.reference.sha256 == draft["image"]["sha256"]
    assert any(k.endswith(f"{draft['draft_id']}.bound-N-r{applied.revision}.json") for k in assets.assets)
    [listed] = await service.list_view_drafts(PROJECT, "user-1")
    assert listed["bound_view_ids"] == ["N"] and not listed["bindable"]


async def test_an_unchecked_draft_or_the_wrong_heading_is_refused_at_preview():
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    with pytest.raises(SetupConflict, match="check"):
        await _bind(service, draft["draft_id"])
    await _check(service, draft["draft_id"])
    with pytest.raises(SetupConflict, match="its own heading"):
        await _bind(service, draft["draft_id"], heading="W", view_id="W")


async def test_a_draft_discarded_after_its_plan_was_previewed_is_refused_at_apply():
    from magic_video_editor.studio.store import RevisionConflict
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    plan, revision = await _bind(service, draft["draft_id"])
    await service.discard_view_draft(PROJECT, draft["draft_id"], revision, "user-1")
    # Discard moved the revision, so the older plan cannot apply, and a new preview refuses it.
    with pytest.raises(RevisionConflict):
        await service.apply_project_setup(PROJECT, revision, plan.plan_token,
                                          [i.impact_id for i in plan.impacts if i.requires_confirmation], "user-1")
    with pytest.raises(SetupConflict, match="discarded"):
        await _bind(service, draft["draft_id"])
    with pytest.raises(DraftUnavailable, match="discarded"):
        await _check(service, draft["draft_id"])


async def test_a_draft_from_a_replaced_source_needs_confirmation_to_bind():
    from hashlib import sha256

    from .test_turnarounds import _png
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    current = await service.get_project(PROJECT, "user-1")
    replacement = _png(size=(90, 110))
    key = f"sources/studio/{PROJECT}/views/S/references/b.png"
    await assets.write_once(key, replacement, "image/png")
    await service.set_character_view(PROJECT, current.revision, ViewId.S, "user-1",
                                     reference=(key, sha256(replacement).hexdigest()), replace_reference=True)
    plan, _ = await _bind(service, draft["draft_id"])
    superseded = [i for i in plan.impacts if i.kind == "draft_source_superseded"]
    assert len(superseded) == 1 and superseded[0].requires_confirmation


def test_the_draft_routes_make_check_and_refuse_as_documented():
    import asyncio

    from fastapi.testclient import TestClient

    from magic_video_editor.app import ServiceRole, create_app
    from magic_video_editor.memory import InMemoryRuntime

    service, _, assets, take, _ = asyncio.run(_picked())
    headers = {"X-Studio-Actor-Id": "user-1"}
    base = f"/studio/projects/{PROJECT}/view-drafts"
    with TestClient(create_app(InMemoryRuntime(service._jobs, assets, studio_service=service),
                               service_role=ServiceRole.API)) as client:
        revision = client.get(f"/studio/projects/{PROJECT}", headers=headers).json()["revision"]
        body = {"expected_revision": revision, "turnaround_id": take.turnaround_id, "pick_revision": 1,
                "heading": "N", "request_id": "route-1"}
        assert client.post(base, headers=headers, json={**body, "standing": True}).status_code == 422
        assert client.post(base, headers=headers, json={**body, "expected_revision": revision + 5}).status_code == 409
        created = client.post(base, headers=headers, json=body)
        assert created.status_code == 200, created.text
        draft_id = created.json()["draft_id"]
        revision = client.get(f"/studio/projects/{PROJECT}", headers=headers).json()["revision"]
        checked = client.post(f"{base}/{draft_id}/check", headers=headers,
                              json={"expected_revision": revision, "facing": True, "equipment": True, "identity": True})
        assert checked.status_code == 200 and checked.json()["bindable"]
        image = client.get(checked.json()["image_path"], headers=headers)
        assert image.status_code == 200 and image.content == assets.assets[checked.json()["image"]["key"]]
        assert [d["draft_id"] for d in client.get(base, headers=headers).json()["drafts"]] == [draft_id]
        assert client.post(f"{base}/draft-{'f' * 32}/discard", headers=headers,
                           json={"expected_revision": revision + 1}).status_code == 404


async def test_a_draft_lists_back_gear_it_shows_for_the_first_time_as_synthesized():
    from magic_video_editor.studio.models import TurnaroundEquipment

    from .test_turnarounds import AXE, _complete, _picks_from, _start
    service, pool, assets, project = await _project()
    await _real_identity_anchor(service, assets)
    project = await service.get_project(PROJECT, "user-1")
    quiver = TurnaroundEquipment(item="quiver", attachment="back", source_picture_side="unsided")
    take, record = await _start(service, project, equipment=(AXE, quiver))
    await _complete(service, pool, record, take)
    suggested = (await service.get_turnaround(PROJECT, take.turnaround_id, "user-1"))["suggested_picks"]
    await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, _picks_from(suggested), "user-1")
    # Hidden behind the body in the S source, the quiver is first seen side-on at E.
    draft = await _draft(service, take, heading=ViewId.E)
    assert draft["synthesized_parts"] == ["the character's right side", "the quiver on the back"]


async def _apply(service, plan, revision):
    return await service.apply_project_setup(
        PROJECT, revision, plan.plan_token, [i.impact_id for i in plan.impacts if i.requires_confirmation], "user-1")


async def _bound(service, take, heading=ViewId.N):
    draft = await _draft(service, take, heading=heading)
    await _check(service, draft["draft_id"])
    plan, revision = await _bind(service, draft["draft_id"], heading=heading.value, view_id=heading.value)
    return draft, await _apply(service, plan, revision)


async def test_a_bound_draft_is_revalidated_before_it_is_reused_for_another_direction():
    service, _, _, take, _ = await _picked()
    draft, _ = await _bound(service, take)
    # Already the N reference, the same draft may not become W's reference.
    with pytest.raises(SetupConflict, match="its own heading"):
        await _bind(service, draft["draft_id"], heading="W", view_id="W")
    # Nor can it be discarded while it is a reference.
    current = await service.get_project(PROJECT, "user-1")
    with pytest.raises(DraftUnavailable, match="reference"):
        await service.discard_view_draft(PROJECT, draft["draft_id"], current.revision, "user-1")


async def test_a_check_whose_manifest_update_lost_its_race_is_recovered():
    from magic_video_editor.studio.store import RevisionConflict
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    append, lost = service._store.append, []

    async def loses_once(project_id, revision, manifest):
        if not lost and any(d.check_count for d in manifest.view_drafts):
            lost.append(revision)
            raise RevisionConflict(revision)
        return await append(project_id, revision, manifest)

    service._store.append = loses_once
    checked = await _check(service, draft["draft_id"])
    assert lost and checked["checked"] and checked["check"]["number"] == 2
    # The orphaned first receipt never blocks the next check.
    again = await _check(service, draft["draft_id"], identity=False)
    assert again["check"]["number"] == 3 and not again["checked"]


async def test_every_stored_draft_asset_counts_toward_storage_and_is_released_on_delete():
    service, _, assets, take, _ = await _picked()
    before = await service._owner_usage.stored_bytes("user-1")
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    stored = [k for k in assets.assets if "/view-drafts/" in k]
    assert len(stored) == 3  # image, descriptor, check receipt
    assert await service._owner_usage.stored_bytes("user-1") == before + sum(len(assets.assets[k]) for k in stored)
    # A repeated request writes nothing and counts nothing again.
    await _draft(service, take)
    assert await service._owner_usage.stored_bytes("user-1") == before + sum(len(assets.assets[k]) for k in stored)
    current = await service.get_project(PROJECT, "user-1")
    await service.delete_project(PROJECT, "user-1", current.manifest.name)
    assert await service._owner_usage.stored_bytes("user-1") == 0


async def test_a_discard_that_lands_during_an_apply_is_never_overtaken_by_it(monkeypatch):
    from magic_video_editor.studio import setup
    from magic_video_editor.studio.store import RevisionConflict
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    plan, revision = await _bind(service, draft["draft_id"])
    reconcile = setup.reconcile

    async def discard_meanwhile(service_, current, desired):
        result = await reconcile(service_, current, desired)
        await service.discard_view_draft(PROJECT, draft["draft_id"], current.revision, "user-1")
        return result

    monkeypatch.setattr(setup, "reconcile", discard_meanwhile)
    with pytest.raises(RevisionConflict):
        await _apply(service, plan, revision)
    [listed] = await service.list_view_drafts(PROJECT, "user-1")
    assert listed["discarded"] and listed["bound_view_ids"] == []


async def test_a_draft_whose_source_direction_was_removed_is_refused():
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    current = await service.get_project(PROJECT, "user-1")
    desired = current_setup(current.manifest).model_dump(mode="json")
    desired["views"] = [v for v in desired["views"] if v["view_id"] != "S"]
    plan = await service.preview_project_setup(PROJECT, current.revision, desired, "user-1")
    await _apply(service, plan, current.revision)
    with pytest.raises(SetupConflict, match="removed"):
        await _bind(service, draft["draft_id"])


async def test_a_draft_made_while_the_picks_were_saved_again_is_kept_but_never_bindable():
    from magic_video_editor.studio.models import TurnaroundPick
    service, _, assets, take, suggested = await _picked()
    append, saved = service._store.append, []

    async def picks_saved_meanwhile(project_id, revision, manifest):
        if manifest.view_drafts and not saved:
            saved.append(True)
            picks = tuple(TurnaroundPick(heading=ViewId(h), frame_index=e["frame"])
                          for h, e in suggested.items() if h != "S")
            await service.save_turnaround_picks(PROJECT, take.turnaround_id, 1, picks, "user-1")
        return await append(project_id, revision, manifest)

    service._store.append = picks_saved_meanwhile
    draft = await _draft(service, take)
    service._store.append = append
    # Its stored assets stay listed (and so discardable), but it says why it can't be bound.
    [listed] = await service.list_view_drafts(PROJECT, "user-1")
    assert listed["draft_id"] == draft["draft_id"] and "picks changed" in listed["stale_reason"]
    checked = await _check(service, draft["draft_id"])
    assert checked["checked"] and not checked["bindable"]
    with pytest.raises(SetupConflict, match="picks changed"):
        await _bind(service, draft["draft_id"])


async def test_a_draft_from_an_older_pick_set_is_refused_at_preview():
    service, _, _, take, suggested = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    from .test_turnarounds import _picks_from
    await service.save_turnaround_picks(PROJECT, take.turnaround_id, 1, _picks_from(suggested), "user-1")
    with pytest.raises(SetupConflict, match="picks changed"):
        await _bind(service, draft["draft_id"])


async def test_a_heading_with_no_saved_pick_is_refused():
    service, _, _, take, _ = await _picked()
    with pytest.raises(DraftUnavailable, match="no saved pick"):
        await _draft(service, take, heading=ViewId.LEFT)


async def test_a_frame_that_cannot_be_keyed_is_a_draft_error(monkeypatch):
    from magic_video_editor.plugins import subject_pool
    from magic_video_editor.plugins.subject_http import TerminalSubjectProviderError
    service, _, _, take, _ = await _picked()

    def unkeyable(frame):
        raise TerminalSubjectProviderError("no green-dominant border")

    monkeypatch.setattr(subject_pool, "key_green_screen", unkeyable)
    with pytest.raises(DraftUnavailable, match="could not be keyed"):
        await _draft(service, take)


async def test_a_bind_receipt_that_cannot_be_written_never_fails_the_committed_setup(monkeypatch):
    from magic_video_editor.contracts import AssetStorageUnavailable
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    plan, revision = await _bind(service, draft["draft_id"])
    write_once = assets.write_once

    async def no_receipts(key, *args, **kwargs):
        if ".bound-" in key:
            raise AssetStorageUnavailable("storage is down")
        return await write_once(key, *args, **kwargs)

    monkeypatch.setattr(assets, "write_once", no_receipts)
    applied = await _apply(service, plan, revision)
    assert next(v for v in applied.manifest.views if str(v.view_id) == "N").reference.revision_id == draft["draft_id"]


async def test_binding_the_same_draft_again_leaves_a_second_receipt():
    service, _, assets, take, _ = await _picked()
    draft, first = await _bound(service, take)
    source = next(v for v in first.manifest.views if str(v.view_id) == "S").reference.revision_id
    # Point N at another reference, then back at the draft.
    desired = current_setup(first.manifest).model_dump(mode="json")
    for view in desired["views"]:
        if view["view_id"] == "N":
            view["reference"] = {"revision_id": source}
    plan = await service.preview_project_setup(PROJECT, first.revision, desired, "user-1")
    cleared = await _apply(service, plan, first.revision)
    plan, revision = await _bind(service, draft["draft_id"])
    again = await _apply(service, plan, revision)
    receipts = sorted(k for k in assets.assets if ".bound-N-r" in k)
    assert len(receipts) == 2 and receipts[-1].endswith(f"-r{again.revision}.json") and cleared.revision < again.revision


async def test_a_take_whose_settlement_was_never_counted_cannot_be_drafted_or_bound():
    from magic_video_editor.studio.service import MotionOperationNotReady
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    current = await service.get_project(PROJECT, "user-1")
    takes = tuple(t.model_copy(update={"stored_bytes_recorded": False}) for t in current.manifest.turnarounds)
    await service._store.append(PROJECT, current.revision, current.manifest.model_copy(update={"turnarounds": takes}))
    with pytest.raises(MotionOperationNotReady):
        await _draft(service, take, request_id="d-2")
    with pytest.raises(SetupConflict, match="no longer matches"):
        await _bind(service, draft["draft_id"])


async def test_a_draft_records_the_suggestion_its_pick_was_checked_against(monkeypatch):
    from magic_video_editor.studio import turnarounds
    from magic_video_editor.studio.service import SuggestionsUnavailable
    service, _, _, take, suggested = await _picked()
    draft = await _draft(service, take)
    assert draft["suggested_frame"] == suggested["N"]["frame"] and draft["picks_version"] == turnarounds.PICKS_VERSION

    async def unavailable(*args):
        raise SuggestionsUnavailable("frame_analysis_failed", "no suggestions")

    monkeypatch.setattr(turnarounds, "_pick_analysis", unavailable)
    assert (await _draft(service, take, heading=ViewId.W, request_id="d-w"))["suggested_frame"] is None


def test_draft_storage_failures_have_their_own_responses(monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from magic_video_editor.app import ServiceRole, create_app
    from magic_video_editor.contracts import AssetStorageUnavailable
    from magic_video_editor.memory import InMemoryRuntime
    from magic_video_editor.studio.limits import OwnerStorageLimitExceeded

    service, _, assets, take, _ = asyncio.run(_picked())
    headers = {"X-Studio-Actor-Id": "user-1"}
    with TestClient(create_app(InMemoryRuntime(service._jobs, assets, studio_service=service),
                               service_role=ServiceRole.API)) as client:
        revision = client.get(f"/studio/projects/{PROJECT}", headers=headers).json()["revision"]
        body = {"expected_revision": revision, "turnaround_id": take.turnaround_id, "pick_revision": 1,
                "heading": "N", "request_id": "route-1"}
        write_once = assets.write_once

        async def down(key, *args, **kwargs):
            if "/view-drafts/" in key:
                raise AssetStorageUnavailable("storage is down")
            return await write_once(key, *args, **kwargs)

        monkeypatch.setattr(assets, "write_once", down)
        assert client.post(f"/studio/projects/{PROJECT}/view-drafts", headers=headers, json=body).status_code == 503
        monkeypatch.setattr(assets, "write_once", write_once)

        async def full(owner_id):
            raise OwnerStorageLimitExceeded(10, 10)

        monkeypatch.setattr(service, "_require_storage_headroom", full)
        assert client.post(f"/studio/projects/{PROJECT}/view-drafts", headers=headers, json=body).status_code == 507


async def test_checks_respect_the_storage_limit(monkeypatch):
    from magic_video_editor.studio.limits import OwnerStorageLimitExceeded
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)

    async def full(owner_id):
        raise OwnerStorageLimitExceeded(10, 10)

    monkeypatch.setattr(service, "_require_storage_headroom", full)
    with pytest.raises(OwnerStorageLimitExceeded):
        await _check(service, draft["draft_id"])
    assert not [k for k in assets.assets if ".check-" in k]


async def test_a_stale_draft_never_reports_itself_bindable():
    service, _, _, take, suggested = await _picked()
    draft = await _draft(service, take)
    checked = await _check(service, draft["draft_id"])
    assert checked["bindable"] and checked["stale_reason"] is None
    from .test_turnarounds import _picks_from
    await service.save_turnaround_picks(PROJECT, take.turnaround_id, 1, _picks_from(suggested), "user-1")
    [listed] = await service.list_view_drafts(PROJECT, "user-1")
    assert not listed["bindable"] and "picks changed" in listed["stale_reason"]


async def test_a_draft_whose_source_was_removed_never_reports_itself_bindable():
    service, _, _, take, _ = await _picked()
    draft = await _draft(service, take)
    await _check(service, draft["draft_id"])
    current = await service.get_project(PROJECT, "user-1")
    desired = current_setup(current.manifest).model_dump(mode="json")
    desired["views"] = [v for v in desired["views"] if v["view_id"] != "S"]
    plan = await service.preview_project_setup(PROJECT, current.revision, desired, "user-1")
    await _apply(service, plan, current.revision)
    [listed] = await service.list_view_drafts(PROJECT, "user-1")
    assert not listed["bindable"] and "removed" in listed["stale_reason"]


async def test_a_standing_request_is_refused_even_when_its_frame_draft_exists():
    service, _, _, take, _ = await _picked()
    await _draft(service, take)
    current = await service.get_project(PROJECT, "user-1")
    with pytest.raises(DraftUnavailable, match="standing pass"):
        await service.create_view_draft(PROJECT, current.revision, take.turnaround_id, 1, ViewId.N, True,
                                        "d-1", "user-1")


async def test_a_discard_retried_after_a_lost_response_answers_again(monkeypatch):
    from magic_video_editor.contracts import AssetStorageUnavailable
    service, _, assets, take, _ = await _picked()
    draft = await _draft(service, take)
    revision = (await service.get_project(PROJECT, "user-1")).revision
    read_limited, failing = assets.read_limited, [True]

    async def down_after_commit(key, *args, **kwargs):
        if failing[0] and key.endswith(f"{draft['draft_id']}.json") and (
                await service.get_project(PROJECT, "user-1")).manifest.view_drafts[0].discarded_at:
            raise AssetStorageUnavailable("storage is down")
        return await read_limited(key, *args, **kwargs)

    monkeypatch.setattr(assets, "read_limited", down_after_commit)
    with pytest.raises(AssetStorageUnavailable):
        await service.discard_view_draft(PROJECT, draft["draft_id"], revision, "user-1")
    failing[0] = False
    # The discard was committed; the retry with the original revision recovers.
    again = await service.discard_view_draft(PROJECT, draft["draft_id"], revision, "user-1")
    assert again["discarded"]
