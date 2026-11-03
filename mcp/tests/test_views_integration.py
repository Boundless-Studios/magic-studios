"""MCP helpers through the actual Studio ASGI routes and memory persistence.

Run with root backend dependencies and PYTHONPATH=mcp/src:src. The standalone
MCP environment skips this module when the optional backend is unavailable.
The memory signed-storage transport supplies storage only, never API responses.
"""
from __future__ import annotations

import base64
import io
from hashlib import sha256

import httpx
import pytest

pytest.importorskip('magic_video_editor.app')
from PIL import Image
from magic_video_editor.app import create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher
from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioApiError, StudioClient


def image_bytes(color):
    output = io.BytesIO()
    Image.new('RGBA', (1024, 1024), color).save(output, format='PNG')
    return output.getvalue()


class StudioTransport(httpx.AsyncBaseTransport):
    def __init__(self, runtime):
        self.runtime = runtime
        self.api = httpx.ASGITransport(app=create_app(runtime))
        self.signed_requests = []

    async def handle_async_request(self, request):
        if request.url.scheme == 'memory' and request.url.host == 'upload':
            self.signed_requests.append(request)
            key = request.url.path.lstrip('/')
            await self.runtime.assets_store.write_once(key, await request.aread(), request.headers['content-type'])
            return httpx.Response(200)
        # Model the trusted actor gateway rather than teaching MCP to assert identity.
        request.headers['X-Studio-Actor-Id'] = 'owner-1'
        return await self.api.handle_async_request(request)

    async def aclose(self):
        await self.api.aclose()


async def test_mcp_view_reference_motion_resume_and_replacement_asgi():
    assets, jobs, store = MemoryAssetStore(), MemoryJobStore(), MemoryStudioStore()
    service = StudioService(store=store, assets=assets, jobs=jobs, dispatcher=NullTaskDispatcher(),
                            motion_generation_available=True)
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    transport = StudioTransport(runtime)
    client = StudioClient(base_url='http://studio.test', transport=transport)
    try:
        project = await tools.create_project(client, 'Two axe character')
        project_id = project['project_id']
        base = f'/studio/projects/{project_id}'
        subject = image_bytes((160, 40, 40, 255))
        await tools.upload_subject_base64(client, project_id, base64.b64encode(subject).decode())
        await tools.approve_subject(client, project_id)
        await tools.configure_sprite_layout(client, project_id, 'isometric')
        await tools.set_character_view(client, project_id, 'N', name='Supplied North')
        added = await tools.add_view_motions(client, project_id, 'N', [
            dict(motion_id='n-walk',name='Walk',preset='walk'),
            dict(motion_id='n-attack',name='Attack',preset='attack',action='swings both axes'),
        ])
        assert added['added_motion_ids'] == ['n-walk','n-attack']
        missing = await tools.generate_motions(client, project_id, ['n-walk'], request_id='missing-ref')
        assert not missing['queued'] and missing['failed']
        assert not jobs.records
        reference = image_bytes((40, 160, 40, 255))
        uploaded = await tools.upload_view_reference_base64(client, project_id, 'N', base64.b64encode(reference).decode())
        first_reference = uploaded['views'][0]['reference']
        assert first_reference['sha256'] == sha256(reference).hexdigest()
        assert (first_reference['width'], first_reference['height']) == (1024,1024)
        await tools.update_view_motion(client, project_id, 'n-walk', {'script':'Keep one axe per hand', 'style':'grimdark'})
        repeated = await tools.add_view_motions(client, project_id, 'N', [dict(motion_id='n-walk',name='Walk',preset='walk')])
        assert repeated['skipped_motion_ids'] == ['n-walk']
        queued = await tools.generate_motions(client, project_id, ['n-walk','n-attack'], request_id='durable-batch')
        assert not queued['failed'] and len(queued['queued']) == 2
        assert queued['request_id'] == 'durable-batch'
        walk = queued['queued'][0]
        evidence = walk['generation_evidence']
        assert evidence['view_reference'] == first_reference
        assert evidence['start_frame_sha256'] == sha256(assets.assets[evidence['start_frame_key']]).hexdigest()
        assert 'Keep one axe per hand' in walk['prompt']
        assert 'swings both axes' in queued['queued'][1]['prompt']
        assert walk['generation_settings']
        job_count = len(jobs.records)
        resumed = await tools.generate_motions(client, project_id, ['n-walk','n-attack'], request_id='durable-batch')
        assert len(jobs.records) == job_count
        assert [q['job_id'] for q in resumed['queued']] == [q['job_id'] for q in queued['queued']]
        takes = await tools.list_takes(client, project_id, 'n-walk')
        old_spec = takes['spec_digest']
        assert takes['view_id'] == 'N' and takes['view_reference'] == first_reference
        changed = image_bytes((40, 40, 160, 255))
        replaced = await tools.upload_view_reference_base64(client, project_id, 'N', base64.b64encode(changed).decode())
        assert replaced['views'][0]['reference']['revision_id'] != first_reference['revision_id']
        takes = await tools.list_takes(client, project_id, 'n-walk')
        assert takes['spec_digest'] != old_spec
        assert takes['view_reference']['sha256'] == sha256(changed).hexdigest()
        status = await tools.wait_for_operations(client, project_id, timeout_s=0)
        old_walk = next(op for op in status['recent'] if op['motion_id'] == 'n-walk')
        assert old_walk['generation_evidence']['view_reference'] == first_reference
        assert old_walk['generation_evidence']['motion_spec_digest'] == old_spec
        assert all('Authorization' not in r.headers for r in transport.signed_requests)
        # Route validation still fences hashes/namespace and does not trust MCP input.
        with pytest.raises(StudioApiError) as error:
            await tools.set_character_view(client, project_id, 'N', reference={'key':'foreign/image.png','sha256':'a'*64})
        assert error.value.status_code == 422
        current = await client.get(base)
        assert current['manifest']['views'][0]['reference']['sha256'] == sha256(changed).hexdigest()
        with pytest.raises(ValueError, match='explicit'):
            await tools.calibrate_motion_body(client, project_id, 'n-attack', 'not-ready', 0, 'not-ready')
    finally:
        await client.aclose()


