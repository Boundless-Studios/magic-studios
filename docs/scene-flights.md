# Scene flights

A **scene flight** is a camera journey across location images. Each **leg** is one
frame-pinned image-to-video clip: its first frame is the origin image and its last
frame is the destination image. When leg N lands on the image leg N+1 departs from,
the chained legs play as one continuous camera move.

This is the local replacement for Gaia's Scene Flight Lab legs, which Gaia renders
remotely through Runware (`require_seam_capable_model`, Seedance first/last frame).
Magic Studios renders the same contract on our own GPU with MiniMax H3 FL2VA:
`MiniMaxH3ImageToVideo` with both `first_frame` and `last_frame` bound.

## How it runs

A scene flight is stored as a movie of kind `scene_flight`; a leg is a shot whose
opening image is the origin and whose closing image is the destination; every
generation is a take. Persistence, owner limits, durable submission identities,
reconciliation, worker provenance, same-origin video delivery and assembly are the
Movie Studio pipeline's own. Flights do not appear in the movie list, and movie
story edits are refused on a flight.

| Setting | Value | Why |
| --- | --- | --- |
| Capability | `video.minimax_h3.text_to_video.v1` | The registered H3 FL2VA template Movie Studio already submits on both machine paths |
| Canvas | **768×448** | The nearest canvas to 16:9 with both sides multiples of H3's required 32 px, and the size Movie Studio already runs on the 16 GB desktop and the standard 24 GB worker. Both images are cover-cropped to it identically (a 1536×864 or 1376×768 plate loses about 2–4% of its width). Exact 16:9 at 32 px steps would be 1024×576, which no machine here has been validated at |
| Frame rate | 24 fps | H3's native clock |
| Length | 5–15 s in 0.5 s steps, default **5 s** | H3's trained clip range is 124–362 frames on its 17k+5 grid; 5 s is 124 frames (about 5.2 s). Gaia's own default is 5 s |
| Steps | **20**, `res_multistep` / `simple` | The base template on both paths. The desktop's 8-step LightX2V capability is bounded to 512×512 / 4 s and validated only for sprites, so flights never use it |
| Seed | Request `seed`, else derived from the take ID | Recorded on the take; a re-roll draws a new one |
| Negative prompt | Stated as "Avoid: …" in the prompt | The H3 graph uses `BasicGuider` with no negative conditioning |
| Audio | Whatever the executor's template does | The desktop omits audio decode; the standard worker keeps it. Gaia strips audio in its own transcode |

**Machine defaults.** On the desktop, `DesktopPool` runs the active preset's
`minimax_h3_text_to_video.template.json`. For `balanced` that is the W4A8 base graph
with the `last_frame` slot (20 steps, Comfy Kitchen attention when `ck` is set). The
legacy `turbo` preset rewrites steps at submission and offers no flights.
`GET /studio/generation-config` reports `scene_flight.available` and the recipe.

**Hosted workers.** Hosted jobs go through the outbound GPU worker pool with the
standard 24 GB requirement. The pinned pool templates (`outbound-gpu-worker-pool`
`e6691d7`, template revision 3) declare
`{"name": "last_frame", "node_id": "201", "required": false}` on the standard H3
template, with node `201` (`LoadImage`) wired to `MiniMaxH3ImageToVideo.last_frame`,
the same slot the desktop presets carry (`local/build_presets.py`). The coordinator
therefore reports `generation_available: true` whenever a worker pool is configured.
The coordinator decides this from its own pinned templates, so every hosted 24 GB
worker must run the same package before flights are dispatched. A worker still on an
older pool revision rejects the `last_frame` input and the take fails. Upgrading also
moves the standard (`h3-24gb-standard`) recipe fingerprint, so a profiled worker
redoes its profile check on the new package. The same graph (revision 2 plus the slot)
produced a 124-frame leg on the 4090 in 107 s with both ends within 1.3% of their pins;
see the proof note below.

## The repeatable process

1. **Create** a flight.
2. **Add images**: origin and destination plates (PNG, JPEG or WebP, at most 20 MiB
   and 25 megapixels). Images are normalized to PNG and stored immutably; the same
   bytes always give the same `image_key`.
