import asyncio
from outbound_gpu_worker_pool.contracts import JobSubmission
from magic_video_editor.desktop_studio import DesktopPool, DiskMap
from magic_video_editor.local_studio import LocalAssetStore

async def main():
 assets=LocalAssetStore()
 assets.assets=DiskMap('assets',binary=True)
 assets.content_types=DiskMap('content_types')
 pool=DesktopPool(assets)
 request=JobSubmission('setup-flux-smoke-v1','setup-flux-smoke-v1','image.flux2_klein.subject.v1',(),'outputs/local-setup/starting-image.png',payload={'prompt':'A small red sailboat floats on a calm blue lake, distant mountains, clear daylight, cinematic photograph.','steps':20,'seed':42})
 await pool.submit(request)
 if pool.tasks:await asyncio.gather(*list(pool.tasks.values()))
 result=await pool.get(request.job_id)
 print(vars(result),flush=True)
 if result.status!='completed':raise RuntimeError('FLUX smoke test failed')
 from pathlib import Path
 Path('local/starting-image.png').write_bytes(await assets.read(request.output_key))
 await pool.plugin._client.aclose()

asyncio.run(main())
