import io
import json
from hashlib import sha256

import pytest
from PIL import Image
from pydantic import ValidationError

from magic_video_editor.api_models import GenerateMotionPayloadV1
from magic_video_editor.studio.manifest import manifest_digest, set_digest
from magic_video_editor.studio.models import (
    AnimationProjectManifest, Approval, ApprovalState, CharacterView, MotionSpecification,
    SpriteLayout, ViewId,
)
from magic_video_editor.studio.routes import MotionDefinition, MotionResponse, OperationResponse
from magic_video_editor.studio.service import MotionRequestBlocked, _motion_pool_payload, motion_spec_digest, project_file_path
from magic_video_editor.studio.store import RevisionConflict
from .test_motion_references import _service_with_approved_subject
from .test_releases import releasable_manifest


def png(color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new('RGBA', (80, 100), color).save(buffer, format='PNG')
    return buffer.getvalue()


async def view_project(views=(ViewId.N,)):
    service, store, assets, project = await _service_with_approved_subject()
    project = await service.set_sprite_layout('project-1', project.revision, SpriteLayout.ISOMETRIC, 'user-1')
    for index, view_id in enumerate(views):
        key = f'sources/studio/project-1/views/{view_id.value}/references/a.png'
        content = png((index * 25, 30, 40, 255))
        await assets.write_once(key, content, 'image/png')
        project = await service.set_character_view('project-1', project.revision, view_id, 'user-1',
            reference=(key, sha256(content).hexdigest()), replace_reference=True)
    return service, store, assets, project


@pytest.mark.parametrize('value', ['north', 1, True, ['N']])
def test_view_type_is_explicit(value):
    for model in (MotionDefinition, MotionSpecification):
        with pytest.raises(ValidationError):
            model(motion_id='walk-n', name='Walk', preset='walk', view_id=value)


def test_no_walk_only_field_or_child_reference_wire():
    for fields in ({'walk_direction': 'N'}, {'view_reference': {'key': 'a'}}):
        with pytest.raises(ValidationError):
            MotionDefinition(motion_id='walk-n', name='Walk', preset='walk', **fields)
    with pytest.raises(ValidationError, match='override'):
        MotionDefinition(motion_id='walk-n', name='Walk', preset='walk', view_id='N', references=[
            {'role':'start','key':'a','sha256':'a'*64},
        ]).to_specification()


def test_legacy_manifest_spec_and_set_digest_round_trip_unchanged():
    manifest = releasable_manifest()
    encoded = manifest.model_dump_json(exclude_none=True)
    assert 'sprite_layout' not in encoded and '"views"' not in encoded
    assert 'view_id' not in encoded and 'view_reference' not in encoded
    legacy = json.loads(encoded)
    restored = AnimationProjectManifest.model_validate(legacy)
    assert restored.sprite_layout is SpriteLayout.UNASSIGNED
    assert manifest_digest(restored) == sha256(encoded.encode()).hexdigest()
    assert set_digest(restored) == set_digest(manifest)
    assert restored.set_approval == manifest.set_approval
    spec = restored.motions[0].specification
    assert motion_spec_digest(spec) == sha256(json.dumps(json.loads(spec.model_dump_json(exclude_none=True)), separators=(',',':')).encode()).hexdigest()


async def test_eight_views_with_independent_walk_attack_and_custom_actions_dispatch_real_first_frames():
    views = tuple(view for view in ViewId if view not in {ViewId.LEFT, ViewId.RIGHT})
    service, _, assets, project = await view_project(views)
    specs = tuple(MotionSpecification(
        motion_id=f'{preset}-{view.value.lower()}', name=f'{preset} {view.value}', preset=preset,
        view_id=view, action='swings both axes' if preset == 'attack' else None,
    ) for view in views for preset in ('walk', 'attack'))
    project = await service.add_motions('project-1', project.revision, specs, 'user-1')
    service._owner_limits = service._owner_limits.model_copy(update={"concurrent_job_limit": 0})
    frames = {}
    for spec in specs:
        project = await service.get_project('project-1', 'user-1')
        job = await service.generate_motion('project-1', project.revision, spec.motion_id, 'user-1', request_id=spec.motion_id)
        payload = GenerateMotionPayloadV1.model_validate(job.payload)
        pool, inputs = _motion_pool_payload(payload)
        assert inputs == (payload.start_frame_key,)
        assert pool['images']['first_frame'] == payload.start_frame_key
        frame = assets.assets[payload.start_frame_key]
        assert sha256(frame).hexdigest() == payload.start_frame_sha256
        assert Image.open(io.BytesIO(frame)).size == (512,512)
        assert payload.view_reference['key'].startswith(f'sources/studio/project-1/views/{spec.view_id.value}/')
        assert 'actual camera projection' in payload.prompt
        assert 'retain the same view' in payload.prompt
        if spec.preset.value == 'walk':
            assert 'planted stance foot moving backward' in payload.prompt
            assert 'connected to its grip' not in payload.prompt
            frames[spec.view_id] = payload.start_frame_key
        else:
            assert 'swings both axes' in payload.prompt and 'committed strike' in payload.prompt
            assert 'reciprocal stride' not in payload.prompt
            assert 'held equipment coherent and connected to its grip' in payload.prompt
            assert frames[spec.view_id] != payload.start_frame_key
            assert payload.reference_preparation['algorithm'] == 'full_canvas_contain_fill_v2'
            assert payload.reference_preparation['fill'] == .58
        evidence = OperationResponse.from_domain(job).generation_evidence
        assert evidence['view_reference'] == payload.view_reference
        assert evidence['start_frame_sha256'] == payload.start_frame_sha256
    assert len(set(frames.values())) == 8
    assert len(project.manifest.motions) == 16
    project = await service.get_project('project-1', 'user-1')
    custom = MotionSpecification(motion_id='cheer-n', name='Cheer', kind='custom', prompt='Raises both axes', view_id='N')
    project = await service.add_motion('project-1', project.revision, custom, 'user-1')
    job = await service.generate_motion('project-1', project.revision, 'cheer-n', 'user-1')
    assert 'Raises both axes' in job.payload['prompt']
    assert 'actual camera projection' in job.payload['prompt']
    assert MotionResponse.from_domain(project.manifest.motions[-1]).view_reference is not None


async def test_missing_reference_blocks_before_queue_and_shared_pose_cannot_override():
    service, _, _, project = await _service_with_approved_subject()
    project = await service.set_sprite_layout('project-1', project.revision, SpriteLayout.SIDE_VIEW, 'user-1')
    project = await service.set_character_view('project-1', project.revision, ViewId.LEFT, 'user-1')
    project = await service.add_motion('project-1', project.revision, MotionSpecification(
        motion_id='attack-left',name='Attack',preset='attack',view_id='left'), 'user-1')
    with pytest.raises(MotionRequestBlocked, match='reference before generation'):
        await service.generate_motion('project-1', project.revision, 'attack-left', 'user-1')
    with pytest.raises(MotionRequestBlocked, match='shared starting pose'):
        await service.generate_motions('project-1', project.revision, ('attack-left',), 'user-1','batch',shared_starting_pose=True)
    assert await service._jobs.list_for_project('project-1') == ()


async def test_layout_cannot_orphan_and_unknown_motion_views_fail():
    service, _, _, project = await view_project()
    with pytest.raises(ValueError, match='orphan'):
        await service.set_sprite_layout('project-1',project.revision,SpriteLayout.SIDE_VIEW,'user-1')
    with pytest.raises(ValueError, match='declared'):
        await service.add_motion('project-1',project.revision,MotionSpecification(motion_id='walk-e',name='Walk',preset='walk',view_id='E'),'user-1')
    with pytest.raises(ValueError, match='layout'):
        await service.set_character_view('project-1',project.revision,ViewId.LEFT,'user-1')


@pytest.mark.parametrize('kind', ['foreign', 'hash', 'decode'])
async def test_binding_verifies_ownership_hash_and_decode(kind):
    service, _, assets, project = await view_project()
    key = f'sources/studio/{"foreign" if kind == "foreign" else "project-1"}/views/N/references/b.png'
    content = b'not-image' if kind == 'decode' else png()
    await assets.write_once(key,content,'image/png')
    with pytest.raises(ValueError):
        await service.set_character_view('project-1',project.revision,ViewId.N,'user-1',reference=(key,'a'*64 if kind == 'hash' else sha256(content).hexdigest()),replace_reference=True)


async def test_dispatch_rechecks_bound_hash():
    service, _, assets, project = await view_project()
    project = await service.add_motion('project-1',project.revision,MotionSpecification(motion_id='walk-n',name='Walk',preset='walk',view_id='N'),'user-1')
    assets.assets[project.manifest.views[0].reference.key] = png((0,0,255,255))
    with pytest.raises(ValueError, match='hash'):
        await service.generate_motion('project-1',project.revision,'walk-n','user-1')


async def test_reference_replacement_is_atomic_preserves_selected_takes_and_stales_inflight():
    from magic_video_editor.studio.models import MotionCandidate
    service, store, assets, project = await view_project()
    project = await service.add_motion('project-1',project.revision,MotionSpecification(motion_id='walk-n',name='Walk',preset='walk',view_id='N'),'user-1')
    job = await service.generate_motion('project-1',project.revision,'walk-n','user-1')
    project = await service.get_project('project-1', 'user-1')
    motion = project.manifest.motions[0]
    candidate = MotionCandidate(candidate_id='old',artifact_key='outputs/old.mp4',sha256='a'*64,motion_spec_digest=job.payload['motion_spec_digest'])
    motion = motion.model_copy(update={'candidates':(candidate,), 'selected_candidate_id':'old'})
    project = await store.append('project-1',project.revision,project.manifest.model_copy(update={'motions':(motion,), 'set_approval':Approval(state=ApprovalState.APPROVED)}))
    key = 'sources/studio/project-1/views/N/references/new.png'
    content = png((0,255,0,255)); await assets.write_once(key,content,'image/png')
    old_revision = project.revision
    project = await service.set_character_view('project-1',project.revision,ViewId.N,'user-1',reference=(key,sha256(content).hexdigest()),replace_reference=True)
    updated = project.manifest.motions[0]
    assert updated.candidates == (candidate,) and updated.selected_candidate_id == 'old'
    assert project.manifest.set_approval.state is ApprovalState.NOT_READY
    assert updated.specification.view_reference == project.manifest.views[0].reference
    assert motion_spec_digest(updated.specification) != job.payload['motion_spec_digest']
    with pytest.raises(RevisionConflict):
        await service.set_character_view('project-1',old_revision,ViewId.N,'user-1',reference=None,replace_reference=True)
    project = await service.set_character_view('project-1',project.revision,ViewId.N,'user-1',reference=None,replace_reference=True)
    assert project.manifest.motions[0].specification.view_reference is None
    assert project.manifest.motions[0].selected_candidate_id == 'old'


async def test_quality_slot_scope_export_metadata_and_no_implicit_mirror():
    from magic_video_editor.studio.exports import _package_manifest, PNG_JSON_V1, GAIA_V1, ReleaseExporter
    from magic_video_editor.studio.memory import MemoryStudioStore
    from magic_video_editor.memory import MemoryAssetStore
    from magic_video_editor.studio.service import StudioService
    from magic_video_editor.studio.models import MotionState
    from magic_video_editor.studio.quality import assess_set
    base = releasable_manifest()
    store = MemoryStudioStore(); assets = MemoryAssetStore()
    created = await store.create(base)
    release = await StudioService(store=store,assets=assets).create_release('project-1',created.revision,'v1','user-1')
    original = _package_manifest(release, {'idle':{}})
    assert 'sprite_layout' not in original and 'view_id' not in original['motions'][0]
    motions = tuple(MotionState(specification=MotionSpecification(
        motion_id=f'walk-{view.value.lower()}', name='Walk', preset='walk',semantic_slot='walk', view_id=view,
    ),candidates=base.motions[0].candidates,selected_candidate_id=base.motions[0].selected_candidate_id)
        for view in (ViewId.N,ViewId.S))
    views = tuple(CharacterView(view_id=view,name=view.value) for view in (ViewId.N,ViewId.S))
    manifest = base.model_copy(update={'sprite_layout':SpriteLayout.ISOMETRIC,'views':views,'motions':motions})
    package = _package_manifest(release.model_copy(update={'manifest':manifest}),{motion.motion_id:{} for motion in motions})
    assert package['sprite_layout'] == 'isometric'
    assert {motion['view_id'] for motion in package['motions']} == {'N','S'}
    assert {motion['sheet'] for motion in package['motions']} == {'motions/walk-n.png','motions/walk-s.png'}
    assert all(motion['facing'] == 'right' for motion in package['motions']) # independent manual flip flag, no heading mapping
    assert not any(finding.code == 'set_semantic_slot_duplicate' for finding in assess_set(motions).findings)
    duplicate = motions[1].model_copy(update={'specification':motions[1].specification.model_copy(update={'view_id':ViewId.N})})
    assert any(finding.code == 'set_semantic_slot_duplicate' for finding in assess_set((motions[0],duplicate)).findings)
    with pytest.raises(ValueError, match='does not support view-scoped'):
        await ReleaseExporter(assets).export(release.model_copy(update={'manifest':manifest}),profile=GAIA_V1)


async def test_calibration_only_accepts_same_view():
    service, _, _, project = await view_project((ViewId.N,ViewId.S))
    project = await service.add_motions('project-1',project.revision,tuple(MotionSpecification(
        motion_id=f'{action}-{view.value.lower()}',name=action,preset=action,view_id=view)
        for view in (ViewId.N,ViewId.S) for action in ('walk','attack')),'user-1')
    await service.require_same_view_calibration('project-1','attack-n','walk-n','user-1')
    with pytest.raises(ValueError,match='same view'):
        await service.require_same_view_calibration('project-1','attack-n','walk-s','user-1')


def test_rest_view_lifecycle_revision_fences_and_cross_actor():
    from .test_motion_references_routes import _studio_runtime, _client, _project_with_approved_subject
    runtime = _studio_runtime(); client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client,runtime)
    response = client.put(f'/studio/projects/{project_id}/sprite-layout',json={'expected_revision':revision,'sprite_layout':'isometric'})
    assert response.status_code == 200
    revision = response.json()['revision']
    assert response.json()['manifest']['sprite_layout'] == 'isometric'
    grant = client.post(f'/studio/projects/{project_id}/views/N/reference/uploads',json={'expected_revision':revision,'content_type':'image/png'})
    assert grant.status_code == 200
    key = grant.json()['key']; content = png(); runtime.assets_store.assets[key] = content
    body = {'expected_revision':revision,'name':'North','reference':{'key':key,'sha256':sha256(content).hexdigest()}}
    bound = client.put(f'/studio/projects/{project_id}/views/N',json=body)
    assert bound.status_code == 200
    assert bound.json()['revision'] == revision + 1
    assert bound.json()['manifest']['views'][0]['reference']['width'] == 80
    assert client.put(f'/studio/projects/{project_id}/views/N',json=body).status_code == 409
    revision = bound.json()['revision']
    assert client.post(f'/studio/projects/{project_id}/motions',json={
        'expected_revision':revision,'motion_id':'walk-e','name':'Walk','preset':'walk','view_id':'E',
    }).status_code == 422
    foreign = client.put(f'/studio/projects/{project_id}/views/N',json={'expected_revision':revision,'name':'x'},headers={'X-Studio-Actor-Id':'foreign'})
    assert foreign.status_code == 404
    draft = client.put(f'/studio/projects/{project_id}/views/N',json={'expected_revision':revision,'reference':None})
    assert draft.status_code == 200 and draft.json()['manifest']['views'][0]['reference'] is None
    assert client.put(f'/studio/projects/{project_id}/sprite-layout',json={'expected_revision':draft.json()['revision'],'sprite_layout':'side_view'}).status_code == 422


