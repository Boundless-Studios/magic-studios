"""Deterministic local Studio composition used for development and E2E."""

from __future__ import annotations

import asyncio
import os
import zlib
from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import urlencode

from fastapi import Query, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageOps
from PIL.PngImagePlugin import PngInfo

from magic_video_editor.app import _execute, create_app
from magic_video_editor.contracts import (
    CompositionGenerationRequest,
    GpuGenerationRequest,
    GpuGenerationResult,
    GpuOperationProgress,
    GpuTelemetry,
    SubjectGenerationRequest,
)
from magic_video_editor.extensions import ExtensionContext, ExtensionLifecycle, load_extensions
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.auth import MemoryStudioAuthStore
from magic_video_editor.studio.enrolment import MemoryStudioEnrolmentStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.operators import ADMIN_EMAILS_ENV, OperatorAccess, admin_emails_from_env
from magic_video_editor.studio.service import StudioService
from magic_video_editor.studio.telemetry import MemoryGpuTelemetryStore
from magic_video_editor.targets import TargetRegistry
from magic_video_editor.targets.example.adapter import ExampleTargetAdapter

_LOCAL_TARGET_URL = "https://example-target.local.invalid/import"
_LOCAL_TARGET_TOKEN = "local-e2e-token"


class LocalAssetStore(MemoryAssetStore):
    async def create_upload_url(
        self, key: str, content_type: str, *, max_bytes: int | None = None
    ) -> str:
        return f"/local-assets/{key}"

    def local_read_path(self, key: str, download_filename: str | None = None) -> str:
        """The single-user local app reads through its own loopback route, which
        also renders posters and frame previews; hosted reads go through the
        owner-authorized `/files` route instead."""
        if download_filename is not None:
            return f"/local-assets/{key}?{urlencode({'download': download_filename})}"
        content = self.assets.get(key, b"")
        media_hint = "?media=image" if content.startswith(b"\x89PNG\r\n\x1a\n") else ""
        return f"/local-assets/{key}{media_hint}"


class DeterministicGpu:
    def __init__(self, assets: LocalAssetStore) -> None:
        self._assets = assets
        self._lock = asyncio.Lock()
        self._progress: dict[str, GpuOperationProgress] = {}

    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        steps = 8 if request.motion_id == "idle" else 12
        try:
            for step in range(steps):
                async with self._lock:
                    self._progress[request.idempotency_key] = GpuOperationProgress(
                        percent=round(step * 100 / steps), stage="rendering"
                    )
                await asyncio.sleep(0.25)
            await self._assets.write_once(
                request.output_key,
                _motion_png(request.motion_id, start_frame_key=request.start_frame_key),
                "image/png",
            )
            return GpuGenerationResult(output_key=request.output_key)
        finally:
            async with self._lock:
                self._progress.pop(request.idempotency_key, None)

    async def status(self) -> GpuTelemetry:
        async with self._lock:
            progress = dict(self._progress)
        return GpuTelemetry(
            provider="Local simulation",
            worker_healthy=True,
            device_name="Deterministic local renderer",
            vram_used_mb=None,
            vram_total_mb=None,
            vram_reserved_mb=None,
            utilization_percent=None,
            active_operation_count=len(progress),
            sampled_at=datetime.now(UTC),
            stale=False,
            operation_progress=progress,
        )


class DeterministicSubjectGenerator:
    """Local-only canonicalizer; production stays disabled without a real provider."""

    def __init__(self, assets: LocalAssetStore) -> None:
        self._assets = assets

    async def generate_subject(self, request: SubjectGenerationRequest) -> str:
        source = await self._assets.read(request.reference_keys[0])
        with Image.open(BytesIO(source)) as opened:
            reference = ImageOps.exif_transpose(opened).convert("RGB")
            fitted = ImageOps.contain(reference, (400, 400))
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        x = (512 - fitted.width) // 2
        y = (512 - fitted.height) // 2
        canvas.paste(fitted.convert("RGBA"), (x, y))
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        await self._assets.write_once(
            request.output_key, output.getvalue(), "image/png"
        )
        return request.output_key


class DeterministicCompositionGenerator:
    """Local-only opening-frame composer: pastes the subject onto a tinted
    canvas so an approve/regenerate round-trip has a visibly different still."""

    def __init__(self, assets: LocalAssetStore) -> None:
        self._assets = assets

    async def compose_frame(self, request: CompositionGenerationRequest) -> str:
        source = await self._assets.read(request.subject_key)
        with Image.open(BytesIO(source)) as opened:
            subject = opened.convert("RGBA")
            fitted = ImageOps.contain(subject, (420, 420))
        tint = (
            (40, 60, 110, 255)
            if "green" not in request.prompt.lower()
            else (30, 90, 40, 255)
        )
        canvas = Image.new("RGBA", (512, 512), tint)
        x = (512 - fitted.width) // 2
        y = (512 - fitted.height) // 2
        canvas.alpha_composite(fitted, (x, y))
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        await self._assets.write_once(
            request.output_key, output.getvalue(), "image/png"
        )
        return request.output_key


