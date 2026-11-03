"""What each of a person's own machines can run, and why not.

Never inferred from VRAM alone: a feature is available on a machine only when it
advertises one of the feature's capabilities (its verified configuration) and
reports the VRAM that capability's jobs require. VRAM only explains a refusal.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from outbound_gpu_worker_pool.contracts import JobRequirements

SUBJECT_CAPABILITY_ID = "image.flux2_klein.subject.v2"
SUBJECT_REQUIREMENTS = JobRequirements(min_vram_mb=16_000)
# The same graph under its own capability, served only by a worker attached with
# the 12GB subject profile (pool `flux2-klein-subject-12gb`): 1024², 20 steps,
# at most three references. Measured on a 4090 held to 12 GB free, not on a
# physical 12 GB card.
SUBJECT_12GB_CAPABILITY_ID = "image.flux2_klein.subject.12gb.v1"
SUBJECT_12GB_REQUIREMENTS = JobRequirements(min_vram_mb=12_000)
SUBJECT_12GB_PROFILE = "flux2-klein-subject-12gb"


@dataclass(frozen=True)
class Feature:
    """One kind of work. `routes` are tried in order; the first a machine serves wins."""

    key: str
    label: str
    routes: tuple[tuple[str, JobRequirements], ...]
    # The machine configuration that unlocks the smallest route, as the Machines page names it.
    configuration: str | None = None

    @property
    def smallest_mb(self) -> int:
        return min(requirements.min_vram_mb or 0 for _, requirements in self.routes)


SUBJECTS = Feature(
    "subjects", "subject images",
    # Preferred first: a machine that serves the 16 GB route keeps taking subjects there.
    ((SUBJECT_CAPABILITY_ID, SUBJECT_REQUIREMENTS),
     (SUBJECT_12GB_CAPABILITY_ID, SUBJECT_12GB_REQUIREMENTS)),
    "Subjects 12GB",
)
SUBJECT_ROUTES = SUBJECTS.routes
MOTIONS = Feature(
    "motions", "motions",
    (("video.minimax_h3.text_to_video.v1", JobRequirements(min_vram_mb=24_000)),
     ("video.minimax_h3.text_to_video.w4a8_16gb.lightx8.v1", JobRequirements(min_vram_mb=16_000)),
     ("video.minimax_h3.text_to_video.w4a8_16gb.v1", JobRequirements(min_vram_mb=16_000))),
    "H3 16GB",
)
# Takes and auditions submit the standard 24 GB H3 graphs only.
MOVIE_TAKES = Feature(
    "movie_takes", "movie takes",
    (("video.minimax_h3.text_to_video.v1", JobRequirements(min_vram_mb=24_000)),
     ("video.minimax_h3.dialogue.v1", JobRequirements(min_vram_mb=24_000))),
    "H3 24GB",
)
MOVIE_PROCESSING = Feature(
    "movie_processing", "movie processing",
    tuple((capability, JobRequirements()) for capability in
          ("movie.ffmpeg.process.v3", "movie.ffmpeg.process.v2", "movie.ffmpeg.process.v1")),
)
FEATURES = (SUBJECTS, MOTIONS, MOVIE_TAKES, MOVIE_PROCESSING)


# A machine heard from within this window can lease now (as motion discovery uses).
ONLINE_WINDOW_SECONDS = 90


def is_online(view: Any) -> bool:
    """Online or busy, and heard from recently: a machine that can lease a new job now."""
    beat = view.last_heartbeat_at
    return (view.status.value in ("online", "busy") and beat is not None
            and (datetime.now(UTC) - beat).total_seconds() <= ONLINE_WINDOW_SECONDS)


def revoked_by(enrolment: Any) -> Callable[[str], Awaitable[bool]] | None:
    """Whether a worker's Studio enrolment was removed. Removal only stops it
    authenticating; its pool view lingers until its heartbeat ages out."""
    if enrolment is None:
        return None

    async def revoked(worker_id: str) -> bool:
        enrolled = await enrolment.worker_by_id(worker_id)
        return enrolled is not None and enrolled.revoked

    return revoked


class MachineCannotRun(Exception):
    """None of the owner's machines can run this work; the message says why."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def card_vram_mb(view: Any) -> int | None:
    """The VRAM the machine reports: its registered figure, else its live telemetry."""
    if view.vram_mb:
        return int(view.vram_mb)
    totals = [gpu.memory_total_mb for gpu in getattr(view, "gpus", ()) if gpu.memory_total_mb]
    return max(totals) if totals else None


