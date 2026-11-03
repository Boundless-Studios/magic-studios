# Synchronized video and sound production in Magic Studios

Status: agreed direction, 2026-09-29. Operator decisions: Magic Studios is where the
whole process lives, and ComfyUI on the outbound worker pool is the engine. Build the
minimum needed to make a real film first, and let that film decide the rest.

## Goal

A general, repeatable way to produce films whose picture, speech, lips and sound
stay in sync, without per-film scripts or manual patching. Every step is driven
through Magic Studios' API and MCP tools and recorded in the movie's revision
history. The operator's listening pass is the only acceptance; automatic checks
decide what reaches it.

## Principles

- **Sync is born, not repaired.** Dialogue shots are H3 audio-visual takes, so
  speech and lips are generated together from pinned voice references. Speech is
  never retimed, stretched or dubbed after generation.
- **Silence by construction.** Shots without dialogue are muted in the build, so
  stray generated speech cannot reach the cut.
- **A failed take is replaced, never patched.** Never mute a generated line while
  its speaking animation stays visible.
- **Picture and dialogue cut together.** Sound design (ambience, music, effects) is
  layered on top as cues, not spliced into takes.
- **Evidence is not acceptance.** Recognition, voice match and lip-sync scores
  filter takes. They do not certify acting or mix quality.

## What already exists (magic-video-editor @ 073a961)

- Movies, shots and takes with revision history, compare-and-swap writes,
  continuation lineage, staleness and continuity review (`movies/`).
- H3 dialogue takes with character voice references (#204), plate pinning and
  24 fps timing (`movies/dialogue.py`); H3 motion takes at the native 24 fps clock
  (#203).
- Build orchestration, automatic muting of no-dialogue shots (#196), 6-frame
  dissolves between chained dialogue takes (#200).
- The FFmpeg assembler: picture-locked audio, up to 8 cues with fades and ducking,
  −16 LUFS master, and a seam measure at each join (`cpu_worker.py`).
- REST API with owner API keys (`movies/routes.py`, `api.md`).
- An evaluation rubric with thresholds (`docs/video-evaluation-rubric.md`), run out
  of band today.

## Phases

### Phase 1 — make a real film possible (minimum)

1. **Agent-drivable movies** (plan: `superpowers/plans/2026-09-29-movie-agent-tools.md`).
   Movie MCP tools covering the whole flow (create, cast, voice, story, openings,
   build, wait, select, cut, soundtracks, assemble, release, download) and
   structured 409 revision errors. `wait_for_movie` advances work through the
   existing reconcile route. A server-side scheduler is deferred: it is an
   infrastructure change, and an agent-driven film does not need it.
2. **Worker package ships the current templates** (separate, security-reviewed PR;
   does not block V1). The Studio worker wheel is built from
   outbound-gpu-worker-pool 630ef2d, which predates the dialogue template, audio
   slots (#21) and 362-frame motion (#22). The bump also brings the pool's #20
   security release, which needs Studio's enrolment to call explicit worker
   enrollment, the pool status router to take an admin-only `authorize_admin`, and
   an operator audit of existing production worker bindings before deploy
   (`outbound-gpu-worker-pool/docs/security-upgrade.md`). The same PR lifts the
   10 s shot cap to the native limit (~15 s), fixes the audition payload (16 fps,
   145 frames) and makes the worker template release one command
   (`dialogue-pipeline-improvements.md` §4.10). V1 runs on the desktop worker,
   whose templates are already installed.

### Phase 2 — first film: V1 "Touchdown" (CINDER, 45 s)

Produce the colony video V1 (`cinder-poc/docs/story/colony-videos.md`) entirely
through Phase 1's tools. Cast: Mara and Ivo (pinned references from the intro and
the L3 film) plus a survey technician (voice locked before generation). It
contains silent shots, a two-speaker exchange, a third speaker, a frame-exact cue
(the glass harmonic) and a continuous score and wind bed. Record every defect
found, with its movie revision, take and time.

### Phase 3 — driven by V1's findings

The candidate features, to be ordered by what V1 actually hits:

- **Per-take automatic checks.** An analysis capability on the worker pool that
  runs the rubric on every take: word error against the script, voice activity
  outside dialogue, voice match to the reference, audio–video offset and mouth lag,
  within-take jumps, loudness. Results live on the take. A release needs a pass or
  a recorded waiver.
- **Multiple candidates per shot** in one request, with a recorded seed.
- **Audio lanes.** Typed lanes (dialogue, ambience, music, effects), a looping
  ambience floor, cues on picture frames, separate audio in/out points (J/L cuts).
- **Frame-exact editing and delivery.** Frame-indexed in/out points; fix the
  768×448 → 768×432 pillarbox; subtitles from the dialogue plan; a release sha256.
- **Selective audio cleanup**, if muting whole shots proves too blunt.

## Done means

V1 plays in CINDER from a pinned Magic Studios release, the operator has listened
and accepted it, and the next colony video can be made with the same tools and no
new scripts.
