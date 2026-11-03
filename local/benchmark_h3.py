"""Compare local H3 presets on the saved setup shot; writes MP4s and timings."""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from outbound_gpu_worker_pool.comfy import ComfyWorkflowPlugin, TemplateRegistry
from outbound_gpu_worker_pool.plugins import ExecutionContext

ROOT=Path(__file__).resolve().parent

async def main():
 preset=sys.argv[1] if len(sys.argv)>1 else 'quality'
 backend=sys.argv[2] if len(sys.argv)>2 else 'pytorch'
 runtime=os.environ.get('MAGIC_BENCH_RUNTIME','wsl')
 base_url=os.environ.get('MAGIC_COMFY_BENCH_URL','http://127.0.0.1:8188')
 db=sqlite3.connect(ROOT/'data/movies.sqlite3')
 movie=json.loads(db.execute('SELECT value FROM entries').fetchone()[0])[-1]
 take=movie['shots'][0]['takes'][0]
 assets=sqlite3.connect(ROOT/'data/assets.sqlite3')
 key=take['provider_frame_key']
 registry=TemplateRegistry.from_directory(ROOT/'presets'/preset)
 spectrum=os.environ.get('MAGIC_BENCH_SPECTRUM')=='1'
 if spectrum:
  graph=registry.template('video.minimax_h3.text_to_video.v1').graph
  original=graph['16']['inputs']['model']
  graph['302']={'class_type':'SpectrumApplyMiniMaxH3','inputs':dict(model=original,enabled=True,blend_weight=.5,degree=1,ridge_lambda=.1,window_size=2.,flex_window=.75,warmup_steps=1,tail_actual_steps=1,max_history=8,debug=True,history_storage='system_ram',offline_archive_storage='system_ram',audio_blend_weight=0.,offline_smoothing_replay=True)}
  graph['16']['inputs']['model']=['302',0]
  graph['9']['inputs']['model']=['302',0]
 if backend=='ck':
  graph=registry.template('video.minimax_h3.text_to_video.v1').graph
  original=graph['16']['inputs']['model']
  graph['301']={'class_type':'ModelAttentionBackend','inputs':{'model':original,'attention':'comfy kitchen attention'}}
  graph['16']['inputs']['model']=['301',0]
  graph['9']['inputs']['model']=['301',0]
 payload=dict(prompt=take['prompt'],width=768,height=448,length=73,steps=8 if preset=='turbo' else 20,
  seed=int(hashlib.sha256(take['take_id'].encode()).hexdigest()[:8],16),fps=24,images={'first_frame':key})
 async with httpx.AsyncClient(timeout=120) as client:
  plugin=ComfyWorkflowPlugin(registry,base_url=base_url,client=client,poll_interval_seconds=1,start_command=(str(ROOT/'ensure-comfy.sh'),) if runtime=='wsl' else None,startup_timeout_seconds=240)
  validated=plugin.validate(SimpleNamespace(capability_id='video.minimax_h3.text_to_video.v1',contract_version=1,payload=payload,input_keys=(key,)))
  await plugin.ensure_running()
  stats=(await client.get(base_url+'/system_stats')).json()['system']
  samples=[]
  async def monitor():
   while True:
    p=await asyncio.create_subprocess_exec('/usr/lib/wsl/lib/nvidia-smi','--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits',stdout=asyncio.subprocess.PIPE)
    out,_=await p.communicate()
    try:samples.append([int(v.strip()) for v in out.decode().strip().split(',')])
    except ValueError:pass
    await asyncio.sleep(.5)
  monitor_task=asyncio.create_task(monitor())
  start=time.monotonic()
  try:
   with tempfile.TemporaryDirectory(prefix='magic-bench-') as temp:
    workspace=Path(temp)
    input_path=workspace/'frame.png'
    input_path.write_bytes(assets.execute('SELECT value FROM entries WHERE key=?',(key,)).fetchone()[0])
    async def progress(value):pass
    context=ExecutionContext(str(uuid4()),'benchmark',datetime.now(UTC)+timedelta(minutes=20),workspace,{key:input_path},asyncio.Event(),progress)
    output=await plugin.execute(context,validated)
    destination=ROOT/f'benchmark-{runtime}-{preset}{"-spectrum" if spectrum else ""}-{backend}.mp4'
    destination.write_bytes(output.path.read_bytes())
    result=dict(runtime=runtime,comfy_version=stats['comfyui_version'],torch_version=stats['pytorch_version'],preset=preset,backend=backend,seconds=round(time.monotonic()-start,2),peak_vram_mib=max((s[0] for s in samples),default=0),size=destination.stat().st_size,prompt_id=output.diagnostics.get('prompt_id'),steps=payload['steps'],width=768,height=448,frames=73)
    if spectrum: result['spectrum']='0.2.29-default-offline-replay'
  except Exception as error:
   result=dict(runtime=runtime,preset=preset,backend=backend,error=str(error),seconds=round(time.monotonic()-start,2))
  finally:
   monitor_task.cancel()
   try:await monitor_task
   except asyncio.CancelledError:pass
  with (ROOT/'benchmark-results.jsonl').open('a') as log:log.write(json.dumps(result)+'\n')
  print(json.dumps(result),flush=True)

asyncio.run(main())
