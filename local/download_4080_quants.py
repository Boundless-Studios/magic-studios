from huggingface_hub import hf_hub_download
from pathlib import Path
root = Path.home() / 'ComfyUI/models'
files = [
 ('Kijai/MiniMax-H3-experimental','minimax_h3_fl2va_pruned_w4a8_mixed.safetensors','diffusion_models'),
 ('Kijai/MiniMax-H3-experimental','minimax_h3_ref2va_pruned_w4a8_mixed.safetensors','diffusion_models'),
 ('Comfy-Org/MiniMax-H3','vae/minimax_h3_video_vae_int8_convrot.safetensors','vae'),
]
for repo,name,folder in files:
 target=root/folder/Path(name).name
 if target.exists():
  print('Already present',target,flush=True)
  continue
 print('Downloading',repo,name,flush=True)
 staging=root/'.quant-download'
 path=Path(hf_hub_download(repo,name,local_dir=staging))
 target=root/folder/Path(name).name
 path.replace(target)
 print('Ready',target,flush=True)
