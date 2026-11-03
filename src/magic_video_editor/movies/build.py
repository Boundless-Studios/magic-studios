"""Movie-level intent: reuse good takes, batch missing shots, then assemble once."""

from hashlib import sha256

from magic_video_editor.movies.dialogue import FPS, GUIDE_FRAMES
from magic_video_editor.movies.models import Clip, MovieBuild, ShotInput
from magic_video_editor.movies.service import MovieError, digest, shot_texts
from magic_video_editor.safety import PromptRefused, PromptScreenUnavailable

ACTIVE = {"waiting", "submitting", "running"}
# Six frames at 24 fps: long enough to hide a re-render step, short enough
# that nothing said on either side is crossed.
CHAINED_DIALOGUE_DISSOLVE = 6 / FPS


# Fields added after builds were already persisted: left unset they are omitted, so a
# build started before they existed still matches its plan after the deploy.
_OPTIONAL_SHOT_FIELDS = ("camera", "still", "beats")


def _shot_plan(s):
    plan = s.model_dump(include=set(ShotInput.model_fields))
    for field in _OPTIONAL_SHOT_FIELDS:
        if not plan.get(field):
            plan.pop(field, None)
    return plan


def plan_digest(movie):
    return digest(
        {
            "style": movie.style,
            "compute": movie.compute_target,
            "characters": [c.model_dump() for c in movie.characters],
            "shots": [
                {
                    **_shot_plan(s),
                    "opening": s.opening_asset_key,
                    "approved": s.opening_approved,
                }
                for s in movie.shots
            ],
            "soundtracks": [t.model_dump() for t in movie.soundtracks],
            "cut": [c.model_dump() for c in movie.cut],
        }
    )


def story_cut(picks, existing=()):
    """One clip per (shot, take) pick in story order, keeping a saved clip's
    trims for the same take. Generate movie saves this cut; Review starts from
    it (`selected_cut`) until the owner saves their own."""
    saved = {(c.shot_id, c.take_id): c for c in existing}
    takes = [take for _, take in picks]
    return [
        saved.get((s.shot_id, take.take_id))
        or Clip(
            shot_id=s.shot_id,
            take_id=take.take_id,
            # A guided dialogue take repeats its predecessor's pinned
            # frames first; the cut starts after them.
            in_seconds=(
                GUIDE_FRAMES / FPS
                if take.mode == "dialogue" and take.predecessor_take_id
                else 0
            ),
            # Never dissolve across a speaking face. Between two chained
            # dialogue takes both sides of the join are quiet — the
            # predecessor's held ending and the successor's lead-in — so
            # a six-frame dissolve spreads the re-render step (about
            # 0.014 at a hard cut) below what the eye catches. Motion
            # shots dissolve over half a second; anything else cuts.
            transition_seconds=(
                CHAINED_DIALOGUE_DISSOLVE
                if take.mode == "dialogue"
                and following is not None
                and following.mode == "dialogue"
                and following.predecessor_take_id == take.take_id
                else 0
                if take.mode == "dialogue"
                or (following is not None and following.mode == "dialogue")
                else 0.5
            ),
            out_seconds=take.duration_seconds,
            mute=not s.dialogue and take.request_id[:7] != "import:",
        )
        for (s, take), following in zip(picks, [*takes[1:], None])
    ]


def selected_cut(movie):
    """The story-order cut of each shot's selected take; shots without a
    finished, current selection are left out."""
    picks = []
    for shot in movie.shots:
        take = next((t for t in shot.takes if t.take_id == shot.selected_take_id), None)
        if take and take.status == "succeeded" and not take.stale:
            picks.append((shot, take))
    return story_cut(picks)


async def start_build(service, id, owner, expected, request_id):
    movie = await service.get(id, owner)
    if movie.build and movie.build.request_id == request_id:
        return await service.reconcile(id, owner)
    movie = await service.current(id, owner, expected)
    if movie.build and movie.build.status in {"generating", "assembling"}:
        return await service.reconcile(id, owner)
    if not movie.shots:
        raise MovieError("Add at least one shot before generating your movie")
    for shot in movie.shots:
        # An imported clip (or any usable take) needs no starting image.
        usable = any(t.status == "succeeded" and not t.stale for t in shot.takes)
        if not shot.continuation_of and not shot.opening_asset_key and not usable:
            raise MovieError(f"Add a starting image for {shot.name} first")
    if not request_id or len(request_id) > 120:
        raise MovieError("A request ID of at most 120 characters is required")
    # Refuse now, on the request that asked, rather than when a later reconcile
    # reaches the offending shot; each take's final prompt is screened again.
    await service.prompt_screening.require_allowed(
        owner, "movie", [movie.style, *(c.appearance for c in movie.characters),
                         *(text for shot in movie.shots for text in shot_texts(shot))],
    )
    movie.build = MovieBuild(request_id=request_id, plan_digest=plan_digest(movie))
    await service.save(movie, movie.revision)
    return await service.reconcile(id, owner)


