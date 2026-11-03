"""Real CPU plugin executions through the authenticated, fenced worker routes."""

import asyncio
import json
import logging
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from outbound_gpu_worker_pool import (
    MemoryAssetStore,
    MemoryAssetTransfer,
    MemoryAuditLog,
    MemoryJobStore,
    MemoryWorkerAuthenticator,
    MemoryWorkerRegistry,
    WorkerEnrollment,
    WorkerIdentity,
)
from outbound_gpu_worker_pool.agent import WorkerAgent, AgentOutcome
from outbound_gpu_worker_pool.plugins import (
    capability_schemas_from_plugins,
    PluginRequestRejected,
)
from outbound_gpu_worker_pool.service import WorkerPoolService
from magic_video_editor.app import create_app
from magic_video_editor.memory import (
    InMemoryRuntime,
    MemoryJobStore as ProductJobs,
    MemoryAssetStore as ProductAssets,
)
from magic_video_editor.cpu_worker import MovieCpuPlugin, CPU_CAPABILITY, _validated
from magic_video_editor.movies.cpu_pool import PoolMovieMedia, CpuPending
from magic_video_editor.movies.models import Clip


@pytest.mark.asyncio
async def test_cpu_only_worker_claims_and_completes_movie_processing(tmp_path, caplog):
    # A released job carries its reason in the agent's log record; surface it
    # so a failure here says what FFmpeg refused rather than only "released".
    caplog.set_level(logging.INFO, logger="outbound_gpu_worker_pool")
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=120x80:r=24:d=1",
            "-c:v",
            "libx264",
            str(path),
        ],
        check=True,
    )
    assets = MemoryAssetStore(allowed_read_prefixes=("outputs/",))
    source = "outputs/source.mp4"
    assets.assets[source] = path.read_bytes()
    assets.content_types[source] = "video/mp4"
    # The same assembly in-process first: an FFmpeg refusal raises with its
    # stderr here, where the worker path below would only report "ValueError".
    from magic_video_editor.cpu_worker import _assemble

    await asyncio.to_thread(
        _assemble,
        [
            (path.read_bytes(), Clip(shot_id="s", take_id="t", in_seconds=0, out_seconds=0.5, transition_seconds=0.5)),
            (path.read_bytes(), Clip(shot_id="s2", take_id="t2", in_seconds=0, out_seconds=0.5)),
        ],
    )
    pool = WorkerPoolService(
        MemoryJobStore(),
        assets,
        MemoryWorkerRegistry(),
        MemoryAuditLog(),
        MemoryWorkerAuthenticator(
            {"token": WorkerIdentity("cpu-box", "static:cpu-box", "static")}
        ),
        capability_schemas_from_plugins([MovieCpuPlugin()]),
        # A house CPU box the operator approved; the pool admits no other.
        enrollments={"cpu-box": WorkerEnrollment("static:cpu-box", None)},
    )
    runtime = InMemoryRuntime(ProductJobs(), ProductAssets())
    runtime.worker_pool = pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://pool"
    ) as http:
        agent = WorkerAgent(
            coordinator_url="http://pool",
            worker_id="cpu-box",
            credential=lambda: "token",
            plugins=(MovieCpuPlugin(),),
            transfer=MemoryAssetTransfer(assets),
            http=http,
            workspace_root=tmp_path / "jobs",
        )
        await agent.heartbeat()
        media = PoolMovieMedia(assets, pool)
        operations = [
            lambda: media.final_frame(source),
            lambda: media.duration_for_key(source),
            lambda: media.assemble(
                [
                    (
                        source,
                        Clip(
                            shot_id="s",
                            take_id="t",
                            in_seconds=0,
                            out_seconds=0.5,
                            transition_seconds=0.5,
                        ),
                    ),
                    (
                        source,
                        Clip(shot_id="s2", take_id="t2", in_seconds=0, out_seconds=0.5),
                    ),
                ],
                "outputs/cut.mp4",
            ),
        ]
        for operation in operations:
            with pytest.raises(CpuPending):
                await operation()
            # Reconciliation retries the same durable job, never duplicates it.
            with pytest.raises(CpuPending):
                await operation()
            outcome = await agent.run_once()
            reasons = [
                getattr(record, "reason", None)
                for record in caplog.records
                if getattr(record, "outcome", None) is not None
            ]
            assert outcome == AgentOutcome.COMPLETED, reasons
            assert await operation()
        assert assets.assets[source + ".last.png"].startswith(b"\x89PNG")
        assert json.loads(assets.assets[source + ".duration.json"])[
            "duration_seconds"
        ] == pytest.approx(1)
        assert b"ftyp" in assets.assets["outputs/cut.mp4"][:32]
        from magic_video_editor.cpu_worker import _duration

        assert _duration(assets.assets["outputs/cut.mp4"]) == pytest.approx(
            0.75, abs=0.05
        )
        views = await pool.worker_views()
        assert views[0].gpus == ()
        assert CPU_CAPABILITY in views[0].capability_ids


@pytest.mark.parametrize(
    "payload,keys",
    [
        ({"operation": "frame", "sources": [{"key": "secret"}]}, ("allowed",)),
        (
            {
                "operation": "assemble",
                "sources": [
                    {"key": "allowed", "in_seconds": float("nan"), "out_seconds": 1}
                ],
            },
            ("allowed",),
        ),
        (
            {
                "operation": "frame",
                "sources": [{"key": "allowed"}],
                "command": "curl bad",
            },
            ("allowed",),
        ),
        ({"operation": "probe", "sources": [{"key": "allowed"}]}, ("allowed", "extra")),
    ],
)
def test_cpu_plugin_rejects_ungranted_inputs_and_unbounded_commands(payload, keys):
    with pytest.raises(PluginRequestRejected):
        _validated(payload, keys)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["movie.ffmpeg.process.v1", "movie.ffmpeg.process.v2"])
async def test_old_cpu_worker_cannot_claim_transition_jobs(stale):
    from unittest.mock import AsyncMock

    pool = SimpleNamespace(
        get=AsyncMock(return_value=None),
        worker_views=AsyncMock(
            return_value=[
                SimpleNamespace(
                    status="online",
                    capability_ids=(stale,),
                    tenant_id=None,
                )
            ]
        ),
        submit=AsyncMock(),
    )
    media = PoolMovieMedia(MemoryAssetStore(), pool)
    assert (
        await media._process(
            {"operation": "assemble", "sources": []}, "outputs/new.mp4"
        )
        is False
    )
    pool.submit.assert_not_called()


@pytest.mark.parametrize("value", [-1, 2, float("nan"), float("inf"), True, "0.5"])
def test_cpu_plugin_rejects_invalid_transition_duration(value):
    with pytest.raises(PluginRequestRejected):
        _validated(
            {
                "operation": "assemble",
                "sources": [
                    {
                        "key": "outputs/clip.mp4",
                        "in_seconds": 0,
                        "out_seconds": 1,
                        "transition_seconds": value,
                    }
                ],
            },
            ("outputs/clip.mp4",),
        )