async def test_final_payload_allows_verified_frame_pair_without_fabricated_composition():
    service, _, _, project = await view_project()
    project = await service.add_motion('project-1',project.revision,MotionSpecification(motion_id='idle-n',name='Idle',preset='idle',view_id='N'),'user-1')
    job = await service.generate_motion('project-1',project.revision,'idle-n','user-1')
    payload = dict(job.payload)
    assert payload['composition_revision_id'] is None
    assert GenerateMotionPayloadV1.model_validate(payload).start_frame_key
    for field in ('start_frame_key','start_frame_sha256'):
        invalid = {**payload,field:None}
        with pytest.raises(ValidationError,match='present together'):
            GenerateMotionPayloadV1.model_validate(invalid)
    invalid = {**payload,'start_frame_key':None,'start_frame_sha256':None,'composition_revision_id':'fake'}
    with pytest.raises(ValidationError,match='requires a start frame'):
        GenerateMotionPayloadV1.model_validate(invalid)


async def test_reference_binding_cannot_overwrite_concurrent_revision(monkeypatch):
    import asyncio
    service, store, assets, project = await view_project()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = service._read_view_reference
    async def paused_read(*args):
        entered.set()
        await resume.wait()
        return await original(*args)
    monkeypatch.setattr(service,'_read_view_reference',paused_read)
    key = 'sources/studio/project-1/views/N/references/new.png'
    content = png((0,255,0,255)); await assets.write_once(key,content,'image/png')
    pending = asyncio.create_task(service.set_character_view('project-1',project.revision,ViewId.N,'user-1',
        reference=(key,sha256(content).hexdigest()),replace_reference=True))
    await entered.wait()
    renamed = await service.set_character_view('project-1',project.revision,ViewId.N,'user-1',name='Concurrent name')
    resume.set()
    with pytest.raises(RevisionConflict):
        await pending
    latest = await service.get_project('project-1','user-1')
    assert latest.revision == renamed.revision
    assert latest.manifest.views[0].name == 'Concurrent name'
    assert latest.manifest.views[0].reference == project.manifest.views[0].reference


