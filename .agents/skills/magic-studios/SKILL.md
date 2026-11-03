---
name: magic-studios
description: Generate and edit supplied-view character idle, walk and attack sprite animations through Magic Studios MCP, including remote uploads, loop extraction, body-scale matching, and exports. Use with an existing Magic Studios connection; not for generic video editing or replacing Studio's motion pipeline.
---

# Magic Studios sprite workflow

Use the running server's MCP tools for generation and editing. Do not recreate its motion compiler, frame decoder or loop detector in client scripts. Tool discovery and returned schemas are authoritative; names below are unprefixed logical names.

## Connect and preserve context

- Use the configured MCP connection. Local desktop access is via SSH tunnel, not an unauthenticated LAN listener. Connection setup and a Mac test prompt are in [references/connection.md](references/connection.md).
- A user may request a specific hosted worker by name or ID (for example, “use my desktop”). Follow [worker selection](references/workflow.md#choose-a-hosted-worker) to resolve it, obtain its settings and pin the job; never silently substitute another machine. The SSH-local connection already targets its desktop.
- Call `get_generation_config` before generating on the desktop. It reports the actual machine default, not user-selectable quality tiers. A 404 on another deployment means discovery is unsupported; do not infer hardware or copy the desktop's profile there.
- Generation runs on the owner's GPU or, when they ask, on their own Higgsfield account. Use Higgsfield only when the user requests it or has no machine and agrees: it bills their Higgsfield balance. Pass `providers.higgsfield.settings` from `get_generation_config` unchanged (optionally another listed `higgsfield_model`); without a saved key, ask them to add it on the Machines page. Keys are entered in the browser only. Never retry a failed Higgsfield job silently. See [Higgsfield](references/workflow.md#use-the-owners-higgsfield-key).
- Use `get_project` and `list_takes` before editing an existing project. Record selected candidate IDs, hashes, geometry and timing. `create_frame_revision` selects the new revision; use a separate project for experiments or preserve/restore the original selection according to the user's task.
- Prefer the server default: omit `settings` in `generate_motions` unless pinning a requested hosted worker, in which case pass its discovered `settings` unchanged. Do not pass a 16GB execution profile to a 24GB server or invent LoRA/attention parameters—the worker must implement them. Read [references/workflow.md](references/workflow.md) for supported editing calls and limitations.

## Supplied views and acceptance

Studio owns motion compilation, generation and native processing; it contains no LLM or visual judge. Humans review in the UI. The calling agent orchestrates MCP and a fresh independent judge using the authoritative `unit-art-acceptance` skill. Read [view production](references/view-production.md) before creating or changing a project setup, removing directions/actions, or preparing durable retry records and evidence packets. If that acceptance skill or actual timed viewing is unavailable, retain the affected criteria as UNVERIFIED.

## Other directions from one image

When a direction has no art, a front-facing (S) or front three-quarter (SE, SW) reference can be turned in place to supply the rest:

1. Check `get_generation_config`: an available machine on `h3-24gb-standard` must report `features.last_frame`. Then `start_turnaround` with that view's current reference and up to four one-sided items (an axe in a hand, a pauldron on a shoulder).
2. Reconcile and `get_turnaround` until it settles. Read `suggested_picks`, check them with `inspect_turnaround_frames`, and save them with `save_turnaround_picks`. If a pick falls outside its region or out of turn order, it needs that heading's confirmation; ask the person before confirming one.
3. `create_view_draft` for each heading the person wants. Show them the draft image with its `placement_card` and `synthesized_parts`. The synthesized parts are invented, never matched to the source.
4. A draft binds only after `check_view_draft` records facing, equipment and identity as correct. Record that check, and bind with `preview_setup`/`apply_setup` (`{"revision_id": "draft-…"}` on that heading's view), only with the person's explicit go-ahead for that direction. Never judge a draft correct on your own. Mirrored or rotated pixels are never a direction.

## Generate a bounded result

Upload client bytes with `upload_subject_base64`; paths such as `/Users/...` passed to a remote tool are paths on the desktop, not the Mac. Inspect the prepared subject, then approve it when authorized by the task. Add Studio's recommended motions if absent; it adds six, so generate only the requested motion IDs. Do not re-add existing motions.

Queue one requested motion first. Use the built-in walk preset without an action override. For a specific weapon, supply only short action text, for example `actions={"attack":"swings each of the axes in their hand to attack"}`. Actions are at most 200 characters. Studio compiles the full prompt, including attack wind-up, strike, recovery, reference facing, fixed camera and framing. Add detail only to address an observed defect; keep prompt mechanics on the server. Read [references/workflow.md](references/workflow.md) for the minimal call sequence and what is automatic.

Read both `queued` and `failed`, then `wait_for_operations`. A timeout is not a failed render: inspect project operations before submitting again. Do not silently repeat a failed GPU generation; diagnose and report, then retry only within the user's generation budget. Interrupted server jobs are marked failed, not replayed. One desktop GPU job executes at a time.

## Extract and inspect

- Re-list takes after settlement. Derive alpha only when needed; re-list to find the derived candidate. Source videos, alpha videos and packed sheets are different artifacts.
- Use `suggest_motion_loop` with `mode="idle"` for a resting cycle, `mode="walk"` for a single reciprocal stride or `mode="attack"` for one windup/strike/recovery. This is the MCP equivalent of the UI’s separate loop-suggestion click, followed by a separate save. Specify 16 output frames and save with `cell_size=256` to match the fresh UI defaults; MCP suggestion alone defaults to 12 frames. Use 8 or 12 only when chosen for the deliverable. Use returned indices and timing; source indices are zero-based. The matching end is excluded to avoid a duplicate endpoint.
- Inspect the suggested phases and the surrounding source interval with `inspect_candidate_frames` (`limit` at most 12, paginate with `start`/`stride`). These use native server-side decoding, not browser seeking. Check last-to-first as well as internally wrapped indices.
- Idle: verify coherent subtle motion and a clean wrap; a frozen source is not a complete idle proposal. Walk: verify alternating leg progression and one complete stride. Attack: verify one strike, impact/follow-through, and a plausible recovery. Never reverse the entire attack to fake a loop or include a second hit accidentally. If recovery is missing, regenerate or deliberately curate a short return segment; the suggestion is not guaranteed to manufacture one.
- Match attack body size to the deliberately chosen **same-view** walk using `calibrate_motion_body`. Pick a comparable ready/resting pose, retain returned reference ID/hash, and pass its normalization unchanged except explicitly chosen padding/edge cleanup. One fixed transform applies to the whole clip. Do not fit each frame to its own weapon bounds.
- Manual framing and pivot controls apply to transparent idle, walk, attack and custom motions in the UI; MCP uses the existing revision `normalization` fields. Apply one fixed scale/translation across the clip and preserve shared expanded geometry rather than fitting individual frames.
- Save with `create_frame_revision`. 256/512 is the target cell size; the server may expand every cell uniformly to preserve overhang. Inspect the saved revision—not just the source—on light and dark backgrounds. Green spill can already exist in generated video. Compare source → alpha → normalized output before blaming one stage. Global green removal damages green skin.

## Deliver

Use `get_candidate_download` for PNG, WebM or GIF and retain `frame_geometry`. Place every animation at its exported pivot, not its canvas center. A null pivot means registration is not established; report it rather than claiming cross-animation alignment. Use the returned URL with the Mac's own downloader if files must land on the Mac; remote path-based download tools write on the MCP server's filesystem. Release/export tools are for an approved complete set, not a shortcut around missing required motions.

For directional sets, use supplied reference pixels for each declared view. Missing references remain draft. Obtain separately authorized reference preparation when needed; never infer, rotate or automatically mirror an absent view. View IDs organize supplied camera views and do not flip their pixels.

Return artifact links, selected source indices, frame count/timing, body reference and pivots, actual recipe/profile, and any unresolved clipping or seam defects. Technical quality findings are not visual acceptance. Separate static fidelity/pose continuity from observed playback cadence and in-game grounding; do not claim the latter from still images. Preserve the user's visual preference over an automated ranking.

## Server faults

When a reproducible server fault blocks the task and server changes are authorized, follow [references/server-maintenance.md](references/server-maintenance.md). Keep reusable generation behavior on the server and install the updated packaged skill for callers.