def gb(mb: int) -> str:
    """A card's size: cards report MiB, so 12,282 reads as 12 GB."""
    return f"{mb / 1024:.0f} GB"


def required_gb(mb: int) -> str:
    """A route's minimum: minimums are round MB figures, so 24,000 reads as 24 GB."""
    return f"{mb / 1000:.0f} GB"


def route_for(view: Any, feature: Feature) -> tuple[str, JobRequirements] | None:
    if view.draining:
        return None
    return next(((capability, requirements) for capability, requirements in feature.routes
                 if capability in view.capability_ids
                 and (view.vram_mb or 0) >= (requirements.min_vram_mb or 0)), None)


def reason(view: Any, feature: Feature) -> str | None:
    """Why this machine cannot run the feature, or None when it can."""
    if route_for(view, feature) is not None:
        return None
    if view.draining:
        return "it is shutting down"
    needed = feature.smallest_mb
    if not needed:
        return "it has no FFmpeg; install FFmpeg and ffprobe on it, then update the worker"
    attach = f" attached with the {feature.configuration} configuration" if feature.configuration else ""
    vram = card_vram_mb(view)
    if vram is None:
        return f"it reports no GPU; {feature.label} need a {required_gb(needed)} NVIDIA card{attach}"
    if vram < needed:
        return f"{feature.label} need a {required_gb(needed)} card and this one has {gb(vram)}"
    return f"its {gb(vram)} card can make {feature.label} once it is{attach or ' attached with a matching configuration'}"


def features_for(view: Any) -> list[dict[str, Any]]:
    """Each feature this machine can or cannot run, for the Machines page and API."""
    rows = []
    for feature in FEATURES:
        route = route_for(view, feature)
        rows.append({"key": feature.key, "label": feature.label, "available": route is not None,
                     "capability_id": route[0] if route else None,
                     "reason": None if route else reason(view, feature)})
    return rows


def subject_route_for(view: Any) -> tuple[str, JobRequirements] | None:
    return route_for(view, SUBJECTS)


def subject_reason(view: Any) -> str | None:
    return reason(view, SUBJECTS)


def _own_views(views: Iterable[Any], tenant_id: str) -> list[Any]:
    # The pool reads `tenant_id=None` as every tenant; keep exactly this owner's.
    return [view for view in views if view.tenant_id == tenant_id]


async def require_route(
    pool: Any, tenant_id: str | None, feature: Feature, *,
    labels: Mapping[str, str] | None = None, house_tenants: frozenset[str] = frozenset(),
    revoked: Callable[[str], Awaitable[bool]] | None = None,
) -> tuple[str, JobRequirements]:
    """The route this owner's machines can actually run for `feature`.

    House work (`tenant_id` None, or a cluster tenant in `house_tenants`) keeps
    the first route, which the house machines serve. An owner's own machines are
    asked what they advertise. Only enrolled machines that are online now count,
    so an offline or removed 16 GB machine never pulls work off an online 12 GB
    one. If none can run any route the work is refused here, with each machine's
    reason, rather than queued for a machine that will not lease it.
    """
    worker_views = getattr(pool, "worker_views", None)
    if tenant_id is None or tenant_id in house_tenants or worker_views is None:
        return feature.routes[0]
    views = _own_views(await worker_views(tenant_id=tenant_id), tenant_id)
    if revoked is not None:
        views = [view for view in views if not await revoked(view.worker_id)]
    ready = [view for view in views if is_online(view)]
    for route in feature.routes:
        if any(route_for(view, feature) == route for view in ready):
            return route
    if not views:
        raise MachineCannotRun(
            f"None of your machines has checked in during the last day, so nothing can make "
            f"{feature.label}. Start the worker on a machine that can.")
    names = labels or {}
    reasons = "; ".join(
        f"{names.get(view.worker_id, view.worker_id)}: "
        f"{reason(view, feature) or 'it is offline; start its worker'}" for view in views)
    raise MachineCannotRun(f"None of your machines can make {feature.label}. {reasons}.")


async def subject_route(
    pool: Any, tenant_id: str | None, *, labels: Mapping[str, str] | None = None,
    house_tenants: frozenset[str] = frozenset(),
    revoked: Callable[[str], Awaitable[bool]] | None = None,
) -> tuple[str, JobRequirements]:
    return await require_route(pool, tenant_id, SUBJECTS, labels=labels,
                               house_tenants=house_tenants, revoked=revoked)