def test_bound_view_preview_replacement_and_integrity_never_mint_for_unauthorized_keys():
    from .test_motion_references_routes import _studio_runtime, _client, _project_with_approved_subject
    runtime = _studio_runtime()
    client = _client(runtime)
    project_id, revision = _project_with_approved_subject(client, runtime)
    base = f'/studio/projects/{project_id}'
    configured = client.put(f'{base}/sprite-layout', json={
        'expected_revision': revision, 'sprite_layout': 'isometric',
    })
    assert configured.status_code == 200
    revision = configured.json()['revision']
    old_key = None
    for index, content in enumerate((png(), png((0, 255, 0, 255)))):
        upload = client.post(f'{base}/views/N/reference/uploads', json={
            'expected_revision': revision, 'content_type': 'image/png',
        })
        assert upload.status_code == 200
        key = upload.json()['key']
        runtime.assets_store.assets[key] = content
        before_mints = len(runtime.assets_store.minted_url_ttls)
        unbound = client.post(f'{base}/assets/preview', json={'asset_key': key})
        assert unbound.status_code == 422
        assert len(runtime.assets_store.minted_url_ttls) == before_mints
        bound = client.put(f'{base}/views/N', json={
            'expected_revision': revision,
            'reference': {'key': key, 'sha256': sha256(content).hexdigest()},
        })
        assert bound.status_code == 200
        revision = bound.json()['revision']
        preview = client.post(f'{base}/assets/preview', json={'asset_key': key})
        assert preview.status_code == 200, preview.text
        assert preview.json() == {'key': key, 'upload_url': project_file_path(project_id, key), 'upload_headers': {}}
        # Previews are owner-authorized API paths: no bearer URL is minted at all.
        assert len(runtime.assets_store.minted_url_ttls) == before_mints
        before_mints = len(runtime.assets_store.minted_url_ttls)
        foreign = client.post(f'{base}/assets/preview', json={'asset_key': key},
            headers={'X-Studio-Actor-Id': 'foreign'})
        assert foreign.status_code == 404
        assert len(runtime.assets_store.minted_url_ttls) == before_mints
        if old_key is not None:
            superseded = client.post(f'{base}/assets/preview', json={'asset_key': old_key})
            assert superseded.status_code == 422
            assert superseded.json()['detail']['code'] == 'asset_preview_not_allowed'
            assert len(runtime.assets_store.minted_url_ttls) == before_mints
        old_key = key
    runtime.assets_store.assets[key] = png((0, 0, 255, 255))
    tampered = client.post(f'{base}/assets/preview', json={'asset_key': key})
    assert tampered.status_code == 422
    assert tampered.json()['detail']['code'] == 'asset_preview_reference_invalid'
    assert len(runtime.assets_store.minted_url_ttls) == before_mints


