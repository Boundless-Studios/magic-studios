from huggingface_hub import hf_hub_download
from pathlib import Path
root=Path.home()/'ComfyUI/models'
for name in [
 'vae/minimax_h3_audio_vae_fp32.safetensors',
 'vae/minimax_h3_video_vae_int8_convrot.safetensors',
 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
]:
 print('Downloading',name,flush=True)
 print('Ready',hf_hub_download('Comfy-Org/MiniMax-H3',name,local_dir=root),flush=True)
