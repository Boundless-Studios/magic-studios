"""Exercise a profile through the real worker/coordinator protocol and local ComfyUI.

Uses isolated in-memory enrollment/assets/jobs, never a user's Studio project or
pairing code. ComfyUI must already be running and idle. Output is written only to
--output-dir. This is not evidence of a hosted deployment or hosted enrollment.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI
from outbound_gpu_worker_pool.agent import WorkerAgent, AgentOutcome
from outbound_gpu_worker_pool.contracts import JobSubmission, JobRequirements, WorkerIdentity
from outbound_gpu_worker_pool.memory import MemoryJobStore, MemoryAssetStore, MemoryWorkerRegistry, MemoryAuditLog, MemoryWorkerAuthenticator, MemoryAssetTransfer
from outbound_gpu_worker_pool.plugins import capability_schemas_from_plugins
from outbound_gpu_worker_pool.routes import create_worker_router
from outbound_gpu_worker_pool.service import WorkerPoolService
from outbound_gpu_worker_pool.enrollment import WorkerEnrollment
from outbound_gpu_worker_pool.machine_profiles import LIGHTX8, RECIPE_KEY, CONFIG_KEY, describe_profile, prepare_environment, ProfileComfyPlugin


async def verify(args):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(args.comfy_url.rstrip('/') + '/queue')
        response.raise_for_status()
        queue = response.json()
        if queue.get('queue_running') or queue.get('queue_pending'):
            raise ValueError('ComfyUI queue must be idle before this test')
    prepared = await prepare_environment({'MVS_WORKER_PROFILE': args.profile, 'OGWP_COMFY_URL': args.comfy_url})
    versions = json.loads(prepared['OGWP_WORKER_RUNTIME_VERSIONS'])
    profile = describe_profile(args.profile)
    report = {'profile': profile, 'runtime_versions': versions, 'verification_scope': 'isolated local protocol test'}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.preflight_only:
        (args.output_dir/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2)); return
    if not args.source:
        raise ValueError('--source PNG is required for a render test')
    assets = MemoryAssetStore(allowed_read_prefixes=('sources/enrollment/', 'outputs/enrollment/'))
    source = 'sources/enrollment/source.png'
    assets.assets[source] = args.source.read_bytes()
    assets.content_types[source] = 'image/png'
    worker_id = 'isolated-profile-test'
    identity = WorkerIdentity(worker_id, 'static:isolated-profile-test', 'static')
    plugin_client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3))
    plugin = ProfileComfyPlugin(args.profile, versions, client=plugin_client, base_url=args.comfy_url)
    jobs = MemoryJobStore()
    pool = WorkerPoolService(jobs, assets, MemoryWorkerRegistry(), MemoryAuditLog(),
        MemoryWorkerAuthenticator({'local-test':identity}), capability_schemas_from_plugins([plugin]),
        enrollments={worker_id:WorkerEnrollment(identity.subject, 'isolated')})
    app = FastAPI(); app.include_router(create_worker_router(pool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://coordinator') as http:
        agent = WorkerAgent(coordinator_url='http://coordinator', worker_id=worker_id,
            credential=lambda:'local-test', plugins=(plugin,), transfer=MemoryAssetTransfer(assets),
            http=http, workspace_root=args.output_dir/'workspace', tenant_id='isolated',
            vram_mb=int(prepared['OGWP_WORKER_VRAM_MB']), gpu_model=prepared['OGWP_WORKER_GPU_MODEL'],
            runtime_versions=versions)
        await agent.heartbeat()
        job_id = str(uuid4()); output_key='outputs/enrollment/'+job_id+'.mp4'
        await pool.submit(JobSubmission(job_id, job_id, profile['capability_id'], (source,), output_key,
            tenant_id='isolated', requirements=JobRequirements(min_vram_mb=profile['min_vram_mb'],
                worker_ids=(worker_id,), runtime_versions={RECIPE_KEY:versions[RECIPE_KEY], CONFIG_KEY:versions[CONFIG_KEY]}),
            payload={'prompt':'The character walks in place — a clean, seamless walk cycle.',
                     'width':512,'height':512,'length':96,'fps':24,'steps':profile['steps'],
                     'seed':42,'images':{'first_frame':source}}))
        started = time.monotonic()
        outcome = await agent.run_once()
        record = await pool.get(job_id)
        if outcome != AgentOutcome.COMPLETED:
            raise RuntimeError(f'Profile test failed: {outcome}; {record.failure_code}')
        content = assets.assets[output_key]
        target=args.output_dir/'generated.mp4'; target.write_bytes(content)
        report.update(elapsed_seconds=round(time.monotonic()-started, 3),
                      worker_attestation=record.execution_metadata,
                      job_id=job_id, executing_worker_id=record.executed_by,
                      output_sha256=hashlib.sha256(content).hexdigest(), output_bytes=len(content),
                      effective_payload=record.payload, output_path=str(target))
        assert record.executed_by == worker_id and record.output_sha256 == report['output_sha256']
        assert record.execution_metadata['diagnostics']['effective_inputs']['steps'] == profile['steps']
        assert record.execution_metadata['diagnostics']['runtime_versions'] == versions
    await plugin_client.aclose()
    (args.output_dir/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=LIGHTX8)
    parser.add_argument('--comfy-url', default='http://127.0.0.1:8189')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--preflight-only', action='store_true')
    asyncio.run(verify(parser.parse_args()))


if __name__ == '__main__': main()