@pytest.mark.parametrize('view_id,heading', [
    ('N', 'away from the viewer toward the top of the screen'),
    ('NE', 'away from the viewer and toward screen-right'),
    ('E', 'toward screen-right'),
    ('SE', 'toward the viewer and screen-right'),
    ('S', 'toward the viewer toward the bottom of the screen'),
    ('SW', 'toward the viewer and screen-left'),
    ('W', 'toward screen-left'),
    ('NW', 'away from the viewer and toward screen-left'),
    ('left', 'toward screen-left'),
    ('right', 'toward screen-right'),
])
def test_view_walk_prompt_declares_heading_without_changing_reference_projection(view_id, heading):
    from magic_video_editor.studio.models import Backdrop, ViewReference
    from magic_video_editor.studio.service import _motion_prompt
    reference = ViewReference(revision_id='ref-1', key=f'sources/studio/p/views/{view_id}/references/a.png',
                              sha256='a' * 64, width=80, height=100)
    spec = MotionSpecification(motion_id='walk-view', name='Walk', preset='walk', prompt='The character walks in place.',
                               view_id=view_id, view_reference=reference)
    prompt = _motion_prompt(spec, Backdrop.NATIVE_ALPHA)
    assert f'Declared screen-space heading {view_id}: {heading}.' in prompt
    assert 'Preserve its actual camera projection, facing angle and foreshortening' in prompt
    assert 'Walk forward in place along this projected heading with the root stationary' in prompt
    assert 'planted stance foot moving backward relative to the hips, opposite the implied projected forward travel' in prompt
    assert 'forward recovery of the other foot along the same projected heading' in prompt
    assert f"Maintain the supplied {view_id} reference's actual body plane and facing throughout." in prompt
    assert 'three-quarter' not in prompt and 'profile facing' not in prompt and 'rear/back view' not in prompt
    assert 'Natural sway and torso counterrotation retain the same view' in prompt
    assert 'Complete a full reciprocal stride' in prompt
    import re
    assert not re.search(r'\b(turn|rotate|mirror)\b', prompt, re.IGNORECASE)
    assert '45-degree' not in prompt and 'side visible' not in prompt
    assert 'character identity and all equipment' in prompt
    assert spec.view_reference == reference


