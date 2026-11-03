# Movies in Magic Studios

Approved direction, September 9, 2026: a separate Movies capability using the
existing studio visual language, with Story, Shots, Cut, Release and History.
Independent generation must use the networked GPU pool; continuation shots wait
for the selected predecessor's final frame. Preserve successful work and require
explicit generation requests within a small batch budget.

## Architecture

Keep movie manifests separate from animation manifests to preserve existing
release digests and animation contracts. Store immutable movie revisions with
optimistic concurrency in PostgreSQL (memory equivalent for local tests). The
movie service owns shot dependencies, take selection, continuity provenance and
budget reservations. The outbound GPU pool owns capability validation, worker
matching, leases and attested outputs. Use the registered MiniMax capability and
its actual bounds, never silently clamp the user's requested duration.

Every requested take is durably recorded before submission with a stable pool
idempotency key. Independent approved shots can fan out; continuation takes pin
the predecessor take. Failed/cancelled attempts remain in history and successful
takes survive retries. A changed predecessor requires explicit review of affected
shots rather than automatic regeneration. Reconciliation is idempotent and resumes
durable pending work. Composition approval precedes video generation; composition
briefs describe only the opening frame, separately from motion prompts.

CPU media operations extract continuation frames and assemble selected trimmed
clips into a normalized MP4. Music and narration cues are uploaded, trimmed,
offset and mixed with per-cue gain; imported clip audio is replaced explicitly.
They use bounded subprocesses and immutable output
keys. Release pins the exact cut and take choices. Owned asset grants and the
existing authenticated actor dependency protect every project and media route.

## Product

Onboarding offers Animation or Movie. Movies reuse the studio shell, typography,
blue primary actions, shot rail, large preview and focused inspector. Story holds
the brief and editable shot plan. Shots review compositions and takes, show
dependencies, and generate selected work with visible batch capacity. Cut supports
ordering, trims and assembly. Release offers a reviewed immutable movie download.
History exposes durable revisions. A continuation's explicit Generate request
authorizes starting from the selected predecessor take's final frame. It does not
need a separately generated composition; if no predecessor take is selected, the
request waits. Changing that selection marks existing dependent takes for review.
The user can accept a visually valid retained take after review without a new
render, while the original generation provenance remains intact.

## Cinder lessons verified against source notes

Sources: `cinder-poc/docs/video/intro-pipeline.md` and
`cinder-poc/docs/story/intro-trailer-shotlist.md`, September 6, 2026. The notes call
the first cut a draft, not an accepted final movie. They document approved plates,
independent beat clips and local assembly as the reliable route. This port carries
forward separate time-zero direction and motion prompts, one clear action per
shot, a shared visual brief, as-is opening uploads, existing-clip import, selective
new takes, editable ordering/trims, and music/narration offsets. It avoids the
old silent switch from an oversized clip to an unreliable stitched request.

Supported generation lengths are explicit: 1.5–15 seconds in half-second steps
on H3's native 24 fps clock, the longest request whose grid-aligned frame count
fits the template's 362-frame ceiling. Imported clips use measured duration.
Resolution is currently normalized to 768×432; resolution selection, upscaling and
automated narration generation are not part of this implementation. New cuts use
bounded half-second crossfades, adjustable per join; historical cuts remain intact.
The optional hosted shot planner returns suggestions for explicit review and save;
manual planning works without that provider.

## Verification

Test independent fan-out, blocked continuation, explicit take provenance, batch
limits, idempotent concurrent requests, retry preservation, stale revisions,
cross-owner access and immutable releases. Use tiny fixture videos for real FFmpeg
assembly/extraction checks. Exercise onboarding and movie workspaces in component
tests and a local browser flow; run existing animation regression suites.