async def test_idle_proposal_asgi_keeps_source_timing_and_rejects_frozen_source():
    """Seed finalized source artifacts; exercise all proposal reads through real HTTP."""
    import math
    from PIL import ImageDraw
    from magic_video_editor.studio.models import MotionCandidate, MotionSpecification

    assets, jobs, store = MemoryAssetStore(), MemoryJobStore(), MemoryStudioStore()
    service = StudioService(store=store, assets=assets, jobs=jobs, dispatcher=NullTaskDispatcher())
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    transport = StudioTransport(runtime)
    client = StudioClient(base_url='http://studio.test', transport=transport)
    try:
        created = await tools.create_project(client, 'Idle source fixture')
        project_id = created['project_id']
        project = await service.add_motion(project_id, created['revision'],
            MotionSpecification(motion_id='idle',name='Idle',preset='idle'), 'owner-1')
        candidates = []
        for frozen in (False, True):
            sheet = Image.new('RGBA', (64*73,64))
            for index in range(73):
                tile = Image.new('RGBA',(64,64))
                draw = ImageDraw.Draw(tile)
                # Subtle pixel/brightness changes provide a measured repeatable source
                # signal; this fixture does not claim perceptually accepted breathing.
                phase = 0 if frozen else math.sin(index*2*math.pi/72)
                shade = 120 + round(6*phase)
                draw.rectangle((16,8,47,55), fill=(shade,shade,shade,255))
                sheet.paste(tile,(index*64,0))
            output = io.BytesIO(); sheet.save(output,format='PNG'); content = output.getvalue()
            candidate_id = 'frozen' if frozen else 'subtle'
            key = f'outputs/studio/{project_id}/{candidate_id}.png'
            await assets.write_once(key,content,'image/png')
            candidates.append(MotionCandidate(candidate_id=candidate_id,artifact_key=key,sha256=sha256(content).hexdigest(),
                duration_ms=3042, metadata=dict(decodable=True, media_kind='sheet',frame_count=73,width=64*73,height=64,
                    constant_geometry=True,has_alpha=True,packing_estimate=dict(width=64*73,height=64),
                    frame_rate='24/1',time_base='1/24')))
        source_motion = project.manifest.motions[0].model_copy(update={'candidates':tuple(candidates),'selected_candidate_id':'subtle'})
        project = await store.append(project_id,project.revision,project.manifest.model_copy(update={'motions':(source_motion,)}))
        proposal = await tools.suggest_motion_loop(client,project_id,'idle','subtle','idle')
        assert proposal['mode'] == 'idle'
        assert proposal['status'] == 'complete', proposal
        assert len(proposal['ordered_frames']) == 12
        assert proposal['source_frame_rate'] == '24/1'
        assert proposal['source_time_base'] == '1/24'
        assert proposal['source_interval_ms'] > 0
        frozen = await tools.suggest_motion_loop(client,project_id,'idle','frozen','idle')
        assert frozen['status'] == 'incomplete' and not frozen['ordered_frames']
        current = await client.get(f'/studio/projects/{project_id}')
        assert current['revision'] == project.revision
        assert current['manifest']['motions'][0]['selected_candidate_id'] == 'subtle'
        assert len(current['manifest']['motions'][0]['candidates']) == 2
    finally:
        await client.aclose()


async def test_mcp_atomic_setup_stages_one_image_and_preserves_partial_matrix():
    assets, jobs = MemoryAssetStore(), MemoryJobStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets, jobs=jobs)
    runtime = InMemoryRuntime(jobs, assets, studio_service=service)
    client = StudioClient(base_url='http://studio.test', transport=StudioTransport(runtime))
    try:
        project = await tools.create_project(client, 'One image')
        project_id = project['project_id']
        assert (await tools.get_setup_capabilities(client))['reference_staging']
        reference = await tools.upload_setup_reference_base64(client, project_id,
            base64.b64encode(image_bytes((160, 40, 40, 255))).decode())
        desired = {'schema_version': 1, 'sprite_layout': 'single_view',
            'identity_reference': {'revision_id': reference['revision_id']},
            'views': [{'view_id': 'single', 'heading': None, 'name': 'Original',
                'reference': {'revision_id': reference['revision_id']}, 'motions': [
                    {'motion_id': 'idle', 'name': 'Idle', 'preset': 'idle'},
                    {'motion_id': 'death', 'name': 'Death', 'preset': 'death'}]}]}
        plan = await tools.preview_setup(client, project_id, 1, desired)
        assert (await tools.get_project(client, project_id))['revision'] == 1
        applied = await tools.apply_setup(client, project_id, 1, plan['plan_token'],
            [impact['impact_id'] for impact in plan['impacts'] if impact['requires_confirmation']])
        assert applied['active_operations'] == []
        current = await tools.get_setup(client, project_id)
        assert [motion['motion_id'] for motion in current['setup']['views'][0]['motions']] == ['idle', 'death']
        assert current['setup']['identity_reference']['revision_id'] == reference['revision_id']
        with pytest.raises(StudioApiError) as stale:
            await tools.apply_setup(client, project_id, 1, plan['plan_token'])
        assert stale.value.status_code == 409
    finally:
        await client.aclose()
