# Movie Studio

Choose **Movie** when creating a project. Existing movies can be reopened from
the project home screen. Movie data is separate from animation projects and releases.

1. **Movie:** write the brief and motion prompts, upload starting images or compose
   them, and arrange the right-hand sequence. A single shot editor shows the still
   image until generation finishes, then plays its video in the same place.
   There is no opening-frame approval step. **Generate movie** saves the draft,
   reuses ready takes, generates missing shots and stitches them in story order.
   **Generate another take** saves prompt changes and replaces the current take
   when the new generation succeeds, keeping earlier takes in history. Independent
   shots run across eligible GPU workers; continuations use their predecessor's
   selected final frame. Existing image and clip imports remain available.
   Shots without dialogue are sent to MiniMax H3 in its own image-to-video format:
   the starting image is referenced and sets the medium and look, so the style text
   is not repeated. Under **Motion direction**, **Camera** takes one move written as
   type, amplitude and speed ("The camera pushes in with small amplitude at slow
   speed", or "The camera holds a Static Shot"); left empty, the action text's own
   camera wording stands. **Stays still** names what in the starting image must not
   change. Keep the action to one physical event. Timed beats
   (`beats: [{at_seconds, action}]`) are written through the API and agent tools and
   listed there; beats past a shortened shot's end are flagged and dropped on save.
2. **Cut:** adjust ordering, trims, music and narration, then assemble the movie.
3. **Release & export:** review the assembled movie and export an immutable release.

The compact toolbar stays visible above the editor. Reference uploads and take
history expand when needed. Save edits before leaving the workspace; generation
also saves the current draft. Concurrent edits are protected by revision checks.
Old `/shots` links redirect to the unified `/story` movie workspace.

## Iterating with a small GPU budget

The batch budget counts all active takes, including waiting continuations.
Independent scenes submit concurrently. Attempts are recorded before dispatch,
and ambiguous submission retries reuse their durable identities. Completed
clips remain available when another clip fails. Select a shot again to request
a distinct new take. Replacing an upstream take marks dependent footage for
continuity review; it does not automatically spend another generation. An
explicit continuity acceptance retains the original generation provenance.

Current generation uses the registered MiniMax H3 capability at 768×448,
24 fps (H3’s native clock, which keeps the picture and its generated sound in
step), 20 steps; both dimensions are multiples of the model’s required 32 pixels.
Attached starting images are preserved alongside an immutable crop matching that canvas.
Continuation inputs use the selected take’s actual last frame. Choose a requested
length of 1.5–15 seconds in half-second steps. H3 rounds the frame count up to
its 17k+5 temporal grid, whose trained ceiling is 362 frames: a 15-second request
is 360 frames and runs 362 (about 15.1 seconds), so 15 seconds is the longest
request. Completed takes display their measured duration (for example, a
requested four seconds is 107 frames, about 4.5 seconds). Takes submitted before
the 24 fps clock keep their original 16 fps recipe on retry. Voice auditions use
the same clock: nine seconds, 226 frames. Cuts and continuations
use that measured clip, with no silent truncation. Imported clips also use their
measured duration (up to ten minutes). Final assembly is 768×432 H.264,
24 fps, with AAC when a soundtrack is present. New movie sequences default to 0.5-second dissolves between shots. In Cut,
choose a cut, a smooth dissolve or a one-second dissolve for each join. Overlaps
shorten the movie and are capped at half of either neighbouring trim (rounded
down to a 24 fps frame). Soundtracks use the resulting movie timeline. Existing
saved cuts and releases retain their original transitions; use selected takes or
change a join and assemble again to apply dissolves without GPU generation.
Dissolves smooth the edit between scenes; use a continuation shot to generate
continuous action from the previous take’s final frame. Configurable resolution
and upscaling are not included.

Reconciliation runs with API requests and while the movie is open. Reloading
resumes durable pending work; already-submitted GPU jobs remain owned by the
pool. CPU assembly uses eligible network CPU workers, with bounded local processing
as a fallback when none are available.

## Runtime

`/studio/movies` uses the existing verified Studio actor dependency. PostgreSQL
startup applies `017_movies.sql` under an advisory lock. The current pool
configuration supplies capability matching, leases, cancellation and output
attestation. Movie storage and active generations count toward the same owner
caps as animation work. Opening plates and clips keep their original worker
routing; completed clips expose job and worker details for troubleshooting.
With no pool configured, the UI supports planning and import but
reports generation unavailable. Opening composition uses the configured composition provider when present,
otherwise the existing FLUX image capability in the GPU pool. Pooled plates are
generated at 1024×1024, then cropped to the movie frame; review the actual crop
before approving it. The local studio uses deterministic fixture clips and
does not contact a GPU or paid provider.

The in-app director is enabled with `MAGIC_VIDEO_EDITOR_DIRECTOR_API_KEY`
(an OpenRouter API key; `MAGIC_VIDEO_EDITOR_DIRECTOR_MODEL` picks the model,
default `anthropic/claude-sonnet-4.5`, and `MAGIC_VIDEO_EDITOR_DIRECTOR_URL`
points at any other OpenAI-compatible chat-completions endpoint with function
calling). Given a brief — a synopsis, a screenplay or a session transcript — it
returns a cast (appearance, voice, audition line) and shots with their spoken
lines, opening frame, action and soundscape, chained as continuations; shots
that carry more than one take of dialogue are split at speaker turns and
sentence boundaries. In production the key is the `DIRECTOR_API_KEY`
repository secret, mounted on the API service through Secret Manager. When it
is absent the hosted planner below, or manual planning, applies.

The optional hosted planner is enabled with
`MAGIC_VIDEO_EDITOR_MOVIE_PLANNER_URL` and
`MAGIC_VIDEO_EDITOR_MOVIE_PLANNER_AUDIENCE`. It is an HTTPS service accepting
`brief`, `style`, `shot_count`, duration limits and director instructions, returning
`{ "shots": [...] }` using the `ShotInput` schema. Requests carry a Google identity
token for the configured audience. The adapter validates count, durations, prompt
limits and acyclic ordered dependencies. Suggestions are editable drafts and
never submit GPU jobs. A compatible planner service must be provided separately;
manual shot planning works without it.

Limits: six active takes at most per movie, 60 shots, 120 cut clips, eight audio
cues, 256 MiB per imported clip, 50 MiB per audio cue and 512 MiB total assembly
source bytes. The browser uses a conservative 40-shot authoring limit. Media is
validated and copied to immutable project-scoped keys before use. Assembly has
a 240-second CPU deadline and may need a shorter cut if it exceeds that budget.

Browser MP4 previews and release downloads use authenticated same-origin delivery
(`?proxy=true` on the asset or release-download endpoint). This avoids requiring
cross-origin storage access for saving or reviewing footage. Delivery preserves
the exact asset allowlist and frozen release key, supports HTTP Range requests,
and uses private, non-cacheable responses. Each API process stages at most one
video at a time, bounded to 512 MiB; temporary files are removed after delivery
or disconnect. Clients omitting the flag retain signed-URL redirects.

## Verification

- Backend: `PYTHONPATH=src .venv/bin/python -m pytest -q`.
- PostgreSQL checks additionally read `MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL`.
- Web: `npm test -- --run`, `npm run lint`, `npm run build` from `web`.
- Browser: `E2E_PORT=8847 npm run e2e -- movie-flow.spec.ts` from `web`.

The browser test exercises two independent shots, a continuation, page reload,
trimmed assembly, soundtrack, release/download, and mobile layout. Live GPU
quality and production rollout require separate verification.


### Generate the whole movie

In Movie, **Generate movie** reuses current successful takes, generates missing
shots within the project's batch limit, selects them, and stitches one movie in
story order. Continuations wait for their predecessor's selected final frame;
independent shots can run concurrently on eligible network workers. Existing
trims for matching takes are retained. **Generate another take** remains
available for deliberate new takes, and **Preview take** opens a modal player.

The actor-owned `POST /studio/movies/{id}/build` endpoint accepts
`expected_revision` and `request_id`. Build intent and batch request IDs persist
in the movie snapshot before job submission, so reconnecting does not duplicate
work. Keep the movie workspace open for orchestration: browser reconciliation
advances batches and assembly; reopening it resumes an unfinished build. Failed
takes stop the build for an explicit retry, retaining completed takes. Changing
the story, starting image, soundtrack or cut stops the current build for
review. Release remains an explicit reviewed action.

The legacy `opening_approved` snapshot field and approval endpoint remain readable
for old clients, but neither shot generation nor whole-movie builds require the
flag. Images are still validated, owner-scoped, stored immutably, and pinned into
the generation request. The unified editor sends `select_when_ready: true` for
individual takes; completion selects the newest successful current take through
the existing continuity invalidation boundary.


The Movie toolbar presents Generate movie first, followed by Review movie when
an assembled movie is ready. Generation saves the plan automatically; Save draft
in the creative brief is optional for work you want to return to later. Review
contains trimming, transitions, Save movie and preview assembly. Existing `/cut`
links redirect to `/review`. Release & export follows Review.
