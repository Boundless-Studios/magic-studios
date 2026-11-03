"""Build approved desktop workflows; production templates remain untouched."""
import copy
import json
from pathlib import Path

root = Path(__file__).resolve().parent
source = root / 'templates'
# Keep the upstream graphs but use native W4A8 to fit more weights on the 4080.
for name, weight in [('minimax_h3_text_to_video','minimax_h3_fl2va_pruned_w4a8_mixed.safetensors'),('minimax_h3_dialogue','minimax_h3_ref2va_pruned_w4a8_mixed.safetensors')]:
 path=source/(name+'.template.json')
 template=json.loads(path.read_text())
 template['graph']['6']['inputs']['unet_name']=weight
 template['graph']['11']['inputs']['vae_name']='minimax_h3_video_vae_int8_convrot.safetensors'
 if 'text_to_video' in name:
  # Sprite motion needs pixels only. H3 still samples its joint AV latent,
  # but the unused audio decoder and audio VAE need not run or load.
  if not any(slot['name']=='last_frame' for slot in template['image_slots']):
   template['image_slots'].append({'name':'last_frame','node_id':'201','required':False})
  template['graph']['201']={'class_type':'LoadImage','inputs':{'image':'last_frame.png'}}
  template['graph']['104']['inputs']['last_frame']=['201',0]
  template['graph'].pop('23', None)
  template['graph'].pop('24', None)
  template['graph']['91']['inputs'].pop('audio', None)
 template['model_version']='fl2va-w4a8-mixed' if 'text_to' in name else 'ref2va-w4a8-mixed'
 path.write_text(json.dumps(template,indent=2)+'\n')
for preset in ['quality','cache','turbo','sol','balanced']:
 folder=root/'presets'/preset
 folder.mkdir(parents=True,exist_ok=True)
 for path in source.glob('*.template.json'):
  template=json.loads(path.read_text())
  if template['model_id']=='minimax-h3':
   graph=template['graph']
   if preset=='cache':
    graph['300']={'class_type':'ApplyMiniMaxH3FirstBlockCache','inputs':dict(model=['6',0],mode='H3 Safe — 0.08 / max 2',threshold=.08,start_percent=.1,end_percent=.95,max_consecutive_hits=2,temporal_guard=False)}
    graph['16']['inputs']['model']=['300',0]
    graph['9']['inputs']['model']=['300',0]
   if preset=='sol':
    graph['300']={'class_type':'BlockSparseAttention','inputs':dict(model=['6',0],selection='sol-attn',start_percent=.2,end_percent=.9,dense_blocks='0,1',min_tokens=12288,extra_tokens=256,sink_conditioning='exact_kv_and_rows',verbose=True)}
    graph['300']['inputs']['selection.tau']=1.0
    graph['16']['inputs']['model']=['300',0]
    graph['9']['inputs']['model']=['300',0]
   # This Turbo adapter is trained for FL2VA. Keep reference/dialogue full-step.
   if preset=='turbo' and 'text_to_video' in path.name:
    graph['300']={'class_type':'LoraLoaderModelOnly','inputs':dict(model=['6',0],lora_name='minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors',strength_model=1.)}
    graph['16']['inputs']['model']=['300',0]
    graph['9']['inputs'].update(model=['300',0],scheduler='beta',steps=8)
    graph['17']['inputs']['sampler_name']='euler'
   template['template_version']+='-desktop-'+preset
  (folder/path.name).write_text(json.dumps(template,indent=2)+'\n')
  if 'text_to_video' in path.name:
   quantized=copy.deepcopy(template)
   quantized['capability_id']='video.minimax_h3.text_to_video.w4a8_16gb.v1'
   quantized['template_version']+='-16gb-v1'
   (folder/'minimax_h3_16gb.template.json').write_text(json.dumps(quantized,indent=2)+'\n')
 # An explicit recipe always starts from the clean source graph, never from
 # another preset's cache/LoRA stack. The existing 16 GB alias remains base.
 balanced=json.loads((source/'minimax_h3_text_to_video.template.json').read_text())
 balanced['capability_id']='video.minimax_h3.text_to_video.w4a8_16gb.lightx8.v1'
 balanced['template_version']+='-desktop-lightx8-768p-v1'
 graph=balanced['graph']
 graph['300']={'class_type':'LoraLoaderModelOnly','inputs':dict(model=['6',0],lora_name='minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors',strength_model=1.)}
 graph['16']['inputs']['model']=['300',0]
 graph['9']['inputs'].update(model=['300',0],scheduler='simple',steps=8)
 graph['17']['inputs']['sampler_name']='res_multistep'
 (folder/'minimax_h3_lightx8_16gb.template.json').write_text(json.dumps(balanced,indent=2)+'\n')
print('Built quality, cache, turbo, sol and balanced presets with explicit LightX2V 8-step recipe')