3. **Set legs**: each names its origin and destination `image_key` and either a
   camera `prompt` or both place names. With names only, the prompt is Gaia's
   `scene_flight_leg` wording (version 1) clause for clause: the forward-drift
   handoff, the emptiness rule and the no-text guard.
4. **Generate a take** of one leg. Each request with a new `request_id` is another
   take (a re-roll); repeating a `request_id` is idempotent. The newest successful
   take is selected automatically, and any succeeded take can be selected instead.
5. **Poll** the flight. Reading it reconciles: it is what folds a finished GPU job
   into a take, measures it and records provenance. Nothing advances server-side
   without a reader (the web page polls every 4 s while work is running).
6. **Review**: each succeeded take reports `first_frame_diff` and `last_frame_diff`,
   the mean absolute greyscale difference (0–1) between its decoded first/last frame
   and the frozen origin/destination crops. They are evidence for review, never a
   gate.
7. **Download** the take, or **chain** the flight: every leg's selected take joined
   in leg order with hard cuts. Adjacent legs must share their boundary image. The
   chain is Movie Studio's assembly (768×432, 24 fps H.264, the 448 px canvas
   cropped to 16:9) and reports the measured difference at each join.

Editing a leg's images, prompt, avoid text or length marks its earlier takes stale
(cancelling one still running) and clears its selection.

## API

All routes need the normal Studio identity (session cookie, API key, or the desktop's
loopback identity). Mutations take `expected_revision`; a stale one answers `409` with
`{"detail": {"message": ..., "current_revision": N}}`.

| Route | Body | Returns |
| --- | --- | --- |
| `GET /studio/scene-flights/capabilities` | | `generation_available`, `image_types` and the recipe |
| `POST /studio/scene-flights` | `{"name"}` | Flight |
| `GET /studio/scene-flights` | | `[{flight_id, name, revision, leg_count, active_takes}]` |
| `GET /studio/scene-flights/{id}` | | Flight, reconciled |
| `POST /studio/scene-flights/{id}/images` | `{"content_base64", "content_type"}` | `{image_key, width, height, sha256, revision}` |
| `PUT /studio/scene-flights/{id}/legs` | `{"expected_revision", "legs": [Leg]}` | Flight |
| `POST /studio/scene-flights/{id}/legs/{leg_id}/takes` | `{"expected_revision", "request_id", "seed"?}` | Flight |
| `POST /studio/scene-flights/{id}/legs/{leg_id}/select` | `{"expected_revision", "take_id"}` | Flight |
| `POST /studio/scene-flights/{id}/legs/{leg_id}/takes/{take_id}/cancel` | `{"expected_revision"}` | Flight |
| `GET /studio/scene-flights/{id}/legs/{leg_id}/takes/{take_id}/video` | `?download=true` for an attachment | MP4 (Range supported) |
| `POST /studio/scene-flights/{id}/chain` | `{"expected_revision", "request_id"}` | Flight |
| `GET /studio/scene-flights/{id}/chain/video` | `?download=true` | MP4 |
| `GET /studio/scene-flights/{id}/assets/{key}` | | Redirect to an image (origin, destination, frozen pins, last frames) |

A **Leg** input: `leg_id` (`[A-Za-z0-9_-]`, ≤80), `origin_image_key`,
`destination_image_key`, and either `prompt` (≤1600 characters) or `origin_name` +
`destination_name` (with optional `origin_description` / `destination_description`);
optional `negative_prompt` (≤400), `name`, `duration_seconds` (5–15, 0.5 steps). At
most 24 legs. The provider prompt (wrapper plus leg wording) must fit H3's 2000
characters.

A **Flight**:

