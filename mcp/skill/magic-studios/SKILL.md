---
name: magic-studios
description: Drive Magic Studios through its MCP server. Animate a character from one image into game sprites (idle, walk, attack, custom actions), make short movies from shots, fly a camera between location images, and gather licensed sound effects and music from free libraries into reviewed audio packs. Inspect frames, save loops, assemble, release and export. Use when the user has a Magic Studios account and the magic-studios MCP server connected.
---

# Magic Studios

Magic Studios is built for agents: everything in the studio is an API call. It does four things:

- **Animate a character:** one character image becomes game-ready sprite animations, exported as PNG frames with a JSON manifest.
- **Make a movie:** shots generated from opening images are cut together, with music and narration, into an MP4.
- **Fly through a scene:** camera moves between location images chain into one continuous shot.
- **Gather audio:** free sounds (CC0 by default) and the user's own files are collected per cue, reviewed, and released as a zip with a licence manifest and credits.

This skill drives the studio through the `magic-studios` MCP server, which calls the hosted API at `https://api.magicstudios.ai` with the user's API key.

Tool discovery and the schemas the server returns are authoritative. The names below are the server's tool names.

## Before you start

- The user needs a Magic Studios account and an API key from the web app: account menu → **API keys**. The key goes in the MCP server's `MAGIC_STUDIOS_API_KEY` setting. Never ask for it in chat and never pass it to a tool.
- Some things need the browser and cannot be done here: creating or revoking API keys, attaching a machine, saving a Higgsfield key, and deleting the account.
- Generation runs on a machine the user attached or on their own Higgsfield account. Call `get_generation_config` before generating. If it lists no available machine and no saved Higgsfield key, tell the user to set one up on the **Your machines** page.
- Higgsfield bills the user's own Higgsfield account for each attempt. Use it only when the user asks for it or agrees, and never retry a failed Higgsfield job without asking.

## Animate a character

1. `create_project(name)`.
2. Supply the character. Use `upload_subject_base64(project_id, image_base64, content_type)` for a finished image, then `approve_subject`. Or upload references with `upload_reference` and call `generate_subject(project_id, reference_keys, guidance)`, wait, then approve.
3. Add motions. `add_recommended_motions` adds idle, walk, attack, guard, cast and hurt. Generate only the ones the user asked for.
4. `generate_motions(project_id, motion_ids=["walk"])`. Omit `settings` to use the default from `get_generation_config`. For Higgsfield, pass `providers.higgsfield.settings` from it unchanged. For a specific action, pass short text such as `actions={"attack": "swings the axe in their right hand"}` (at most 200 characters); the server writes the full prompt.
5. Read both `queued` and `failed` in the result. A motion in `failed` started nothing.
6. `wait_for_operations(project_id)`. A timeout is not a failed render. Check the project's operations before submitting again, and never resubmit work that is still running.

Generate one motion first. Submit more after it settles; the account limits how many jobs run at once.

## Other directions from one image

When a direction has no art, a front-facing (S) or front three-quarter (SE, SW) reference can be turned in place to supply the rest:

