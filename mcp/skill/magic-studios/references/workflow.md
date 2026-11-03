# Tool calls in order

For animating a character, making a movie and flying through a scene.

Use named arguments, and read each tool's schema first. `P` is the project ID. Use the motion and take IDs the server returns rather than assuming them.

## Generate one motion

```text
get_generation_config()
generate_motions(project_id=P, motion_ids=["walk"])
wait_for_operations(project_id=P)
list_takes(project_id=P, motion_id="walk")
```

Omit `settings` to use the default machine. To run on the user's Higgsfield account, pass `settings=config["providers"]["higgsfield"]["settings"]` unchanged, or change only `provider_parameters.higgsfield_model` to another listed model, keeping `duration_seconds` inside that model's range. Don't add machine fields to a Higgsfield request; the server rejects them.

Omit `actions` for a walk. For an attack with a particular weapon, send one short line:

```text
generate_motions(project_id=P, motion_ids=["attack"],
                 actions={"attack": "swings the axe in their right hand"})
```

The server already adds wind-up, one strike, follow-through, recovery, fixed facing, camera and framing. Long prompts repeat those rules and can conflict with them.

## Save a walk loop

```text
suggest_motion_loop(project_id=P, motion_id="walk", candidate_id=TAKE,
                    mode="walk", target_frame_count=16)
inspect_candidate_frames(project_id=P, motion_id="walk", candidate_id=TAKE,
                         start=START, limit=12, stride=1)
create_frame_revision(project_id=P, motion_id="walk", source_candidate_id=TAKE,
                      ordered_frames=INDICES, frame_duration_ms=MS,
                      loop_policy="loop", target_frame_count=16, cell_size=256)
```

Use the indices and timing the suggestion returns. Source indices start at zero, and the matching end frame is left out so the loop does not repeat a frame. For an idle, use `mode="idle"`; a frozen take returns an incomplete suggestion with no frames.

## Match an attack to the walk

```text
suggest_motion_loop(project_id=P, motion_id="attack", candidate_id=ATTACK,
                    mode="attack", target_frame_count=16)
calibrate_motion_body(project_id=P, motion_id="attack", candidate_id=ATTACK,
                      source_frame=READY_FRAME, reference_motion_id="walk",
                      reference_candidate_id=WALK, reference_frame=0)
create_frame_revision(project_id=P, motion_id="attack", source_candidate_id=ATTACK,
                      ordered_frames=INDICES, frame_duration_ms=MS,
                      loop_policy="loop", target_frame_count=16, cell_size=256,
                      normalization=RETURNED_NORMALIZATION)
```

- **Pick both frames on purpose.** `READY_FRAME` is a resting attack pose. `WALK` is the walk take the user selected, in the same view.
- **Keep the walk selected while you export the attack.** Changing it makes the calibration stale.
- **Check the calibration frames.** A weapon crossing the body can skew the measurement.
- **One transform per clip.** It applies to the whole clip, and a wider attack canvas is expected.
- **Don't fake the recovery.** Never reverse a whole attack to make a loop. If the recovery is missing, say so and offer to generate again.

## Several directions

A project can hold several views, one per direction. Call `get_setup_capabilities` and `get_setup`, then `preview_setup` and `apply_setup` to declare the views and their animations. Each view needs its own reference image. The studio never mirrors or invents a direction the user did not supply.

## Make a movie

```text
get_movie_capabilities()                                     # check dialogue_available, max_batch_size
create_movie(name="Heist")
set_movie_story(movie_id=M, shots=[
    {"shot_id": "s1", "name": "Rooftop", "prompt": "The thief crouches at the edge and looks down"},
    {"shot_id": "s2", "name": "Jump", "prompt": "The thief leaps to the next roof", "continuation_of": "s1"},
])
upload_movie_asset(movie_id=M, path="rooftop.png")          # returns asset_key
set_shot_opening(movie_id=M, shot_id="s1", asset_key=KEY)
build_movie(movie_id=M)
wait_for_movie(movie_id=M)                                   # call again if it times out
get_movie(movie_id=M)
generate_takes(movie_id=M, shot_ids=["s2"], select_when_ready=False)  # only to retry; at most batch_size shots
wait_for_movie(movie_id=M)
select_movie_take(movie_id=M, shot_id="s2", take_id=TAKE)
build_movie(movie_id=M)                                      # recuts from the selected takes and assembles
wait_for_movie(movie_id=M)
release_movie(movie_id=M, name="v1")
download_movie_release(movie_id=M, release_id=RELEASE, destination="heist-v1.mp4")
```

