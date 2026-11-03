from huggingface_hub import hf_hub_download
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
root = Path.home() / 'ComfyUI/models'
names = [
 'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors',
 'diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors',
 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
 'vae/minimax_h3_video_vae_fp16.safetensors',
 'vae/minimax_h3_audio_vae_fp32.safetensors',
]
def download(name):
 print('Downloading', name, flush=True)
 result = hf_hub_download('Comfy-Org/MiniMax-H3', name, local_dir=root)
 print('Ready', result, flush=True)
with ThreadPoolExecutor(max_workers=2) as pool:
 list(pool.map(download, names))
