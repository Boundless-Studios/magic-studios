import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from outbound_gpu_worker_pool.contracts import JobSubmission

from magic_video_editor import desktop_studio as desktop
from magic_video_editor.local_studio import LocalAssetStore
from magic_video_editor.movies.service import MovieService


def test_installed_package_finds_checkout_from_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv('MAGIC_DESKTOP_ROOT', raising=False)
    monkeypatch.setattr(desktop, '__file__', str(tmp_path / 'site-packages/magic_video_editor/desktop_studio.py'))
    checkout = tmp_path / 'checkout'
    (checkout / 'local/presets').mkdir(parents=True)
    monkeypatch.chdir(checkout)
    assert desktop.desktop_root() == checkout


def test_explicit_desktop_root_overrides_install_and_working_directory(tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    monkeypatch.setenv('MAGIC_DESKTOP_ROOT', str(checkout))
    monkeypatch.chdir(tmp_path)
    assert desktop.desktop_root() == checkout


def test_missing_desktop_templates_explain_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, 'ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='Set MAGIC_DESKTOP_ROOT'):
        desktop.DesktopPool(LocalAssetStore())


@pytest.fixture(autouse=True)
def local_data(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, 'DATA', tmp_path)


async def test_movie_and_media_survive_new_runtime():
    assets = LocalAssetStore()
    assets.assets = desktop.DiskMap('assets', binary=True)
    assets.content_types = desktop.DiskMap('content_types')
    await assets.write_once('outputs/test.png', b'original', 'image/png')
    assert not await assets.write_once('outputs/test.png', b'changed', 'image/png')
    store = desktop.DiskMovieStore()
    movie = await MovieService(store, assets).create('owner', 'Saved movie')
    await store.account_asset(movie.movie_id, 'outputs/test.png', 8)
    reloaded = desktop.DiskMovieStore()
    assert (await reloaded.get(movie.movie_id)).name == movie.name
    assert await reloaded.stored_bytes('owner') == 8
    assert desktop.DiskMap('assets', binary=True)['outputs/test.png'] == b'original'


async def test_animation_manifest_and_operation_survive_restart():
    from magic_video_editor.studio.manifest import create_project_manifest
    from magic_video_editor.contracts import JobSubmission as StudioSubmission, JobOperation, VideoJobResult
    store = desktop.DiskStudioStore()
    manifest = create_project_manifest(project_id='orc', name='Orc', owner_id='owner')
    created = await store.create(manifest)
    await store.append('orc', created.revision, manifest.model_copy(update={'name': 'Saved Orc'}))
    restarted = desktop.DiskStudioStore()
    assert (await restarted.get('orc')).manifest.name == 'Saved Orc'
    assert len(await restarted.list_revisions('orc')) == 2
    jobs = desktop.DiskJobStore()
    await jobs.submit(StudioSubmission('test-job', 'unique', JobOperation.PACKAGE, 'source', 'output', {}))
    await jobs.complete_unclaimed('test-job', VideoJobResult(output_key='output'))
    assert (await desktop.DiskJobStore().get('test-job')).status == desktop.JobStatus.COMPLETED


async def test_animation_uses_real_pool_and_disables_simulated_generators(monkeypatch):
    runtime = desktop.create_local_studio_runtime()
    monkeypatch.setattr(desktop, 'create_local_studio_runtime', lambda: runtime)
    desktop.create_desktop_app()
    pool = runtime.movie_service.pool
    assert runtime.studio_service._motion_pool is pool
    assert runtime.studio_service._motion_generation_available is True
    assert isinstance(runtime.subject_generator, desktop.PoolSubjectGenerator)
    assert isinstance(runtime.studio_service._store, desktop.DiskStudioStore)
    assert isinstance(runtime.gpu, desktop.UnavailableLocalGenerator)
    pool.jobs['done'] = {'status': 'completed', 'output_key': 'output'}
    assert (await pool.get('done')).status is desktop.PoolJobStatus.COMPLETED
    with pytest.raises(RuntimeError, match='configured ComfyUI pool'):
        await desktop.UnavailableLocalGenerator().generate(None)
    await pool.plugin._client.aclose()


async def test_single_gpu_queue_idempotence_and_restart(monkeypatch):
    assets = LocalAssetStore()
    pool = desktop.DesktopPool(assets)
    active = 0
    peak = 0
    calls = 0

    async def execute(context, request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        path = context.workspace / 'result.mp4'
        path.write_bytes(b'video')
        return SimpleNamespace(path=path, content_type='video/mp4')

    monkeypatch.setattr(pool.plugin, 'execute', execute)
    request = JobSubmission('job-a', 'key-a', 'video.minimax_h3.text_to_video.v1', (), 'outputs/a.mp4', payload={'prompt': 'A tree moves in the breeze.'})
    await pool.submit(request)
    await pool.submit(request)
    await pool.submit(replace(request, job_id='job-b', idempotency_key='key-b', output_key='outputs/b.mp4'))
    await asyncio.gather(*list(pool.tasks.values()))
    assert peak == 1 and calls == 2
    completed_record = await pool.get('job-a')
    assert completed_record.status == 'completed'
    assert completed_record.capability_id == request.capability_id
    assert completed_record.requirements == request.requirements
    assert completed_record.executed_by is None
    assert completed_record.execution_metadata == {}
    assert completed_record.output_sha256 == hashlib.sha256(b'video').hexdigest()
    with pytest.raises(ValueError):
        await pool.submit(replace(request, payload={'prompt': 'Changed'}))
    pool.jobs['interrupted'] = {'status': 'processing'}
    restarted = desktop.DesktopPool(assets)
    assert (await restarted.get('job-a')).status == 'completed'
    assert (await restarted.get('interrupted')).status == 'failed'
    await pool.plugin._client.aclose()
    await restarted.plugin._client.aclose()


async def test_desktop_worker_status_reports_the_real_gpu(monkeypatch):
    from datetime import UTC, datetime
    import httpx

    async def status(self):
        return SimpleNamespace(worker_healthy=True, sampled_at=datetime.now(UTC),
            device_name='NVIDIA GeForce RTX 4080', utilization_percent=50,
            vram_used_mb=12000, vram_total_mb=16376)

    monkeypatch.setattr(desktop.DesktopTelemetry, 'status', status)
    app = desktop.create_desktop_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        response = await client.get('/pool/workers')
    assert response.status_code == 200
    worker = response.json()['workers'][0]
    assert worker['status'] == 'online'
    assert worker['gpus'][0]['memory_total_mb'] == 16376
    assert 'video.minimax_h3.text_to_video.v1' in worker['capabilities']


async def test_turbo_uses_eight_steps_without_changing_the_submission(monkeypatch):
    monkeypatch.setenv('MAGIC_DESKTOP_PRESET', 'turbo')
    pool = desktop.DesktopPool(LocalAssetStore())
    seen = []

    async def execute(context, request):
        seen.append(request.inputs['steps'])
        path = context.workspace / 'result.mp4'
        path.write_bytes(b'video')
        return SimpleNamespace(path=path, content_type='video/mp4')

    monkeypatch.setattr(pool.plugin, 'execute', execute)
    request = JobSubmission('turbo-a', 'turbo-a', 'video.minimax_h3.text_to_video.v1', (), 'outputs/turbo.mp4', payload={'prompt': 'A boat sails.', 'steps': 20})
    await pool.submit(request)
    await asyncio.gather(*list(pool.tasks.values()))
    assert seen == [8]
    assert request.payload['steps'] == 20
    assert (await pool.get('turbo-a')).preset == 'turbo'
    await pool.plugin._client.aclose()


def test_balanced_defaults_are_bounded_and_preserve_explicit_base_recipe():
    from magic_video_editor.studio.models import MotionGenerationSettings
    from magic_video_editor.studio.service import _motion_pool_profile, MOTION_LIGHTX8_CAPABILITY_ID
    settings = desktop.balanced_motion_settings(MotionGenerationSettings())
    assert settings.steps == 8
    assert _motion_pool_profile(settings)[0] == MOTION_LIGHTX8_CAPABILITY_ID
    base = MotionGenerationSettings(steps=12, provider_parameters={'execution_profile': 'h3-w4a8-16gb'})
    assert desktop.balanced_motion_settings(base) == base
    with pytest.raises(ValueError, match='verified 16 GB profile'):
        desktop.balanced_motion_settings(MotionGenerationSettings(width=1024))


async def test_balanced_registry_and_discovery_use_real_winning_recipe(monkeypatch):
    import httpx
    monkeypatch.setenv('MAGIC_DESKTOP_PRESET', 'balanced')
    monkeypatch.setenv('MAGIC_DESKTOP_ATTENTION', 'ck')
    monkeypatch.setenv('MAGIC_DESKTOP_GLOBAL_ATTENTION', 'sage')
    app = desktop.create_desktop_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        config = (await client.get('/studio/generation-config')).json()
    assert config['default_settings']['steps'] == 8
    assert config['global_attention'] == 'sage'
    pool = desktop.DesktopPool(LocalAssetStore())
    graph = pool.plugin._registry.template('video.minimax_h3.text_to_video.w4a8_16gb.lightx8.v1').graph
    assert graph['300']['inputs']['lora_name'] == 'minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors'
    assert graph['17']['inputs']['sampler_name'] == 'res_multistep'
    assert graph['9']['inputs']['scheduler'] == 'simple'
    assert graph['301']['inputs']['attention'] == 'comfy kitchen attention'
    base = pool.plugin._registry.template('video.minimax_h3.text_to_video.w4a8_16gb.v1').graph
    assert not any(n['class_type'] == 'LoraLoaderModelOnly' for n in base.values())
    await pool.plugin._client.aclose()


def test_importing_desktop_helpers_does_not_open_or_recover_job_databases(tmp_path):
    import os
    import subprocess
    import sys
    runtime_dir = tmp_path / 'unstarted-runtime'
    subprocess.run([sys.executable, '-c', 'import magic_video_editor.desktop_studio'],
                   env={**os.environ, 'MAGIC_DESKTOP_DATA': str(runtime_dir)}, check=True)
    assert list(runtime_dir.glob('*.sqlite3')) == []


@pytest.mark.parametrize('outcome,expected', [
    ('completed', desktop.JobStatus.COMPLETED),
    ('failed', desktop.JobStatus.FAILED),
    ('cancelled', desktop.JobStatus.CANCELLED),
    ('processing', desktop.JobStatus.QUEUED),
])
async def test_desktop_pool_outcomes_sync_through_studio(outcome, expected):
    from magic_video_editor.studio.service import StudioService
    from magic_video_editor.studio.memory import MemoryStudioStore

    assets = LocalAssetStore()
    pool = desktop.DesktopPool(assets)
    jobs = desktop.DiskJobStore()
    submitted = await jobs.submit(_desktop_motion_submission('sync-job'))
    pool.jobs['sync-job'] = {
        'status': outcome, 'output_key': submitted.record.output_key, 'output_sha256': 'a' * 64,
        'failure_message': 'GPU stopped', 'retryable': True,
    }
    # Old persisted desktop rows lack all of the hosted worker fields.
    record = await pool.get('sync-job')
    assert record.completion_path == 'desktop_local'
    assert record.executed_by is None
    assert record.execution_metadata == {}
    assert record.capability_id is None
    assert record.requirements.runtime_versions == {}
    service = StudioService(store=MemoryStudioStore(), assets=assets, jobs=jobs,
                            motion_pool=pool)
    await service._sync_pool_motion_records([await jobs.get('sync-job')])
    synced = await jobs.get('sync-job')
    assert synced.status is expected
    if outcome == 'completed':
        assert synced.result.output_key == submitted.record.output_key
        assert synced.result.execution_provenance['executing_worker_id'] is None
        assert synced.result.execution_provenance['worker_attestation'] == {}
        assert synced.result.execution_provenance['runtime_versions'] == {}
        assert synced.result.execution_provenance['output_sha256'] == 'a' * 64
    elif outcome == 'failed':
        assert synced.failure_message == 'GPU stopped'
        assert synced.retryable
    await pool.plugin._client.aclose()


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'cancelled'])
async def test_restart_retains_terminal_gpu_outcome_for_studio_sync(outcome):
    jobs = desktop.DiskJobStore()
    await jobs.submit(_desktop_motion_submission('recover'))
    desktop.DiskMap('gpu_jobs')['recover'] = {
        'status': outcome, 'output_key': _desktop_motion_submission('recover').output_key}
    restarted_jobs = desktop.DiskJobStore()
    assert (await restarted_jobs.get('recover')).status is desktop.JobStatus.QUEUED
    pool = desktop.DesktopPool(LocalAssetStore())
    assert (await pool.get('recover')).status.value == outcome
    assert pool.tasks == {}
    await pool.plugin._client.aclose()