Selecting a take doesn't change the saved cut, and assembly renders the cut, so rebuild after a retry, or use `set_movie_cut` with the new `take_id` and then `assemble_movie`. Shots with `dialogue` become voiced takes, but only when `dialogue_available` is true. Set the cast with `set_movie_cast` and attach voice clips with `attach_movie_voice` first, and give every dialogue shot an opening image, even one that continues another shot. Motion shots can carry `camera` (one move, such as "The camera pushes in with small amplitude at slow speed"), `still` (what must not change) and `beats`. `set_movie_cut` replaces the whole cut, and clips are muted only where `mute` is true. `set_movie_soundtracks` adds WAV or MP3 cues uploaded with `upload_movie_asset`.

## Fly through a scene

One leg in one call. The helper creates the flight and uploads both images before it starts the take, so check first:

```text
get_scene_flight_capabilities()                              # generation_available: the server can make flights
get_movie_capabilities()                                     # generation_available: this account can start takes
generate_scene_flight_leg(origin_path="street.png", destination_path="square.png",
                          prompt="The camera glides down the street into the square")
wait_for_scene_flight(flight_id=F)
download_scene_flight_video(flight_id=F, destination="street.mp4", leg_id=LEG, take_id=TAKE)
```

Several legs, chained:

```text
get_scene_flight_capabilities()                              # generation_available: the server can make flights
get_movie_capabilities()                                     # generation_available: this account can start takes
create_scene_flight(name="Canyon")
add_scene_flight_image(flight_id=F, path="river.png")       # returns image_key
add_scene_flight_image(flight_id=F, path="arch.png")
add_scene_flight_image(flight_id=F, path="falls.png")
set_scene_flight_legs(flight_id=F, legs=[
    {"leg_id": "a", "origin_image_key": RIVER, "destination_image_key": ARCH,
     "prompt": "The camera flies low along the river toward the arch"},
    {"leg_id": "b", "origin_image_key": ARCH, "destination_image_key": FALLS,
     "prompt": "The camera rises through the arch toward the falls"},
])
generate_scene_flight_take(flight_id=F, leg_id="a")         # at most three legs at a time
generate_scene_flight_take(flight_id=F, leg_id="b")
wait_for_scene_flight(flight_id=F)
select_scene_flight_take(flight_id=F, leg_id="a", take_id=TAKE_A)   # optional; the newest success is selected
chain_scene_flight(flight_id=F)
wait_for_scene_flight(flight_id=F)
download_scene_flight_video(flight_id=F, destination="canyon.mp4")
```

Each leg ends exactly on its destination image, and the next leg starts on it, so the chain plays as one move. Changing a leg makes its earlier takes stale.

## Gather audio

```text
get_audio_capabilities()                                     # free_sounds_available, processing_available
create_audio_set(name="Glass")
set_audio_set(set_id=S, cues=[
    {"cue_id": "crack", "name": "Glass crack", "kind": "sfx", "prompt": "glass crack", "keep": 2},
])
search_free_sounds(set_id=S, query="glass crack", max_seconds=5)   # CC0 only unless the set allows CC-BY
import_free_sound(set_id=S, cue_id="crack", source_id="101")       # a Freesound preview, licence recorded
upload_audio_candidate(set_id=S, cue_id="crack", path="my-crack.wav")
# The user listens and keeps or rejects in the web app.
select_audio_candidates(set_id=S, cue_id="crack", candidate_ids=[A, B])
get_audio_readiness(set_id=S)
release_audio_set(set_id=S, name="v1")
download_audio_release(set_id=S, release_id=RELEASE, destination="glass-v1.zip")
```

The zip holds `audio/` (loudness renditions, or the originals for public accounts), `originals/`, `manifest.json` with every file's licence, author, source and hashes, and `CREDITS.md`.
