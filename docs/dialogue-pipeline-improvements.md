# Dialogue pipeline: what the first end-to-end run taught us, and what to build next

Status: proposal for discussion (2026-09-16). Written after the first dialogue
trailer produced entirely through the Studio (cast → six spoken takes →
imported ending → scored → mastered), with no scripts touching the media.

## 1. What the product now does

- **Cast with voice references.** Characters carry a frozen 2–15 s clip of
  their own voice. Every take of that character is anchored to it.
- **Dialogue shots.** A shot with lines becomes a MiniMax H3 dialogue take:
  speech and lips are generated together from the character's voice reference.
  Take length is derived from the lines (syllable rate, punctuation pauses,
  turn gaps) on H3's 17k+5 frame grid at 24 fps. Nothing is compressed to fit.
- **Anchored continuity.** A continuation opens on the predecessor's real
  final frame (pinned at frame 0) and lands on its own plate (pinned at the
  last frame). The prompt is generated from the shot, not typed.
- **Sound survives assembly.** Every take keeps its native audio; joins
  stream-copy or crossfade both lanes; dialogue joins are hard cuts that drop
  the pinned guide frames; cues duck under speech; a swell lets a cue rise into
  what follows it; the programme is mastered by gain to −16 LUFS / −1.5 dBTP.
- **Worker contract.** Templates may declare `audio_slots`; the dialogue
  capability `video.minimax_h3.dialogue.v1` takes two plates and up to two
  voices. Autogrow inputs are written as dotted keys — the ComfyUI API silently
  drops the nested form, which is why earlier takes had no voices at all.

## 2. Measured outcome of the first run

- Transcript matched the screenplay word for word across nine lines.
- 16/16 utterances bound to the correct voice reference (speaker-embedding
  similarity), with clear margins.
- The five largest picture changes sat exactly on the five joins
  (0.0089–0.0123 mean frame difference); the accepted hand-finished trailer's
  own cuts measure 0.0086–0.0114. One join was 2.7 % over the 0.012 limit.
- First takes throughout; no re-rolls.
- The pre-made ending's music was found (by envelope cross-correlation) to be
  the same score file at position 43.0 s at the ending's first frame, so the
  bed under the dialogue hands over to the ending's own copy seamlessly.

## 3. What still needed a person (or a script) to know something

| Step | Today | Why it matters |
|---|---|---|
| Splitting lines into takes (≤15 s, ≤2 speakers) | Manual | The only authoring step that needs the frame grid in your head |
| Trimming a pre-made ending out of a master file | ffmpeg outside the product | Only out-of-band media step in the run |
| Aligning and levelling the score at the hand-over | Measured by script | A new video needs the same measurement |
| Checking joins and voice binding | Analysis scripts outside the product | The Studio does not judge its own output |
| Shipping a new template to a worker host | Hand-built image, manifest, profile | Every template change repeats it |

## 4. Proposed changes, in priority order

Each item names the surface, the change, and the acceptance test.

### 4.1 Auto-split dialogue into takes
- **Where:** `PUT /studio/movies/{id}/story`, `movies/dialogue.py`, Story UI.
- **Change:** a shot whose lines do not fit one take (>362 frames or >2
  speakers) is split at speaker turns, then sentence boundaries, into
  `shotNN-a`, `shotNN-b`… chained as continuations sharing the plate. Never
  mid-sentence; an unsplittable single sentence is the only remaining error.
- **Done when:** pasting a whole nine-line scene into one shot yields the
  six-take chain that was built by hand.

### 4.2 Verify what the runtime actually bound
- **Where:** worker `comfy.py` `execute()`.
- **Change:** before queueing, fetch `/object_info` for each template node and
  assert every autogrow key set by the template is accepted; after the job,
  read `/history` and assert the reference nodes executed. Fail the job with a
  named reason instead of returning a voiceless clip.
- **Done when:** a template with a nested (dropped) input fails on the worker
  instead of producing output.

### 4.3 Seam gate in assembly, surfaced in Review
- **Where:** `cpu_worker._assemble`, `Assembly` model, Review step.
- **Change:** compute the mean frame difference across each join after
  normalisation; record `assembly.joins[] = {at_seconds, diff, limit}` with
  limit 0.012. Assembly still succeeds; Review marks joins over the limit with
  "generate another take of shot N".