```json
{
  "flight_id": "…", "name": "…", "revision": 7, "generation_available": true,
  "recipe": {"capability_id": "video.minimax_h3.text_to_video.v1", "width": 768, "height": 448,
             "fps": 24, "steps": 20, "default_duration_seconds": 5.0, "min_duration_seconds": 5.0,
             "max_duration_seconds": 15.0, "duration_step_seconds": 0.5, "max_legs": 24},
  "images": ["outputs/movies/…/openings/<sha256>.png"],
  "legs": [{
    "leg_id": "harrowfen-miretrace", "index": 0, "name": "Harrowfen to Miretrace",
    "origin_image_key": "…", "destination_image_key": "…", "prompt": "…", "negative_prompt": "",
    "duration_seconds": 5.0, "selected_take_id": "…",
    "takes": [{
      "take_id": "…", "request_id": "…", "status": "succeeded", "stale": false, "error": null,
      "video_key": "outputs/movies/…/takes/<take>.mp4", "last_frame_key": "…",
      "origin_frame_key": "…first-768x448.png", "destination_frame_key": "…last-768x448.png",
      "prompt": "<the exact provider prompt>", "seed": 42, "width": 768, "height": 448, "fps": 24,
      "length_frames": 124, "steps": 20, "capability_id": "video.minimax_h3.text_to_video.v1",
      "executor_preset": "balanced", "duration_seconds": 5.17,
      "first_frame_diff": 0.01, "last_frame_diff": 0.02,
      "job_id": "…", "completed_by_worker_id": null, "completed_at": null,
      "output_sha256": "…", "output_byte_length": 1234567
    }]
  }],
  "chain": {"request_id": "…", "status": "succeeded", "video_key": "…", "error": null,
            "joins": [{"index": 0, "at_seconds": 5.17, "diff": 0.004, "limit": 0.012}]}
}
```

Take `status` is `waiting`, `submitting`, `running`, `succeeded`, `failed` or
`cancelled`. `duration_seconds` is the requested length until the clip exists and its
measured length afterwards. `generation_available: false` means the server has no
executor that pins a last frame; generation answers `503`.

### Calling it from Gaia

One leg, from Gaia's `FlightLeg` (origin plate → destination plate):

1. `POST /studio/scene-flights` once per Gaia flight; keep the `flight_id`.
2. `POST …/images` with each plate's bytes (Gaia already reads them for Runware's
   data URIs). Keep the returned keys; repeated plates dedupe.
3. `PUT …/legs` with one entry per Gaia leg. Send Gaia's rendered
   `scene_flight_leg` text as `prompt`, so the Langfuse-managed wording stays Gaia's.
4. `POST …/legs/{leg_id}/takes` with a fresh `request_id` per attempt. Unlike
   Runware there is no billing to reserve; owner concurrency limits still apply
   (`429`).
5. Poll `GET …/{id}` until the take is terminal (a 5 s leg is minutes, not seconds),
   then `GET …/takes/{take_id}/video` and run it through Gaia's existing
   `transcode_leg`. The first frame is the origin crop, so Gaia's poster (the origin
   plate) still matches.

Differences from the Runware path Gaia should expect: 768×448 instead of 864×496;
about 5.2 s instead of exactly 5 s; the plate is cover-cropped by a few percent at
the left and right edges; and on the standard worker the clip carries an audio track.

## Web app and MCP

**New project → Scene flight** opens `/flights/{id}`: add location images, plan legs
(origin and destination pickers, place names, camera direction, avoid text, length),
generate or re-roll a leg, compare takes with their seam measurements, select one,
download it, and chain the flight.

MCP tools: `create_scene_flight`, `add_scene_flight_image` (server path or base64),
`set_scene_flight_legs`, `generate_scene_flight_take`, `generate_scene_flight_leg`
(create, add both images, append the leg and start a take in one call),
`select_scene_flight_take`, `wait_for_scene_flight`, `chain_scene_flight` and
`download_scene_flight_video`.

## Verification

- Backend: `uv run --extra test pytest -q tests/flights`.
- MCP: `uv run --directory mcp --all-extras pytest -q tests/test_scene_flights.py`.
- Web: `npm test -- --run src/features/flights` from `web`.
- The local studio's fixture pool renders a crossfade from the first to the last
  image, so the whole flow (including seam measurement and chaining) runs without a
  GPU.

Live GPU evidence is recorded in the proof note `scene-flight-2026-10-18.md`, preserved in `docs-proofs.zip` in the private `art-archive-2026-11-02` release.
