"""Single-user loopback Studio with real ComfyUI movies and durable local files."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from outbound_gpu_worker_pool.comfy import ComfyWorkflowPlugin, TemplateRegistry
from outbound_gpu_worker_pool.plugins import ExecutionContext
from outbound_gpu_worker_pool.contracts import JobRequirements, JobStatus as PoolJobStatus
from pydantic import TypeAdapter

from magic_video_editor.contracts import GpuTelemetry
from magic_video_editor.local_studio import create_local_studio_app, create_local_studio_runtime
from magic_video_editor.movies.models import Movie
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.store import StoredManifest, RawManifestRevision
from magic_video_editor.studio.models import Release, ExportRecord, MotionGenerationSettings
from magic_video_editor.memory import MemoryJobStore
from magic_video_editor.contracts import JobOperation, JobRecord, JobStatus
from magic_video_editor.plugins.ffmpeg import FfmpegVideoProcessor
from magic_video_editor.plugins.subject_pool import PoolSubjectGenerator
from magic_video_editor.flights.media import registry_supports_scene_flights
from magic_video_editor.flights.service import recipe as scene_flight_recipe

def desktop_root() -> Path:
    """Locate desktop deployment files independently of Python installation mode."""
    configured = os.environ.get('MAGIC_DESKTOP_ROOT')
    if configured:
        return Path(configured).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / 'local/presets').is_dir():
        return source_root
    return Path.cwd()


ROOT = desktop_root()
DATA = Path(os.environ.get('MAGIC_DESKTOP_DATA', ROOT / 'local/data'))
DATA.mkdir(parents=True, exist_ok=True)
log = logging.getLogger(__name__)


class DiskMap(MutableMapping):
    """SQLite mapping keeps uploaded/generated media out of process memory."""
    def __init__(self, name, *, binary=False):
        self.db = sqlite3.connect(DATA / (name + '.sqlite3'))
        self.db.execute('CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, value BLOB)')
        self.binary = binary

    def __getitem__(self, key):
        row = self.db.execute('SELECT value FROM entries WHERE key=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0] if self.binary else json.loads(row[0])

    def __setitem__(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO entries VALUES (?, ?)', (key, value if self.binary else json.dumps(value)))

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        with self.db:
            self.db.execute('DELETE FROM entries WHERE key=?', (key,))

    def __iter__(self):
        return iter([r[0] for r in self.db.execute('SELECT key FROM entries')])

    def __len__(self):
        return self.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]

    def __contains__(self, key):
        return self.db.execute('SELECT 1 FROM entries WHERE key=?', (key,)).fetchone() is not None


class DiskMovieStore(MemoryMovieStore):
    def __init__(self):
        super().__init__()
        self.snapshots = DiskMap('movies')
        self.asset_usage = DiskMap('movie_usage')
        self.rows = {key: [Movie.model_validate(r) for r in self.snapshots[key]] for key in self.snapshots}

    def save(self, movie):
        self.snapshots[movie.movie_id] = [m.model_dump(mode='json') for m in self.rows[movie.movie_id]]

    async def delete(self, id):
        await super().delete(id)
        if id in self.snapshots:
            del self.snapshots[id]

    async def create(self, movie):
        result = await super().create(movie)
        self.save(result)
        return result

    async def append(self, movie, expected):
        result = await super().append(movie, expected)
        self.save(result)
        return result


class DiskStudioStore(MemoryStudioStore):
    """Persist the existing Studio manifests, releases and export contracts."""
    def __init__(self):
        super().__init__()
        self.snapshots = DiskMap('animation_projects')
        self.release_rows = DiskMap('animation_releases')
        self.export_rows = DiskMap('animation_exports')
        for key in self.snapshots:
            revisions = [StoredManifest.model_validate(r) for r in self.snapshots[key]]
            self._revisions[key] = revisions
            self._projects[key] = revisions[-1]
            self._current_revision[key] = revisions[-1].revision
            self._created_at[key] = revisions[0].updated_at
            self._created_by[key] = revisions[0].manifest.owner_id
            self._raw_revisions[key] = [RawManifestRevision(r.revision, r.manifest.model_dump(mode='json'), r.updated_at) for r in revisions]
        for key in self.release_rows:
            release = Release.model_validate(self.release_rows[key])
            self._releases[key] = release
            self._release_names[(release.project_id, release.name)] = key
        self._exports = {key: ExportRecord.model_validate(self.export_rows[key]) for key in self.export_rows}

    def save(self, project_id):
        self.snapshots[project_id] = [r.model_dump(mode='json') for r in self._revisions[project_id]]

    async def create(self, manifest):
        result = await super().create(manifest)
        self.save(manifest.project_id)
        return result

    async def append(self, project_id, expected_revision, manifest):
        result = await super().append(project_id, expected_revision, manifest)
        self.save(project_id)
        return result

    async def create_release(self, **kwargs):
        result = await super().create_release(**kwargs)
        self.release_rows[result.release_id] = result.model_dump(mode='json')
        return result

    async def save_export(self, record):
        result = await super().save_export(record)
        self.export_rows[result.export_id] = result.model_dump(mode='json')
        return result

    async def delete_project(self, project_id):
        await super().delete_project(project_id)
        for rows, live in ((self.release_rows, self._releases), (self.export_rows, self._exports)):
            for key in [key for key in rows if key not in live]:
                del rows[key]
        if project_id in self.snapshots:
            del self.snapshots[project_id]


class DiskJobStore(MemoryJobStore):
    """Keep the Studio operation ledger across local service restarts."""
    def __init__(self):
        super().__init__()
        self.rows = DiskMap('animation_jobs')
        self.adapter = TypeAdapter(JobRecord)
        pool_rows = DiskMap('gpu_jobs')
        for key in self.rows:
            record = self.adapter.validate_python(self.rows[key])
            # Terminal GPU outcomes can still settle an unsynced Studio operation.
            try:
                pool_row = pool_rows.get(key, {})
                if isinstance(pool_row, dict):
                    JobRequirements.from_payload(pool_row.get('requirements', {}))
            except (ValueError, TypeError, AttributeError):
                pool_row = {}
            recoverable = (record.operation is JobOperation.GENERATE_MOTION
                           and isinstance(pool_row, dict)
                           and pool_row.get('status') in {'completed', 'failed', 'cancelled'}
                           and pool_row.get('output_key') == record.output_key)
            if record.status in {JobStatus.QUEUED, JobStatus.PROCESSING} and not recoverable:
                record = replace(record, status=JobStatus.FAILED, failure_message='Local server restarted; generate another take.', retryable=True)
            self.records[key] = record
        self.persist()

    def persist(self):
        for key, record in self.records.items():
            self.rows[key] = self.adapter.dump_python(record, mode='json')

    async def delete_for_project(self, project_id):
        deleted = await super().delete_for_project(project_id)
        for key in [key for key in self.rows if key not in self.records]:
            del self.rows[key]
        return deleted


def _persist_job_mutation(name):
    async def call(self, *args, **kwargs):
        result = await getattr(super(DiskJobStore, self), name)(*args, **kwargs)
        self.persist()
        return result
    return call


for _method in ('submit', 'cancel', 'claim', 'complete', 'fail', 'release', 'complete_unclaimed', 'fail_unclaimed', 'update_progress', 'note_settlement'):
    setattr(DiskJobStore, _method, _persist_job_mutation(_method))


class UnavailableLocalGenerator:
    async def generate(self, request):
        raise RuntimeError('Local animation generation must use the configured ComfyUI pool.')


@dataclass(frozen=True)
class DesktopPoolRecord:
    """Durable desktop outcome; local execution has no pool worker attestation."""

    status: PoolJobStatus
    completion_path: str = 'desktop_local'
    output_key: str | None = None
    capability_id: str | None = None
    requirements: JobRequirements = field(default_factory=JobRequirements)
    executed_by: str | None = None
    execution_metadata: dict = field(default_factory=dict)
    output_sha256: str | None = None
    output_byte_length: int | None = None
    error: str | None = None
    failure_message: str | None = None
    retryable: bool = False
    preset: str | None = None
    identity: str | None = None
    progress: object = None


class DesktopPool:
    def __init__(self, assets):
        self.assets = assets
        self.jobs = DiskMap('gpu_jobs')
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        self.preset = os.environ.get('MAGIC_DESKTOP_PRESET', 'quality')
        if self.preset not in {'quality', 'cache', 'turbo', 'sol', 'balanced'}:
            raise ValueError('Unknown desktop preset')
        preset_directory = ROOT / 'local/presets' / self.preset
        if not preset_directory.is_dir():
            raise RuntimeError(
                f'Desktop templates not found: {preset_directory}. '
                'Set MAGIC_DESKTOP_ROOT to the Magic Video Editor checkout '
                'or start the desktop server from that directory.'
            )
        registry = TemplateRegistry.from_directory(preset_directory)
        attention = os.environ.get('MAGIC_DESKTOP_ATTENTION', 'pytorch')
        backends = {'pytorch': 'pytorch attention', 'sage': 'sage attention', 'ck': 'comfy kitchen attention'}
        if attention not in backends:
            raise ValueError('Unknown desktop attention backend')
        if attention == 'ck':
            for template in registry.templates:
                if template.model_id == 'minimax-h3':
                    graph = template.graph
                    original = graph['16']['inputs']['model']
                    graph['301'] = {'class_type': 'ModelAttentionBackend', 'inputs': {'model': original, 'attention': backends[attention]}}
                    graph['16']['inputs']['model'] = ['301', 0]
                    graph['9']['inputs']['model'] = ['301', 0]
        self.plugin = ComfyWorkflowPlugin(
            registry,
            base_url=os.environ.get('MAGIC_DESKTOP_COMFY_URL', 'http://127.0.0.1:8188'),
            client=httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3)),
            start_command=(str(ROOT / 'local/ensure-comfy.sh'),),
            startup_timeout_seconds=240,
        )
        # A process restart cannot prove whether an interrupted GPU request finished.
        # Mark it failed rather than silently spending another generation.
        for key in self.jobs:
            try:
                job = self.jobs[key]
            except (ValueError, TypeError):
                log.error('Ignoring corrupt local GPU record: %s', key)
                continue
            if not isinstance(job, dict):
                log.error('Ignoring malformed local GPU record: %s', key)
                continue
            if job.get('status') in ('queued', 'processing'):
                job.update(status='failed', failure_message='Local server restarted during generation; request another take.')
                self.jobs[key] = job

    async def submit(self, request):
        identity = hashlib.sha256(repr(request).encode()).hexdigest()
        if request.job_id in self.jobs:
            job = self.jobs[request.job_id]
            if job['identity'] != identity:
                raise ValueError('Job ID reused for different generation inputs')
            return await self.get(request.job_id)
        if self.preset == 'turbo' and request.capability_id in ('video.minimax_h3.text_to_video.v1', 'video.minimax_h3.text_to_video.w4a8_16gb.v1'):
            request = replace(request, payload={**request.payload, 'steps': 8})
        lease = SimpleNamespace(capability_id=request.capability_id, contract_version=1,
                                payload=request.payload, input_keys=request.input_keys)
        validated = self.plugin.validate(lease)
        job = dict(status='queued', output_key=request.output_key, identity=identity, preset=self.preset,
                   capability_id=request.capability_id, requirements=request.requirements.as_payload(),
                   error=None, failure_message=None, retryable=False)
        self.jobs[request.job_id] = job
        self.tasks[request.job_id] = asyncio.create_task(self.run(request, validated))
        return await self.get(request.job_id)

    async def run(self, request, validated):
        job = self.jobs[request.job_id]
        try:
            async with self.slot:
                job['status'] = 'processing'
                self.jobs[request.job_id] = job
                with tempfile.TemporaryDirectory(prefix='magic-gpu-') as temp:
                    workspace = Path(temp)
                    paths = {}
                    for index, key in enumerate(request.input_keys):
                        path = workspace / f'input-{index}{Path(key).suffix}'
                        path.write_bytes(await self.assets.read(key))
                        paths[key] = path
                    async def progress(value):
                        job['progress'] = value
                        self.jobs[request.job_id] = job
                    context = ExecutionContext(request.job_id, request.job_id,
                        datetime.now(UTC) + timedelta(hours=6), workspace, paths,
                        asyncio.Event(), progress)
                    output = await self.plugin.execute(context, validated)
                    content = output.path.read_bytes()
                    await self.assets.write_once(request.output_key, content, output.content_type)
                    job.update(status='completed', output_sha256=hashlib.sha256(content).hexdigest(), output_byte_length=len(content))
        except asyncio.CancelledError:
            job['status'] = 'cancelled'
            raise
        except Exception as error:
            log.exception('Local GPU generation failed: %s', request.job_id)
            job.update(status='failed', failure_message=str(error))
        finally:
            self.jobs[request.job_id] = job
            self.tasks.pop(request.job_id, None)

    async def get(self, id):
        if id not in self.jobs:
            return None
        job = self.jobs[id]
        return DesktopPoolRecord(
            **{key: value for key, value in job.items()
               if key in DesktopPoolRecord.__dataclass_fields__
               and key not in {'status', 'requirements', 'executed_by', 'execution_metadata'}},
            status=PoolJobStatus(job['status']),
            requirements=JobRequirements.from_payload(job.get('requirements', {})),
        )

    async def cancel(self, id):
        task = self.tasks.get(id)
        if task:
            task.cancel()
            job = self.jobs[id]
            job['status'] = 'cancelled'
            self.jobs[id] = job
            await self.plugin.cancel(id)
            return True
        return False


class DesktopTelemetry:
    async def status(self):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(os.environ.get('MAGIC_DESKTOP_COMFY_URL', 'http://127.0.0.1:8188').rstrip('/') + '/system_stats')
                response.raise_for_status()
                device = response.json()['devices'][0]
            total = device['vram_total'] // (1024 * 1024)
            free = device['vram_free'] // (1024 * 1024)
            return GpuTelemetry(provider='Local ComfyUI', worker_healthy=True,
                device_name=device['name'], vram_used_mb=total-free, vram_total_mb=total,
                vram_reserved_mb=None, utilization_percent=None, active_operation_count=None,
                sampled_at=datetime.now(UTC), stale=False, operation_progress={})
        except (httpx.HTTPError, KeyError, IndexError):
            return GpuTelemetry(provider='Local ComfyUI', worker_healthy=False,
                device_name='RTX 4080 (ComfyUI starts on demand)', vram_used_mb=None,
                vram_total_mb=16376, vram_reserved_mb=None, utilization_percent=None,
                active_operation_count=None, sampled_at=datetime.now(UTC), stale=False,
                operation_progress={})


def balanced_motion_settings(settings: MotionGenerationSettings) -> MotionGenerationSettings:
    """Default sprite generations to B, retaining explicit alternative profiles."""
    if settings.provider_parameters.get('execution_profile'):
        return settings
    return MotionGenerationSettings.model_validate({
        **settings.model_dump(), 'steps': 8,
        'provider_parameters': {**settings.provider_parameters,
                                'execution_profile': 'h3-w4a8-16gb-lightx8'},
    })


def create_desktop_app():
    runtime = create_local_studio_runtime()
    runtime.assets.assets = DiskMap('assets', binary=True)
    runtime.assets.content_types = DiskMap('content_types')
    runtime.gpu_telemetry_provider = DesktopTelemetry()
    service = runtime.movie_service
    service.store = DiskMovieStore()
    service.pool = DesktopPool(runtime.assets)
    service.composition = None
    service.max_batch_size = 1
    # Flights run the preset's own H3 template; it must bind a last frame.
    # The legacy turbo preset rewrites steps at submission, which would
    # misrecord a flight's recipe, so it offers no flights.
    service.scene_flights_available = (
        service.pool.preset != 'turbo'
        and registry_supports_scene_flights(service.pool.plugin._registry))
    # Use Studio's own preset compiler, opening-frame renderer and settlement.
    # Only its executor changes to the local ComfyUI instance.
    runtime.jobs = DiskJobStore()
    runtime.processor = FfmpegVideoProcessor()
    runtime.gpu = UnavailableLocalGenerator()
    studio = runtime.studio_service
    studio._store = DiskStudioStore()
    studio._jobs = runtime.jobs
    studio._motion_pool = service.pool
    studio._motion_generation_available = True
    if service.pool.preset == 'balanced':
        studio.motion_settings_resolver = balanced_motion_settings
    runtime.subject_generator = PoolSubjectGenerator(service.pool, runtime.assets)
    studio._subject_generation_available = True
    studio._composition_generation_available = False
    runtime.motion_operations_enabled = True
    runtime.subject_operations_enabled = True
    runtime.composition_operations_enabled = False
    application = create_local_studio_app(runtime=runtime)

    @application.get('/studio/generation-config')
    async def generation_config():
        defaults = MotionGenerationSettings()
        if studio.motion_settings_resolver is not None:
            defaults = studio.motion_settings_resolver(defaults)
        global_attention = os.environ.get('MAGIC_DESKTOP_GLOBAL_ATTENTION', os.environ.get('MAGIC_DESKTOP_ATTENTION', 'pytorch'))
        if global_attention == 'ck':
            global_attention = 'pytorch'  # Kitchen is selected per H3 model.
        return {'scope': 'desktop', 'preset': service.pool.preset,
                'default_settings': defaults.model_dump(mode='json'),
                'default_recipe': 'B: LightX2V 8-step v1.0 768p, strength 1, res_multistep/simple' if service.pool.preset == 'balanced' else service.pool.preset,
                'bounds': {'max_width': 512, 'max_height': 512, 'max_duration_seconds': 4} if service.pool.preset == 'balanced' else None,
                'attention': os.environ.get('MAGIC_DESKTOP_ATTENTION', 'pytorch'),
                'global_attention': global_attention,
                'comfy_url': os.environ.get('MAGIC_DESKTOP_COMFY_URL', 'http://127.0.0.1:8188'),
                # Flights use the preset's base H3 template at the movie canvas,
                # never the 512x512-bounded LightX2V sprite capability.
                'scene_flight': {'available': service.scene_flights_available,
                                 **scene_flight_recipe()}}

    @application.get('/pool/workers')
    async def desktop_workers():
        telemetry = await runtime.gpu_telemetry_provider.status()
        busy = next((key for key in service.pool.jobs if service.pool.jobs[key]['status'] == 'processing'), None)
        return {'cluster_admin': False, 'workers': [{
            'worker_id': 'local-rtx4080', 'label': 'This computer',
            'status': ('busy' if busy else 'online') if telemetry.worker_healthy else 'offline',
            'last_heartbeat_at': telemetry.sampled_at.isoformat(),
            'capabilities': [t.capability_id for t in service.pool.plugin._registry.templates],
            'gpus': [{'index': 0, 'name': telemetry.device_name,
                'utilization_pct': telemetry.utilization_percent,
                'memory_used_mb': telemetry.vram_used_mb, 'memory_total_mb': telemetry.vram_total_mb}],
            'busy_job_id': busy, 'draining': False,
        }]}

    return application


class LazyDesktopApp:
    """Importing helpers/tests must not open or recover the live job database."""
    def __init__(self):
        self.application = None

    async def __call__(self, scope, receive, send):
        if self.application is None:
            self.application = create_desktop_app()
        await self.application(scope, receive, send)


app = LazyDesktopApp()
