# H3 walk experiments — 2026-10-16

**Selected winner: B, LightX2V FL2V Turbo 8-step v1.0 768p + Comfy Kitchen attention.** The user preferred B after reviewing the final B/E outputs, including the axe overlapping the leg. Small green traces were judged comparable. The fresh blind static reviewer preferred E; that judgment does not override the user's choice.

The final gallery (`index.html`, `winner.json`, `results.json`, `comparison.json`, `sha256.json` and the media) left Git on 2026-11-02. It is preserved as `experiments-h3-walk-2026-10-16.zip` in the private `art-archive-2026-11-02` release, with checksums in `SHA256SUMS.txt`.

The gallery contains all 24 timing records, the final source clips and extracted loops/sheets. Duplicate same-seed repeat clips are omitted where all 107 decoded RGBA frames matched. Intermediate frames, graphs, debug images, review working files, model weights and runtime state are excluded. The archive is about 32 MiB and is self-contained. `sha256.json` records the gallery files' digests.

## Winning recipe

See `winner.json` in the gallery archive for exact weights, adapter digest, prompt, source indices and fixed body normalization.

- LoRA: `minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors`, strength 1.0.
- 8 steps, `res_multistep` sampler, `simple` scheduler, seed 42.
- 512×512, requested 4 seconds; actual output 107 frames at 24 FPS; no audio.
- Pruned W4A8 mixed H3; NVFP4/AWQ text encoder; INT8 convolution-rotation video VAE.
- H3 model attention: Kitchen. Process-wide attention: Sage. No additional cache, Spectrum or sparse attention.
- Native Windows portable ComfyUI 0.41.0, PyTorch 2.14.0+cu130, RTX 4080 16GB; reserve VRAM 0, dynamic VRAM, fast disk, pinned memory disabled.
- Studio alpha derivation, pose-cycle-v3 selection of 12 phases, fixed body scale/origin against original walk, opt-in edge-green-v2 cleanup. No per-frame resizing.

At experiment checkpoint `da0e904`, this recorded the chosen recipe without changing defaults. The desktop now uses B through its configured `balanced` default; see the README. The main project's selections are unchanged. The original finalized walk remains selected, SHA-256 `c1a3063c56c465ee09e49a0dd2e3719fbabe243bb2c150f634e89ae022803708`. Attack also remains unchanged.

## Results and interpretation

| Recipe | Render seconds | Interpretation |
|---|---:|---|
| Warm 20-step Kitchen control | 44.438 median of 2 | Timing reference |
| A: LightX2V 4-step 768p | 18.742 median of 3 | Fast draft; local shoulder detail/edge defects |
| **B: LightX2V 8-step 768p** | **25.460, one screening run** | **User-selected winner; about 1.75× faster than control** |
| E: LightX2V 8-step native adapter | 25.467 / 25.697 | Blind static review preferred E; user preferred B |
| D: full original Larry v4, 6-step | 26.808 / 22.887 | Author bypass loader, all 51 AdaLN pairs active; coarse/jagged local detail |
| C: 12-step without Turbo adapter | 30.479 | Conservative alternative |

Full configuration timings are retained in the gallery and `results.json`. Times cover rendering through MP4 output, excluding transparency derivation, analysis and export. First/repeat figures are not interchangeable with a multi-run warm median. All same-input runs here used Windows; these data do not establish a Windows-versus-WSL performance difference.

The earlier Larry 8-step Euler/Beta rows used a partial converted LoRA with all 51 AdaLN pairs removed. Those rows are not a faithful evaluation of the original Larry checkpoint. D corrects that with the author's loader, original weights, runtime AdaLN injection, and Euler/Simple at 6 steps, bypass mode enabled.

E uses a genuinely different adapter file without the `768p` suffix; it was not rendered at a larger image size. Both new recipes reproduced 107/107 identical decoded frames across their two renders. The original baseline has some warm mouth shading and slight green edging; coloration alone is not a new failure. A/B exhibit small local shoulder-rim interruptions under close inspection. User preference accepts B's overall result. Neither a static judge pass nor this selection establishes in-game grounding or perceptual cadence.

The historical baseline prompt differed from the generic prompt used in this study. New renders share the same generic prompt, opening image and primary seed; the new 20-step control separates recipe comparisons from historical prompt differences. The gallery's seed-43 rows are explicit holdouts.

Upstream references: [LightX2V](https://huggingface.co/lightx2v/Minimax-h3-Turbo), [Larry loader](https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo), [arena recipes](https://huggingface.co/spaces/multimodalart/h3-acceleration-arena/blob/main/validate/variants.json).