def test_unassigned_legacy_motion_prompt_is_unchanged_by_declared_heading_constraints():
    from magic_video_editor.studio.models import Backdrop
    from magic_video_editor.studio.service import _motion_prompt
    authored = 'The character walks in place — a clean, seamless walk cycle.'
    spec = MotionSpecification(motion_id='walk', name='Walk', preset='walk', prompt=authored)
    assert _motion_prompt(spec, Backdrop.NATIVE_ALPHA) == authored


def test_view_attack_fill_contains_whole_canvas_once_and_walk_pixels_are_unchanged():
    from magic_video_editor.studio.models import MotionGenerationSettings
    from magic_video_editor.studio.service import _render_view_opening_frame
    from PIL import ImageOps, ImageChops
    content = png((255,0,0,255)) # full 80x100 authored canvas, not an alpha bbox
    settings = MotionGenerationSettings()
    original = Image.new('RGB',(512,512),(0,177,64))
    image = ImageOps.contain(Image.open(io.BytesIO(content)).convert('RGBA'),(512,512),Image.Resampling.LANCZOS)
    original.paste(image,((512-image.width)//2,(512-image.height)//2),image)
    walk_bytes = _render_view_opening_frame(content,settings)
    walk = Image.open(io.BytesIO(walk_bytes))
    assert ImageChops.difference(walk,original).getbbox() is None
    expected = io.BytesIO(); original.save(expected,format='PNG')
    assert walk_bytes == expected.getvalue()
    # A resolved provider parameter alone never shrinks a walk canvas.
    assert _render_view_opening_frame(content,settings.model_copy(update={
        'provider_parameters': {'framing_fill': .58},
    })) == walk_bytes
    attack = Image.open(io.BytesIO(_render_view_opening_frame(content,settings,fill=.58)))
    foreground = ImageChops.difference(attack,Image.new('RGB',attack.size,(0,177,64))).getbbox()
    x0,y0,x1,y1 = foreground
    assert y1-y0 == round(512*.58) # one fill, not .58 squared
    assert abs((x1-x0)/(y1-y0) - .8) < .01
    assert min(x0,y0,512-x1,512-y1) >= 100
    assert abs((x0+x1)/2-256) <= .5 and abs((y0+y1)/2-256) <= .5


async def test_view_attack_preparation_versions_new_jobs_and_preserves_immutable_retries():
    from magic_video_editor.studio.models import MotionGenerationSettings
    from .test_motion_pool import _Pool
    service, _, assets, project = await view_project()
    pool = _Pool(); service._motion_pool = pool
    project = await service.add_motion('project-1',project.revision,MotionSpecification(
        motion_id='attack-n',name='Attack',preset='attack',view_id='N'),'user-1')
    job = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='new')
    original = project.manifest.views[0].reference
    assert job.payload['view_reference']['key'] == original.key
    assert job.payload['view_reference']['sha256'] == original.sha256
    evidence = job.payload['reference_preparation']
    assert evidence['algorithm'] == 'full_canvas_contain_fill_v2' and evidence['fill'] == .58
    assert job.payload['start_frame_key'].endswith(job.payload['start_frame_sha256']+'.png')
    project = await service.get_project('project-1', 'user-1')
    repeated = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='new')
    assert repeated == job and len(pool.submissions) == 1
    unchanged = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='new',
        settings=MotionGenerationSettings(provider_parameters={'framing_fill':.7}))
    assert unchanged == job and len(pool.submissions) == 1
    # Simulate a genuine pre-fix immutable operation under the old unsalted ID.
    from dataclasses import replace
    from datetime import timedelta
    from magic_video_editor.contracts import JobStatus, VideoJobResult
    from magic_video_editor.studio.service import _render_view_opening_frame
    legacy_frame = _render_view_opening_frame(assets.assets[original.key], MotionGenerationSettings())
    legacy_hash = sha256(legacy_frame).hexdigest()
    legacy_key = f'outputs/studio/project-1/opening-frames/view-{legacy_hash}.png'
    await assets.write_once(legacy_key, legacy_frame, 'image/png')
    old = replace(job, status=JobStatus.COMPLETED, result=VideoJobResult(output_key=job.output_key),
        payload={**job.payload, 'start_frame_key': legacy_key, 'start_frame_sha256': legacy_hash,
                 'reference_preparation': {'algorithm': 'full_canvas_contain_v1'}})
    service._jobs.records[job.job_id] = old
    # The immutable request can be older than the store's newest 100 rows.
    for index in range(101):
        service._jobs.records[f'unrelated-{index}'] = replace(old, job_id=f'unrelated-{index}',
            idempotency_key=f'unrelated-{index}', status=JobStatus.FAILED, result=None,
            created_at=old.created_at + timedelta(seconds=index + 1))
    retried = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='new')
    assert retried == old and len(pool.submissions) == 1
    assert retried.payload['reference_preparation'] == {'algorithm': 'full_canvas_contain_v1'}
    assert retried.payload['start_frame_sha256'] == legacy_hash != job.payload['start_frame_sha256']
    fresh = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='fresh')
    assert fresh.manifest_operation_id != old.manifest_operation_id
    assert fresh.payload['start_frame_key'] == job.payload['start_frame_key']
    project = await service.get_project('project-1', 'user-1')
    explicit = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='explicit',
        settings=MotionGenerationSettings(provider_parameters={'framing_fill':.7}))
    assert explicit.payload['reference_preparation']['fill'] == .7
    assert explicit.payload['start_frame_key'] != fresh.payload['start_frame_key']


