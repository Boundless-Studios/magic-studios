from huggingface_hub import hf_hub_download
from pathlib import Path
root = Path.home() / 'ComfyUI/models'
for name in ['diffusion_models/flux-2-klein-base-4b.safetensors', 'text_encoders/qwen_3_4b.safetensors', 'vae/flux2-vae.safetensors']:
 print('Downloading', name, flush=True)
 path=hf_hub_download('Comfy-Org/flux2-klein-4B', 'split_files/'+name, local_dir=root / '.flux-download')
 dest=root/name
 dest.parent.mkdir(parents=True,exist_ok=True)
 Path(path).replace(dest)
 print('Ready',dest,flush=True)