def _desktop_motion_submission(job_id):
    from magic_video_editor.contracts import JobOperation, JobSubmission
    source = 'sources/studio/project/subjects/source.png'
    return JobSubmission(
        job_id, job_id, JobOperation.GENERATE_MOTION, source,
        'outputs/studio/project/motions/idle/candidate.mp4', {},
        project_id='project', manifest_operation_id='candidate',
        payload={'subject_key': source, 'subject_artifact_key': source,
                 'motion_id': 'idle', 'steps': 20, 'duration_seconds': 4},
    )


async def test_completed_desktop_motion_settles_existing_candidate_after_restart():
    import io
    from PIL import Image
    from magic_video_editor.studio.manifest import create_project_manifest
    from magic_video_editor.studio.models import MotionSpecification, MotionKind, MotionPreset
    from magic_video_editor.studio.service import StudioService

    assets = LocalAssetStore()
    store = desktop.DiskStudioStore()
    await store.create(create_project_manifest('project', 'Saved', owner_id='owner'))
    jobs = desktop.DiskJobStore()
    service = StudioService(store=store, assets=assets, jobs=jobs)
    project = await service.add_motion('project', 1, MotionSpecification(
        motion_id='idle', name='Idle', kind=MotionKind.PRESET, preset=MotionPreset.IDLE,
        loop_policy='loop', required=True), 'owner')
    submission = _desktop_motion_submission('settle')
    submission = replace(submission, payload={**submission.payload,
                                            'requested_revision': project.revision})
    await jobs.submit(submission)
    image = io.BytesIO()
    Image.new('RGB', (64, 64), 'green').save(image, format='PNG')
    await assets.write_once(submission.output_key, image.getvalue(), 'image/png')
    desktop.DiskMap('gpu_jobs')['settle'] = {
        'status': 'completed', 'output_key': submission.output_key,
    }
    recovered_jobs = desktop.DiskJobStore()
    pool = desktop.DesktopPool(assets)
    service._jobs = recovered_jobs
    service._motion_pool = pool
    await service._sync_pool_motion_records([await recovered_jobs.get('settle')])
    settled = await service.settle_motion_candidate('settle', 'owner')
    candidates = settled.manifest.motions[0].candidates
    assert sum(c.candidate_id == 'candidate' for c in candidates) == 1
    assert candidates[0].artifact_key == submission.output_key
    assert candidates[0].execution_provenance['executing_worker_id'] is None
    again = await service.settle_motion_candidate('settle', 'owner')
    assert again.revision == settled.revision
    assert pool.tasks == {}
    await pool.plugin._client.aclose()