- **Done when:** the one over-limit join in the first run shows as flagged and
  the others green.

### 4.4 Voice-binding check per take
- **Where:** `reconcile_jobs` on completion; take card.
- **Change:** transcribe dialogue takes and compare against the lines; compute
  speaker similarity of each utterance to the character's reference. Store
  `take.checks = {transcript_match, voice_ok: {…}}`; the card shows
  "Words ✓ · Voices ✓" or the failing line.
- **Done when:** a take whose transcript diverges or binds to the wrong voice
  is marked before anyone listens.

### 4.5 Import with a trim
- **Where:** `POST /shots/{sid}/import` body `{asset_key, start_seconds?, end_seconds?}`.
- **Change:** the Studio cuts the segment (24 fps, audio kept) and stores the
  trimmed immutable clip.
- **Done when:** importing a range from a full master reproduces the ending
  clip that was extracted by hand.

### 4.6 Cue alignment and hand-over helpers
- **Where:** `PUT /soundtracks`, new `POST /soundtracks/align`, Music panel.
- **Change:** `align` cross-correlates a cue's loudness envelope against a clip
  in the cut and returns `{offset_seconds, correlation, level_delta_db}`; the
  panel fills offset, end and swell from it. Add a finish preset
  (score −29 LUFS ducked −8 dB, ambience −40 LUFS, master −16 / −1.5).
- **Done when:** choosing "align to the ending shot" produces the offset and
  swell that were computed by hand.

### 4.7 Ambience floor
- **Where:** `AudioTrack.loop: bool`; assembly; upload types.
- **Change:** a cue may loop under the whole movie so pauses never fall to
  silence. Accept `.ogg` uploads.
- **Done when:** a quiet pause holds at the ambience floor instead of near
  silence.

### 4.8 Build produces the full cut
- **Where:** `movies/build.py`.
- **Change:** the build's cut includes imported and motion shots in story
  order, not only generated takes.
- **Done when:** Generate movie on a story with an imported final shot
  assembles every shot.

### 4.9 Pacing and seed as first-class inputs
- **Where:** `Story.pacing {lead_in, turn_gap, sentence_gap, tail}`;
  `generate` accepts `seed`; capabilities report the defaults.
- **Change:** expose the timing constants (defaults 0.8 / 0.8 / 0.6 / 1.5 s)
  and record the seed per take.
- **Done when:** re-generating a take with its recorded seed reproduces it
  bit-for-bit on the same worker.

### 4.10 Worker release path
- **Where:** worker repo `deploy/hardened`.
- **Change:** one command builds the hardened agent image from a tagged main
  with the host overlay applied from a documented directory, writes the
  profile and manifest, restarts, and runs the verifier. Templates ship in the
  image from the package; the approved-templates directory becomes an
  allow-list rather than a copy.
- **Done when:** a new template reaches a worker host with one command and
  the verifier passes.

### 4.11 API ergonomics
- Upload grants advance `revision`; document it, and let append-only mutations
  (`opening`, `import`) tolerate a stale revision when no conflicting field
  changed.
- Asset endpoint: `?inline=1` returns duration/streams JSON so clients need not
  download to probe.
- Expose the shared-pool worker view under `/studio/`, API-key authenticated,
  so a script can confirm a capability is online.
- Capabilities include `pacing_defaults` and `master_target_lufs`.

### 4.12 Script → breakdown (built 2026-09-18)
- The in-app director (`movies/director.py`) turns a brief into a cast and
  chained dialogue shots through OpenRouter (any tool-capable model; default
  `anthropic/claude-sonnet-4.5`), forced into a JSON schema. Configured by
  the `DIRECTOR_API_KEY` repository secret.

## 5. The repeatable recipe today

1. Create the movie; add the cast with one voice clip each.
2. Add shots with lines, a plate image, and continuation links.
3. Generate movie.
4. Import any pre-made footage as its own shot; set the cut.
5. Add the score as a cue with offset, end and swell; assemble.

With 4.1, 4.3 and 4.4 in place, steps 2–5 need no measurement and no
judgement beyond listening.