async def advance(service, movie):
    build = movie.build
    if not build or build.status not in {"generating", "assembling"}:
        return movie
    id, owner = movie.movie_id, movie.owner_id
    try:
        if build.plan_digest != plan_digest(movie):
            raise MovieError(
                "Movie plan changed. Review it and generate the movie again."
            )
        changed = False
        # Reuse the chosen take, otherwise the newest successful take, otherwise
        # adopt in-flight work. Never queue another take while one is running.
        for shot in movie.shots:
            if shot.shot_id in build.takes:
                continue
            candidates = [
                t for t in shot.takes if not t.stale and t.status == "succeeded"
            ]
            chosen = next(
                (t for t in candidates if t.take_id == shot.selected_take_id), None
            )
            chosen = chosen or (candidates[-1] if candidates else None)
            chosen = chosen or next(
                (t for t in shot.takes if not t.stale and t.status in ACTIVE), None
            )
            chosen = chosen or next(
                (t for t in shot.takes if t.request_id in build.requests), None
            )
            if chosen:
                build.takes[shot.shot_id] = chosen.take_id
                changed = True
        if changed:
            movie = await service.save(movie, movie.revision)
            build = movie.build
        for sid, tid in build.takes.items():
            take = service.take(movie, sid, tid)
            if take.stale or take.status in {"failed", "cancelled"}:
                raise MovieError(
                    f"{service.shot(movie, sid).name} needs another take. Retry generating the movie."
                )
            if (
                take.status == "succeeded"
                and service.shot(movie, sid).selected_take_id != tid
            ):
                # Select through the normal continuity invalidation boundary.
                return await service.select(id, owner, movie.revision, sid, tid)
        active = sum(t.status in ACTIVE for s in movie.shots for t in s.takes)
        slots = max(0, movie.batch_size - active)
        missing = [s for s in movie.shots if s.shot_id not in build.takes]
        ready = [
            s
            for s in missing
            if not s.continuation_of
            or (
                s.continuation_of in build.takes
                and service.take(
                    movie, s.continuation_of, build.takes[s.continuation_of]
                ).status
                == "succeeded"
            )
        ]
        if service.pool is None and (missing or active):
            # No pool is a deployment fact, not a busy one: generate's 503 would be
            # retried on every reconcile, and takes started before a restart without
            # a pool can never be submitted, polled or cancelled. The build waits on
            # every active take in the movie below, so any of them would keep it
            # generating forever.
            raise MovieError(
                "This server has no GPU pool, so the movie's unfinished shots and "
                "takes cannot finish. Import a clip for each shot without a "
                "finished take, then generate the movie again."
            )
        if ready and slots:
            batch = [s.shot_id for s in ready[:slots]]
            request = (
                "build:"
                + sha256(
                    (build.request_id + ":" + ",".join(batch)).encode()
                ).hexdigest()
            )
            if request not in build.requests:
                build.requests.append(request)
                movie = await service.save(movie, movie.revision)
            try:
                return await service.generate(
                    id, owner, movie.revision, batch, request, advance_build=False
                )
            except PromptRefused as refused:
                raise MovieError(str(refused), 422, code=refused.code) from refused
            except PromptScreenUnavailable as unavailable:
                # Like unavailable capacity: the next reconcile tries again.
                raise MovieError(str(unavailable), 503, code=unavailable.code) from unavailable
        if missing or active:
            return movie
        if any(
            service.take(movie, sid, tid).status != "succeeded"
            for sid, tid in build.takes.items()
        ):
            return movie
        if build.status == "generating":
            # The main movie action intentionally makes a complete story-order cut.
            takes = [service.take(movie, s.shot_id, build.takes[s.shot_id]) for s in movie.shots]
            movie.cut = story_cut(list(zip(movie.shots, takes)), movie.cut)
            movie.assembly = None
            build.status = "assembling"
            build.plan_digest = plan_digest(movie)
            movie = await service.save(movie, movie.revision)
            build = movie.build
        request = "build-cut:" + sha256(build.request_id.encode()).hexdigest()
        if not movie.assembly or movie.assembly.request_id != request:
            return await service.assemble(
                id, owner, movie.revision, request, advance_build=False
            )
        if movie.assembly.status == "failed":
            raise MovieError(movie.assembly.error or "Movie assembly failed")
        if movie.assembly.status == "succeeded":
            build.status = "succeeded"
            return await service.save(movie, movie.revision)
        return movie
    except MovieError as error:
        if error.status in {409, 429, 503}:
            # Conflicting reconcilers or temporarily unavailable capacity retry.
            return await service.get(id, owner)
        current = await service.get(id, owner)
        if current.revision != movie.revision:
            return current
        current.build.status = "failed"
        current.build.error = str(error)
        return await service.save(current, current.revision)