1. Check `get_generation_config`: an available machine on `h3-24gb-standard` must report `features.last_frame`. Then `start_turnaround` with that view's current reference and up to four one-sided items (an axe in a hand, a pauldron on a shoulder).
2. Reconcile and `get_turnaround` until it settles. Read `suggested_picks`, check them with `inspect_turnaround_frames`, and save them with `save_turnaround_picks`. If a pick falls outside its region or out of turn order, it needs that heading's confirmation; ask the person before confirming one.
3. `create_view_draft` for each heading the person wants. Show them the draft image with its `placement_card` and `synthesized_parts`. The synthesized parts are invented, never matched to the source.
4. A draft binds only after `check_view_draft` records facing, equipment and identity as correct. Record that check, and bind with `preview_setup`/`apply_setup` (`{"revision_id": "draft-…"}` on that heading's view), only with the person's explicit go-ahead for that direction. Never judge a draft correct on your own. Mirrored or rotated pixels are never a direction.

## Find and save a loop

1. `list_takes(project_id, motion_id)` after the work settles.
2. If the take still has a green backdrop, `derive_candidate_alpha`, then list takes again to find the new take.
3. `suggest_motion_loop(project_id, motion_id, candidate_id, mode, target_frame_count=16)` with `mode` set to `idle`, `walk` or `attack`. The suggestion is a heuristic, not a visual check. A result with `status: "incomplete"` or no frames is not a loop: report it and inspect the take.
4. `inspect_candidate_frames(project_id, motion_id, candidate_id, start, limit, stride)` shows decoded frames as images, at most 12 at a time. Look at the suggested frames and the wrap from the last frame back to the first.
5. `create_frame_revision(project_id, motion_id, source_candidate_id, ordered_frames, frame_duration_ms, loop_policy, target_frame_count=16, cell_size=256)`. Saving selects the new revision.

For an attack, keep the body the same size as the walk. Call `calibrate_motion_body` with a resting attack frame and the selected walk take as the reference, then pass its `normalization` unchanged to `create_frame_revision`. Details are in [references/workflow.md](references/workflow.md).

## Deliver an animation

- `get_candidate_download(project_id, motion_id, candidate_id, format)` returns the download path of a PNG sprite sheet, WebM or GIF, plus the frame geometry, including the pivot. The path works only with the user's API key or signed-in browser, so give it to the user to open in the studio. Place each animation at its pivot, not at the canvas centre. Report a null pivot instead of guessing.
- For a complete set: `run_quality`, `approve_set`, `create_release(project_id, name)`, `export_release(release_id, "png-json")` and `download_export(export_id, dest_path)`. Every required motion needs a selected take before the set can be approved.

## Make a movie

Call `get_movie_capabilities` first: it says what this account can run. Start takes only when `generation_available` is true. A public account needs its own machine; an invited account can also run on the studio's shared machines.

1. `create_movie(name)`.
2. Dialogue and voices only work when `dialogue_available` is true. Public accounts can't upload voices yet. If it is true and the movie has speaking characters, `set_movie_cast(movie_id, characters)`, then `attach_movie_voice(movie_id, character_id, path)` with a 2–15 second WAV or MP3.
3. `set_movie_story(movie_id, shots)`. Each shot has `shot_id`, `name` and a `prompt` of one physical action. Set `continuation_of` to start from the previous shot's last frame. Changing a shot makes its takes stale, and changing `style` makes every take stale, so leave `style` out unless you mean to change it. `brief` is only planning text.
4. Give these shots an image with `upload_movie_asset(movie_id, path)`, then `set_shot_opening(movie_id, shot_id, asset_key)`:
   - every shot that doesn't continue another;
   - every shot with `dialogue`, even one that continues another, because a dialogue shot ends on its plate.
5. `build_movie(movie_id)`, then `wait_for_movie(movie_id)`. The build makes a take for each shot, cuts them in story order and assembles the movie. Work only moves forward while `wait_for_movie` runs; call it again if it times out.
6. Review the takes in `get_movie`. For a better take, `generate_takes(movie_id, shot_ids, select_when_ready=False)`, then wait.
   - The tool picks the new take as soon as it is ready unless `select_when_ready` is false, and picking a take makes every shot that continues from it stale.
   - List at most the movie's `batch_size` shots; `get_movie` shows it, and `set_movie_story` can raise it up to `max_batch_size`.
   - Once the user has reviewed the take, `select_movie_take(movie_id, shot_id, take_id)`.
7. Selecting a take doesn't change the saved cut, and assembly renders the cut. After a retry, call `build_movie` again: it reuses each shot's selected take, saves a new story-order cut and assembles it. To make your own cut instead, `set_movie_cut(movie_id, clips)` with the take IDs you want, optionally `set_movie_soundtracks(movie_id, tracks)` with WAV or MP3 cues from `upload_movie_asset`, then `assemble_movie(movie_id)` and `wait_for_movie`.
8. `release_movie(movie_id, name)`, then `download_movie_release(movie_id, release_id, destination)`.

## Fly through a scene

Check two things before creating a flight:
- `get_scene_flight_capabilities`: its `generation_available` says whether this server can make scene flights at all.
- `get_movie_capabilities`: its `generation_available` says whether this account can start takes, under the same rules as movies.

If either is false, say so before creating the flight or uploading images.

1. For one move, `generate_scene_flight_leg(origin_path, destination_path, prompt)` creates the flight, adds both images, adds the leg and starts a take in one call. Give a `prompt`, or both `origin_name` and `destination_name`.
2. For several legs, `create_scene_flight(name)`, then `add_scene_flight_image(flight_id, path)` for each image; each returns an `image_key`. Then `set_scene_flight_legs(flight_id, legs)`, where each leg has `leg_id`, `origin_image_key`, `destination_image_key` and a prompt. Adjacent legs must share their boundary image to chain.
3. `generate_scene_flight_take(flight_id, leg_id)` for up to three legs at a time; a flight runs at most three takes at once. `wait_for_scene_flight(flight_id)`, then start the next legs. Calling it again on a leg re-rolls it, and the newest successful take is selected. To choose another, `select_scene_flight_take(flight_id, leg_id, take_id)`.
4. `chain_scene_flight(flight_id)` joins the selected takes in order. Wait, then `download_scene_flight_video(flight_id, destination)` for the chain, or pass `leg_id` and `take_id` for one take.

## Gather audio

Call `get_audio_capabilities` first. `free_sounds_available` says whether Freesound search works on this server, and `processing_available` whether candidates get a loudness rendition and measurements (never for public accounts).

1. `create_audio_set(name)`, then `set_audio_set(set_id, cues)`. Each cue has `cue_id`, `name`, `kind` (`sfx`, `ambience`, `music` or `line`), a `prompt` describing the sound, and `keep`, the number of variants to ship. Leave `licence_policy` alone unless the user agrees to CC-BY sounds, which need credit wherever they are used.
2. `search_free_sounds(set_id, query)` for each cue, then `import_free_sound(set_id, cue_id, source_id)` for the promising ones. Imports are Freesound previews; the user can replace one with its original through `replace_audio_original`. `upload_audio_candidate(set_id, cue_id, path)` adds the user's own WAV or MP3.
3. Listening is the user's judgement. Show them the candidates (`get_audio_set`) and let them keep or reject in the web app. A review you give with `review_audio_candidate` is recorded as an API key's, and releases say so.
4. `select_audio_candidates(set_id, cue_id, candidate_ids)` with kept candidates, `get_audio_readiness(set_id)`, then `release_audio_set(set_id, name)` and `download_audio_release`.
5. `send_audio_to_movie(set_id, candidate_id, movie_id)` adds a kept candidate to a movie as a soundtrack cue. CC-BY sounds are refused, because movie releases carry no credits yet.

Audio generation (voices, sound effects, music) is not available yet.

The call sequences for all four are in [references/workflow.md](references/workflow.md).

## Report

Give the user the links and downloaded files, the take IDs, the chosen frame indices and timing for animations, and any warnings the server returned. Quality findings are not visual acceptance. Say what you looked at, and leave the judgement of how it looks to the user.
