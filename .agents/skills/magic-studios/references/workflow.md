# Tool calls and machine defaults

For named-view production, first follow [view production](view-production.md); use returned motion IDs in all calls below rather than assuming IDs equal action names.

## Minimal client contract

The client supplies the prepared facing reference, requested motion IDs and, only when needed, a short weapon/action phrase. Start with these calls after subject approval and adding motions:

```text
generate_motions(project_id=P, motion_ids=["walk"])
# After the walk settles, request the attack separately.
generate_motions(project_id=P, motion_ids=["attack"],
                 actions={"attack":"swings each of the axes in their hand to attack"})
```

Omit `settings` to use the discovered machine default. Omit walk `actions` to use `walks in place`. Action text is at most 200 characters; the server owns the compiled prompt. Its attack preset already supplies wind-up, one strike, follow-through, ready-stance recovery, fixed facing/camera/body size and weapon visibility. Long client prompts duplicate those constraints and can conflict with them.

`wait_for_operations` reconciles the generated source and automatically derived alpha candidate for a chroma-green job. It does **not** automatically choose or save a final cycle. Re-list takes; use the native `suggest_motion_loop` proposal, inspect its phases, and save it with `create_frame_revision`. The native attack analyzer prefers compact cycles. In `pose-cycle-v4` and later, when none match, it can propose a longer recovered cycle with an explicit warning; inspect holds and the number of strikes before saving it. An incomplete suggestion is a review result, not a successful loop: retain it and inspect the source before choosing a bounded retry. Use calibration for attack-to-walk scale; do not reimplement these steps in client image/video scripts.

Submit one requested motion first. For larger sets, settle work in bounded groups before filling more slots; an owner concurrency refusal is not a failed GPU render. Record accepted jobs before another submission.

## UI auto-selection buttons

The UI’s **Suggest idle loop**, **Suggest walk loop** and **Suggest attack loop** clicks select a frame interval after generation. They are separate from generation presets and from **Save**. MCP uses `suggest_motion_loop(mode="idle"|"walk"|"attack")` for these modes, followed by `create_frame_revision` to save/select the proposal.

Specify `target_frame_count=16` on both suggestion and revision, plus `cell_size=256` on revision, to match a fresh UI run. The MCP suggestion default is 12; revision count/cell size are otherwise unset. Use returned indices and timing, inspect all 16 frames in pages of at most 12, and keep incomplete proposals flagged.

The UI computes walk suggestions locally from decoded frame poses; idle and attack suggestions call the native server endpoint. MCP calls the native server endpoint for all three modes. These paths serve the same purpose but are separate implementations, so do not promise identical indices. For attack-to-walk body matching, call `calibrate_motion_body` and pass its normalization when saving; the UI performs this measurement during editing.

## Machine defaults

Call `get_generation_config()` first on the desktop. `default_settings` reflects its active recipe. `generate_motions(project_id, motion_ids=["walk"])` without `settings` uses that default; inspect settled take `generation_settings` to confirm. An existing project may carry older settings; old artifacts are never retroactively changed.

The current 16GB desktop default is B: W4A8 mixed pruned H3, NVFP4/AWQ text encoder, INT8 video VAE, LightX2V 8-step v1.0 **768p** LoRA at strength 1, res_multistep/simple, H3 Kitchen override over global Sage, 512×512, four requested seconds, audio decode/export off. This differs from the native-resolution LoRA E and the legacy Larry `turbo` preset. The server resolves its default before recording job provenance. Profile `h3-w4a8-16gb-lightx8` has exactly 8 steps and bounds 512×512/four seconds. A/B/C are experimental labels, not selectable quality tiers.

The standard hosted/24GB route keeps its existing `video.minimax_h3.text_to_video.v1` contract and 24,000 MB minimum. Worker model/attention configuration belongs to that worker; do not assume its quantization from VRAM alone. No 4090 benchmark or 24GB quality ranking was established. No 16GB worker is eligible merely by lowering the standard route's minimum.

After a ready take exists, use named arguments (discover schemas before calling):

```text
list_takes(project_id=P, motion_id="walk")
# Only derive if settlement did not already produce an alpha candidate.
derive_candidate_alpha(project_id=P, motion_id="walk", candidate_id=RAW)
list_takes(project_id=P, motion_id="walk")
suggest_motion_loop(project_id=P, motion_id="walk", candidate_id=ALPHA,
                    mode="walk", target_frame_count=16)
inspect_candidate_frames(project_id=P, motion_id="walk", candidate_id=ALPHA,
                         start=S, limit=12, stride=STRIDE)
create_frame_revision(project_id=P, motion_id="walk", source_candidate_id=ALPHA,
                      ordered_frames=INDICES, frame_duration_ms=MS,
                      loop_policy="loop", target_frame_count=16, cell_size=256)
get_candidate_download(project_id=P, motion_id="walk", candidate_id=SAVED, format="png")
```

Saving selects `SAVED`; preserve prior selections for comparisons. Suggestions may return warnings or need review even when indices are available. If indices are empty, do not invent a successful loop.

For idle, use the native proposal and save its reviewed indices and timing separately:

```text
suggest_motion_loop(project_id=P, motion_id=IDLE_ID, candidate_id=IDLE_ALPHA,
                    mode="idle", target_frame_count=16)
# Inspect all proposed phases and the wrap before saving.
create_frame_revision(project_id=P, motion_id=IDLE_ID, source_candidate_id=IDLE_ALPHA,
                      ordered_frames=IDLE_INDICES, frame_duration_ms=IDLE_MS,
                      loop_policy="loop", target_frame_count=16, cell_size=256)
```