class InlineWorkerDispatcher:
    def __init__(self, runtime: InMemoryRuntime) -> None:
        self._runtime = runtime

    async def enqueue(self, job_id: str) -> None:
        asyncio.create_task(self._run(job_id))

    async def _run(self, job_id: str) -> None:
        await asyncio.sleep(0.1)
        await _execute(self._runtime, job_id)


def create_local_studio_runtime(*, inline_dispatch: bool = True) -> InMemoryRuntime:
    assets = LocalAssetStore()
    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, assets)
    runtime.gpu = DeterministicGpu(assets)
    telemetry_store = MemoryGpuTelemetryStore()
    runtime.gpu_telemetry_provider = runtime.gpu
    runtime.gpu_telemetry_store = telemetry_store
    runtime.motion_operations_enabled = True
    dispatcher = (
        InlineWorkerDispatcher(runtime) if inline_dispatch else _NoopDispatcher()
    )
    runtime.dispatcher = dispatcher
    runtime.subject_generator = DeterministicSubjectGenerator(assets)
    runtime.subject_operations_enabled = True
    runtime.composition_generator = DeterministicCompositionGenerator(assets)
    runtime.composition_operations_enabled = True
    # The example adapter needs no real credentials -- these env vars only
    # make `TargetRegistry.delivery_configured("example")` true so the
    # service actually attempts delivery. It records what it received
    # in-memory instead of calling out anywhere.
    os.environ.setdefault("MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_URL", _LOCAL_TARGET_URL)
    os.environ.setdefault("MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_TOKEN", _LOCAL_TARGET_TOKEN)
    target_adapter = ExampleTargetAdapter()
    registry = TargetRegistry()
    registry.register(target_adapter)
    runtime.studio_service = StudioService(
        store=MemoryStudioStore(),
        assets=assets,
        jobs=jobs,
        dispatcher=dispatcher,
        motion_generation_available=True,
        subject_generation_available=True,
        composition_generation_available=True,
        gpu_telemetry_store=telemetry_store,
        target_registry=registry,
    )
    from magic_video_editor.movies.local import LocalMoviePool
    from magic_video_editor.movies.media import MovieMedia
    from magic_video_editor.movies.service import MovieService
    from magic_video_editor.movies.store import MemoryMovieStore

    runtime.movie_service = MovieService(
        MemoryMovieStore(),
        assets,
        LocalMoviePool(assets),
        runtime.composition_generator,
        MovieMedia(assets),
        # The fixture pool renders a first-to-last crossfade for flight takes.
        scene_flights_available=True,
    )
    from magic_video_editor.audio.freesound import FreesoundClient
    from magic_video_editor.audio.media import AudioMedia
    from magic_video_editor.audio.service import AudioService
    from magic_video_editor.audio.store import MemoryAudioStore

    runtime.audio_service = AudioService(
        MemoryAudioStore(),
        assets,
        freesound=FreesoundClient.from_env(os.environ),
        media=AudioMedia(),
        movie_service=runtime.movie_service,
    )
    runtime.target_registry = registry
    # Extensions named in MAGIC_VIDEO_EDITOR_EXTENSIONS run here too, on in-memory
    # stores; with a database URL their stores use PostgreSQL, as when hosted.
    users = MemoryStudioAuthStore()
    runtime.studio_auth_store = users
    runtime.extensions = load_extensions(ExtensionContext(
        values=os.environ,
        users=users,
        enrolment=None,
        operators=OperatorAccess(
            users, admin_emails_from_env(os.environ.get(ADMIN_EMAILS_ENV)), MemoryStudioEnrolmentStore()
        ),
        database_url=os.environ.get("MAGIC_VIDEO_EDITOR_DATABASE_URL"),
    ))
    components = [component for extension in runtime.extensions for component in extension.components]
    if components:
        runtime.lifecycle = ExtensionLifecycle(components)
    return runtime


class _NoopDispatcher:
    async def enqueue(self, job_id: str) -> None:
        return None


def _configure_local_auth_environment() -> None:
    """Local sign-in defaults: the gateway header is the identity, no Auth0 tenant
    is needed, and the session cookie works over plain http."""
    os.environ.setdefault("MAGIC_VIDEO_EDITOR_ALLOWED_EMAIL_DOMAINS", "example.com")
    os.environ.setdefault("MAGIC_VIDEO_EDITOR_AUTH_COOKIE_SECURE", "false")