async def test_immutable_attack_retry_survives_unavailable_original_machine():
    from .test_motion_pool import _Pool
    service, _, _, project = await view_project()
    pool = _Pool(); service._motion_pool = pool
    project = await service.add_motion('project-1',project.revision,MotionSpecification(
        motion_id='attack-n',name='Attack',preset='attack',view_id='N'),'user-1')
    class MachineDefaults:
        unavailable = False
        calls = 0
        async def resolve(self, owner_id, settings):
            self.calls += 1
            if self.unavailable:
                raise RuntimeError('original worker is offline or configuration is stale')
            return settings
    resolver = MachineDefaults()
    service.machine_defaults = resolver
    original = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='original')
    assert len(pool.submissions) == 1 and resolver.calls == 1
    resolver.unavailable = True
    project = await service.get_project('project-1', 'user-1')
    retried = await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='original')
    assert retried == original
    assert retried.payload == original.payload
    assert len(pool.submissions) == 1 and resolver.calls == 1
    with pytest.raises(RuntimeError,match='offline or configuration is stale'):
        await service.generate_motion('project-1',project.revision,'attack-n','user-1',request_id='fresh')
    assert len(pool.submissions) == 1 and resolver.calls == 2


async def test_mixed_size_registered_calibration_cache_save_and_export():
    from .test_frame_analysis import calibration_pair
    from magic_video_editor.studio.frame_analysis import calibrate_body, ANALYSIS_VERSION
    from magic_video_editor.studio.models import MotionCandidate, FrameSlot, LoopPolicy, NormalizationSpec
    from magic_video_editor.studio.quality import inspect_sprite_png
    from magic_video_editor.studio.exports import register_sheet_pivot, resolve_registered_pivots
    from magic_video_editor.studio.service import MotionDerivationError, ProjectNotFound
    import zipfile
    service, store, assets, project = await view_project()
    (source_bytes,_),(reference_bytes,_) = calibration_pair()
    reference_bytes = register_sheet_pivot(reference_bytes,128,224)
    project = await service.add_motions('project-1',project.revision,tuple(MotionSpecification(
        motion_id=action+'-n',name=action,preset=action,view_id='N') for action in ('attack','walk')),'user-1')
    candidates = []
    for label,content in (('attack',source_bytes),('walk',reference_bytes)):
        key=f'outputs/studio/project-1/{label}.png'; await assets.write_once(key,content,'image/png')
        candidates.append(MotionCandidate(candidate_id=label,artifact_key=key,sha256=sha256(content).hexdigest(),
            duration_ms=100,metadata=inspect_sprite_png(content)))
    project = await store.append('project-1',project.revision,project.manifest.model_copy(update={
        'motions':tuple(motion.model_copy(update={'candidates':(candidate,),'selected_candidate_id':candidate.candidate_id})
            for motion,candidate in zip(project.manifest.motions,candidates))}))
    source = await service.analyze_candidate_frames('project-1','attack-n','attack','user-1')
    reference = await service.analyze_candidate_frames('project-1','walk-n','walk','user-1')
    assert reference['registered_pivot'] == {'x':128,'y':224}
    cache_key=f'outputs/studio/project-1/motions/walk-n/walk/frames-{ANALYSIS_VERSION}-{candidates[1].sha256[:16]}.json'
    cached=json.loads(assets.assets[cache_key])
    assert 'registered_pivot' not in cached
    # A pre-existing pixel cache receives current registration, never stale embedded metadata.
    cached['registered_pivot']={'x':0,'y':0}
    assets.assets[cache_key]=json.dumps(cached).encode()
    again=await service.analyze_candidate_frames('project-1','walk-n','walk','user-1')
    assert again['registered_pivot']=={'x':128,'y':224}
    with pytest.raises(ProjectNotFound):
        await service.analyze_candidate_frames('project-1','walk-n','walk','foreign')
    calibration=calibrate_body(source,again,0)
    project=await service.create_motion_revision('project-1',project.revision,'attack-n','attack','user-1',
        ordered_frames=(FrameSlot(index=0),),frame_duration_ms=100,loop_policy=LoopPolicy.LOOP,
        target_frame_count=8,cell_size=256,normalization=NormalizationSpec(**calibration['normalization']))
    attack=project.manifest.motions[0].selected_candidate
    expansion=(attack.metadata.height-256)//2
    assert expansion>0 and (attack.metadata.pivot_x,attack.metadata.pivot_y)==(128+expansion,224+expansion)
    assert resolve_registered_pivots(project.manifest.motions)['walk']==(128,224)
    project=await service.assess_set_quality('project-1',project.revision,'user-1')
    project=await service.approve_set('project-1',project.revision,'user-1')
    release=await service.create_release('project-1',project.revision,'calibrated','user-1')
    export=await service.create_export(release.release_id,'png-json','user-1')
    with zipfile.ZipFile(io.BytesIO(assets.assets[export.artifact_key])) as archive:
        package=json.loads(archive.read('manifest.json'))
        geometry={m['motion_id']:m['frame_geometry'] for m in package['motions']}
        assert (geometry['walk-n']['pivot_x'],geometry['walk-n']['pivot_y'])==(128,224)
        assert (geometry['attack-n']['pivot_x'],geometry['attack-n']['pivot_y'])==(128+expansion,224+expansion)
    # Cache identity remains bound to the authoritative candidate; never accept forged hashes.
    cached['source_sha256']='c'*64; assets.assets[cache_key]=json.dumps(cached).encode()
    with pytest.raises(MotionDerivationError,match='identity and hash'):
        await service.analyze_candidate_frames('project-1','walk-n','walk','user-1')
    changed_reference=project.manifest.motions[1].model_copy(update={'selected_candidate_id':None})
    with pytest.raises(ValueError,match='exact selected'):
        resolve_registered_pivots((project.manifest.motions[0],changed_reference))


@pytest.mark.parametrize('view_id', ['N','NE','E','SE','S','SW','W','NW','left','right'])
def test_view_attack_binds_strike_to_projected_facing_and_heading(view_id):
    from magic_video_editor.studio.models import Backdrop, ViewReference
    from magic_video_editor.studio.service import _motion_prompt
    reference = ViewReference(revision_id='reference', key=f'sources/p/views/{view_id}/ref.png',
                              sha256='a'*64, width=512, height=512)
    spec = MotionSpecification(motion_id='attack-view', name='Attack', preset='attack',
        prompt='The character swings each axe.', view_id=view_id, view_reference=reference)
    prompt = _motion_prompt(spec, Backdrop.NATIVE_ALPHA)
    assert f'Declared screen-space heading {view_id}:' in prompt
    assert "Direct the strike forward along the supplied reference's projected facing and declared screen-space heading." in prompt
    assert "feet and pelvis's overall facing, allowing natural weight transfer" in prompt
    assert 'shoulder/arm counterrotation' in prompt
    assert spec.view_reference == reference