An idle proposal returns `source_frame_rate`, `source_time_base` and `source_interval_ms`; retain them with its ordered indices and returned frame duration. These fields are currently idle-specific: do not assume walk/attack proposals expose them or invent their values. Obtain actual timing from source evidence where available. A frozen source returns an incomplete proposal without indices; it cannot be saved as a successfully extracted idle cycle. A complete proposal still requires visual review.

For attack, first use `mode="attack"` and inspect the impact/recovery interval. Then:

```text
calibrate_motion_body(project_id=P, motion_id="attack", candidate_id=ATTACK_ALPHA,
                      source_frame=READY_FRAME, reference_candidate_id=WALK_SHEET,
                      reference_motion_id="walk", reference_frame=0)
create_frame_revision(project_id=P, motion_id="attack", source_candidate_id=ATTACK_ALPHA,
                      ordered_frames=ATTACK_INDICES, frame_duration_ms=ATTACK_MS,
                      loop_policy="loop", target_frame_count=16, cell_size=256,
                      normalization=RETURNED_NORMALIZATION)
```

The calibration reference must be the deliberately chosen exact selected candidate in a reference motion in the same project and view. For view-scoped sources, pass `reference_motion_id` explicitly (for example, `n-walk` when calibrating `n-attack`); cross-view calibration is refused. Only legacy unassigned motions default an omitted reference motion to `walk`. Record both motion and candidate IDs; never choose the first arbitrary walk in a multi-view project. Keep that walk selected while exporting a calibrated attack; changing it makes the calibration stale. Do not normalize the reference walk against an older revision of itself, then select the result: this invalidates its own reference. A first walk can be saved/exported without cross-motion calibration; its pivot may be null until registration is established. Report null pivots rather than inventing coordinates. Use the same target cell size as the walk; a larger expanded attack canvas is expected and should not trigger another automatic fit. `cleanup_green_edges=true` is opt-in and must be checked for lost metal edges/rivets. Preserve timing from actual source FPS and selected interval instead of using the requested four-second duration for the final loop.

For minimal-risk initial Mac testing: config → project read → takes read → native frame inspection → download an existing PNG. No generation, approval, selection or revision is necessary to verify the connection. Only generate in a new test project after that works.


## Choose a hosted worker

Users can say “generate this on my desktop” or give a worker name/ID. Distinguish
hosted Magic Studios routing from the SSH-local desktop connection: local calls
already execute on that desktop and do not choose among enrolled workers.

For hosted H3 motions:

1. Call `get_generation_config()` and inspect its authorized `workers`. Resolve
   the requested machine to an exact `worker_id`. Discovery rows may lack labels;
   use the account's Machines page/API (`GET /studio/workers`) if available to map
   a name to an ID. Ask for the ID if the name is ambiguous or the mapping is not
   accessible; never guess that the automatic selection is “my desktop”.
2. Call `get_generation_config(worker_id=ID)` and confirm `scope="hosted"`,
   `selected.worker_id == ID`, an available verified profile, and non-null
   `settings`. Pass that returned `settings` object to `generate_motions`, including
   its `provider_parameters.worker_id`, `recipe_version`, `configuration_version`
   and any execution-profile/FPS fields. Retain the selected profile and bounds;
   do not copy the default settings from a different worker or invent a quality tier.
3. After `wait_for_operations`, inspect take recipe/execution provenance for the
   actual executing worker and effective settings. If the connected tool omits
   provenance, use the hosted project API when available or report the verification
   gap; a successful submission alone does not prove which machine rendered it.

Example logical calls (use discovered objects, not literal placeholder settings):

```text
config = get_generation_config(worker_id="requested-worker-id")
generate_motions(project_id=P, motion_ids=["walk"], settings=config.settings)
wait_for_operations(project_id=P)
list_takes(project_id=P, motion_id="walk")
```

Without a requested worker, the controller resolves eligible authorized workers,
preferring online over busy, then worker ID. This is not a speed/quality ranking.
Use the current UI Machines controls when available; MCP discovery remains authoritative for client routing.
Pinned jobs cannot move silently to a different worker/recipe. If the requested
worker is offline, unavailable, or its configuration changed, report it and
refresh discovery. Do not switch machines or submit duplicate work without the
user's authorization. Worker selection never widens account access.

Older MCP deployments may lack the `worker_id` discovery argument. Inspect tool
schemas first; use the authenticated hosted API's
`GET /studio/generation-config?worker_id=ID` if available, otherwise report that
the connection must be updated. A desktop `default_settings` response is not a
hosted worker list. These routing instructions cover H3 motions, not all subject
or movie-processing jobs.

## Use the owner's Higgsfield key

Hosted Magic Studios can send a motion to the owner's own Higgsfield account
instead of a GPU worker. Higgsfield bills that account directly; Magic Studios
adds no fee. Confirm the user wants this and has budget for it.

1. Call `get_generation_config()`. If `providers.higgsfield.configured` is false,
   ask the user to save a Higgsfield API key on the Machines page. Keys are saved
   in the browser only; never ask for a key in chat or pass one to a tool.
2. Pass `providers.higgsfield.settings` to `generate_motions` unchanged, or change
   only `provider_parameters.higgsfield_model` to another model listed under
   `providers.higgsfield.models`, keeping `duration_seconds` inside its range.
   Do not add `worker_id`, recipe or execution-profile fields: the server rejects
   a Higgsfield request that claims a machine.
3. `wait_for_operations` as usual. The take's provenance records
   `completion_path: "higgsfield_api"`, the Higgsfield request ID and model, and
   the hash of the downloaded result. Steps and seed are not sent. These models
   have not been benchmarked in Magic Studios; inspect the result like any take.

A failed Higgsfield submission is never resubmitted automatically, and a request
whose outcome is unknown is failed rather than replayed. Report the failure and
retry only with the user's agreement, since each attempt may be billed.