def create_local_studio_app(*, runtime=None):
    _configure_local_auth_environment()
    runtime = runtime if runtime is not None else create_local_studio_runtime()
    assets = runtime.assets
    application = create_app(runtime)

    @application.middleware("http")
    async def local_identity(request: Request, call_next):
        request.scope["headers"] = [
            (key, value)
            for key, value in request.scope["headers"]
            if key.lower() != b"x-studio-actor-id"
        ] + [(b"x-studio-actor-id", b"local-e2e-user")]
        return await call_next(request)

    @application.put("/local-assets/{key:path}")
    async def upload_local_asset(key: str, request: Request) -> Response:
        assets.assets[key] = await request.body()
        assets.content_types[key] = request.headers.get(
            "content-type", "application/octet-stream"
        )
        return Response(status_code=204)

    @application.get("/local-target-deliveries/{system}")
    async def local_target_deliveries(system: str) -> list[dict[str, object]]:
        """E2E-only introspection of what a recording target adapter received,
        so a Playwright spec can assert a release actually reached the target
        without a second live service."""
        adapter = runtime.target_registry.get(system)
        assert isinstance(adapter, ExampleTargetAdapter)
        return adapter.deliveries

    preview_cache: dict[tuple[str, int], dict] = {}
    preview_lock = asyncio.Lock()
    poster_cache: dict[str, bytes] = {}
    poster_lock = asyncio.Lock()

    @application.get("/local-assets/{key:path}")
    async def read_local_asset(key: str, request: Request, download: str | None = None, frame_previews: int | None = Query(default=None, ge=1, le=512), poster: bool = False) -> Response:
        content = await assets.read(key)
        if poster:
            from hashlib import sha256
            from magic_video_editor.local_frame_previews import first_frame_poster
            cache_key = sha256(content).hexdigest()
            async with poster_lock:
                if cache_key not in poster_cache:
                    data = await first_frame_poster(content)
                    if len(poster_cache) >= 16:
                        poster_cache.pop(next(iter(poster_cache)))
                    poster_cache[cache_key] = data
                return Response(poster_cache[cache_key], media_type='image/png', headers={'Cache-Control': 'private, max-age=86400'})
        if frame_previews is not None:
            from hashlib import sha256
            from magic_video_editor.local_frame_previews import build_frame_previews
            cache_key = (sha256(content).hexdigest(), frame_previews)
            async with preview_lock:
                if cache_key not in preview_cache:
                    result = await build_frame_previews(content, frame_previews)
                    if len(preview_cache) >= 2:
                        preview_cache.pop(next(iter(preview_cache)))
                    preview_cache[cache_key] = result
                return JSONResponse(preview_cache[cache_key])
        descriptor = await assets.describe(key)
        if key in assets.content_types and descriptor is not None:
            media_type = descriptor.content_type
        elif key.endswith(".zip"):
            media_type = "application/zip"
        elif key.endswith(".mp4"):
            media_type = "video/mp4"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        else:
            media_type = "application/octet-stream"
        headers: dict[str, str] = {}
        if download is not None:
            headers = {"Content-Disposition": f'attachment; filename="{download}"'}
        elif key.endswith(".zip"):
            headers = {"Content-Disposition": "attachment"}
        headers['Accept-Ranges'] = 'bytes'
        byte_range = request.headers.get('range')
        if byte_range and byte_range.startswith('bytes=') and ',' not in byte_range:
            try:
                start_text, end_text = byte_range[6:].split('-', 1)
                if start_text:
                    start = int(start_text)
                    end = min(int(end_text), len(content) - 1) if end_text else len(content) - 1
                else:
                    suffix = int(end_text)
                    if suffix <= 0:
                        raise ValueError('invalid suffix')
                    start, end = max(0, len(content) - suffix), len(content) - 1
                if start < 0 or start >= len(content) or end < start:
                    raise ValueError('range outside asset')
            except ValueError:
                return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{len(content)}'})
            headers['Content-Range'] = f'bytes {start}-{end}/{len(content)}'
            return Response(content[start:end + 1], status_code=206, media_type=media_type, headers=headers)
        return Response(content, media_type=media_type, headers=headers)

    return application


def _motion_png(motion_id: str, *, start_frame_key: str | None = None) -> bytes:
    frame_count = 4
    frame_size = 96
    sheet = Image.new("RGBA", (frame_size * frame_count, frame_size), (0, 0, 0, 0))
    color = (72, 96, 180, 255) if motion_id == "idle" else (220, 115, 55, 255)
    if start_frame_key:
        # Deterministically perturb the output by the conditioning pose so a
        # test can prove the approved shared start frame reached the
        # provider (T2), without depending on any specific pose content.
        pose_shift = zlib.crc32(start_frame_key.encode()) % 64
        color = (
            min(255, color[0] + pose_shift),
            color[1],
            color[2],
            color[3],
        )
    for index in range(frame_count):
        frame = Image.new("RGBA", (frame_size, frame_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        offset = (0, 2, 0, -2)[index]
        draw.ellipse((24, 20 + offset, 72, 76 + offset), fill=color)
        draw.ellipse((42 + index, 38 + offset, 47 + index, 43 + offset), fill="white")
        sheet.paste(frame, (index * frame_size, 0))
    png_info = PngInfo()
    png_info.add_text("studio_frame_count", str(frame_count))
    output = BytesIO()
    sheet.save(output, format="PNG", pnginfo=png_info)
    return output.getvalue()


app = create_local_studio_app()