@pytest.mark.parametrize('pool_row', [
    {'status': 'completed'},
    {'status': 'completed', 'output_key': 'different-output'},
    {'status': 'processing'},
    {'status': 'completed', 'output_key': 'outputs/studio/project/motions/idle/candidate.mp4', 'requirements': None},
    {'status': 'completed', 'output_key': 'outputs/studio/project/motions/idle/candidate.mp4', 'requirements': {'runtime_versions': ['invalid']}},
    'malformed',
])
async def test_restart_rejects_unlinked_or_unusable_gpu_outcomes(pool_row):
    jobs = desktop.DiskJobStore()
    await jobs.submit(_desktop_motion_submission('invalid-recovery'))
    desktop.DiskMap('gpu_jobs')['invalid-recovery'] = pool_row
    recovered = await desktop.DiskJobStore().get('invalid-recovery')
    assert recovered.status is desktop.JobStatus.FAILED
    assert recovered.retryable is True


async def test_restart_fails_safely_for_corrupt_gpu_json():
    jobs = desktop.DiskJobStore()
    await jobs.submit(_desktop_motion_submission('corrupt'))
    rows = desktop.DiskMap('gpu_jobs')
    with rows.db:
        rows.db.execute('INSERT INTO entries VALUES (?, ?)', ('corrupt', '{bad json'))
    assert (await desktop.DiskJobStore().get('corrupt')).status is desktop.JobStatus.FAILED
    pool = desktop.DesktopPool(LocalAssetStore())
    assert pool.tasks == {}
    await pool.plugin._client.aclose()
