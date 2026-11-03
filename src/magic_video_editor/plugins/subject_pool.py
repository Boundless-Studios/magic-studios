"""Subject generation through the outbound GPU worker pool.

Mirrors the motion path (`studio/service.py`'s `MotionPool` protocol and
`_motion_pool_payload`): submit a pool job for a fixed studio prompt, poll it
to completion, key the raw PNG the pool wrote against its own green screen
backdrop to RGBA, and store the canonical subject artifact.
"""

import asyncio
import hashlib
import time
import uuid
from contextlib import suppress
from io import BytesIO
from typing import Protocol

import numpy as np
from outbound_gpu_worker_pool.contracts import JobRecord as PoolJobRecord
from outbound_gpu_worker_pool.contracts import JobRequirements
from outbound_gpu_worker_pool.contracts import JobStatus as PoolJobStatus
from outbound_gpu_worker_pool.contracts import JobSubmission as PoolJobSubmission
from PIL import Image

from magic_video_editor.contracts import AssetStore, SubjectGenerationRequest
from magic_video_editor.machine_features import (
    SUBJECT_CAPABILITY_ID,
    SUBJECT_REQUIREMENTS,
    MachineCannotRun,
    revoked_by,
    subject_route,
)
from magic_video_editor.plugins.subject_http import (
    RetryableSubjectProviderError,
    TerminalSubjectProviderError,
)
from magic_video_editor.studio.quality import SUBJECT_POLICY_V1

SUBJECT_POOL_CAPABILITY_ID = SUBJECT_CAPABILITY_ID
# The 16 GB route. A 12 GB card serves subjects only through its own capability
# (`machine_features.SUBJECT_ROUTES`), chosen per job from what the owner's
# machines advertise.
SUBJECT_POOL_REQUIREMENTS: JobRequirements = SUBJECT_REQUIREMENTS
SUBJECT_POOL_NAMESPACE = uuid.UUID("b6f8b6c0-2d90-4b8b-9c0a-2f1c8f5a9d41")
SUBJECT_REFERENCE_SLOTS = ("ref_1", "ref_2", "ref_3")

_PROMPT_MAX_CHARS = 2000
_PROMPT_TEMPLATE = (
    "A single full-body character: {direction}. Centered, facing the camera, the "
    "entire body visible with margin on every side, neutral standing pose, clean "
    "crisp edges, no props, no text, no particles, no debris, no motion effects, "
    "no floating fragments, no glow. Replace the background entirely with a "
    "bright, evenly lit chroma key green screen: pure saturated neon green, "
    "flat, no texture, no gradient, no shadows, no floor, nothing else in the "
    "frame."
)
_REFERENCE_PREFIX_TEMPLATE = (
    "Render the exact same character shown in the reference image{s}, keeping "
    "the face, hair, outfit, colours and props identical. "
)
_DEFAULT_DIRECTION = "an original character"
_DEFAULT_DIRECTION_WITH_REFERENCE = "the character from the reference"
_BORDER_WIDTH = 4
_GREEN_DOMINANCE_MIN_G = 120
_GREEN_DOMINANCE_MIN_MARGIN = 40
_KEY_LOW = 40.0
_KEY_HIGH = 110.0

MAX_RAW_SUBJECT_BYTES = SUBJECT_POLICY_V1.max_bytes


def subject_prompt(direction: str | None, reference_count: int = 0) -> str:
    """Build the fixed studio prompt around the user's direction.

    When one or more references are bound to the pool submission, prepend a
    sentence asking the model to reproduce that character and fall back to a
    reference-aware default when the direction is blank.
    """
    has_references = reference_count > 0
    prefix = (
        _REFERENCE_PREFIX_TEMPLATE.format(s="s" if reference_count > 1 else "")
        if has_references
        else ""
    )
    default_direction = (
        _DEFAULT_DIRECTION_WITH_REFERENCE if has_references else _DEFAULT_DIRECTION
    )
    normalized = (direction or "").strip() or default_direction
    # Leave enough room for the prefix and the template's fixed text around
    # {direction}.
    template_overhead = len(prefix) + len(_PROMPT_TEMPLATE.format(direction=""))
    max_direction_chars = max(0, _PROMPT_MAX_CHARS - template_overhead)
    normalized = normalized[:max_direction_chars]
    prompt = prefix + _PROMPT_TEMPLATE.format(direction=normalized)
    return prompt[:_PROMPT_MAX_CHARS]


def _raw_output_key(output_key: str) -> str:
    dot = output_key.rfind(".")
    slash = output_key.rfind("/")
    if dot > slash:
        return f"{output_key[:dot]}.raw{output_key[dot:]}"
    return f"{output_key}.raw.png"


def _pool_job_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(SUBJECT_POOL_NAMESPACE, idempotency_key))


def _pool_seed(idempotency_key: str) -> int:
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return int(digest[:8], 16)


def _estimate_backdrop(array: np.ndarray) -> tuple[float, float, float]:
    """Median RGB of the 4-pixel-wide border frame."""
    height, width = array.shape[:2]
    w = min(_BORDER_WIDTH, height, width)
    border_pixels = np.concatenate(
        [
            array[:w, :, :3].reshape(-1, 3),
            array[-w:, :, :3].reshape(-1, 3),
            array[:, :w, :3].reshape(-1, 3),
            array[:, -w:, :3].reshape(-1, 3),
        ],
        axis=0,
    )
    median = np.median(border_pixels, axis=0)
    return float(median[0]), float(median[1]), float(median[2])


def key_green_screen(png: bytes) -> bytes:
    """Key a raw studio-background PNG to RGBA against the green-screen
    backdrop estimated from the image's own border, with a linear alpha ramp
    and despill on the fringe."""
    image = Image.open(BytesIO(png)).convert("RGBA")
    array = np.asarray(image).astype(np.float64)
    r, g, b, a = (array[..., i] for i in range(4))

    key_r, key_g, key_b = _estimate_backdrop(array)
    is_green_dominant = (
        key_g >= _GREEN_DOMINANCE_MIN_G
        and key_g - max(key_r, key_b) >= _GREEN_DOMINANCE_MIN_MARGIN
    )
    if not is_green_dominant:
        raise TerminalSubjectProviderError(
            "generated image has no green screen backdrop"
        )

    distance = np.sqrt((r - key_r) ** 2 + (g - key_g) ** 2 + (b - key_b) ** 2)
    span = _KEY_HIGH - _KEY_LOW
    factor = np.clip((distance - _KEY_LOW) / span, 0.0, 1.0)
    new_alpha = np.round(a * factor)

    despill_mask = (new_alpha > 0) & (new_alpha < 255) & (g > np.maximum(r, b))
    g = np.where(despill_mask, np.maximum(r, b), g)

    out = np.stack([r, g, b, new_alpha], axis=-1)
    out = np.clip(out, 0, 255).astype(np.uint8)
    keyed = Image.fromarray(out)
    buffer = BytesIO()
    keyed.save(buffer, format="PNG")
    return buffer.getvalue()


_DEBRIS_ALPHA_THRESHOLD = 32
_DEBRIS_KEEP_RATIO = 0.02
_DEBRIS_BBOX_EXPANSION_RATIO = 0.04


def _label_components_8(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Label the 8-connected components of a boolean mask.

    Implemented as a scanline union-find over per-row runs of `True` pixels
    (no scipy dependency): each row is decomposed into contiguous runs, a run
    is connected to any run in the previous row whose span overlaps it once
    expanded by one pixel on each side (the 8-connectivity rule), and
    same-component runs are unioned. This keeps the per-row work proportional
    to the number of runs rather than the number of pixels, which is what
    keeps large, densely-filled masks fast.

    Returns `(labels, num_labels)` where `labels` is an `int32` array the
    same shape as `mask` with `0` for background and `1..num_labels` for
    foreground components.
    """
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)

    parent: list[int] = [0]

    def make_label() -> int:
        parent.append(len(parent))
        return len(parent) - 1

    def find(label: int) -> int:
        root = label
        while parent[root] != root:
            root = parent[root]
        while parent[label] != root:
            parent[label], label = root, parent[label]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    previous_runs: list[tuple[int, int, int]] = []
    for y in range(height):
        row = mask[y]
        indices = np.flatnonzero(row)
        if indices.size == 0:
            previous_runs = []
            continue
        gaps = np.flatnonzero(np.diff(indices) > 1)
        starts = np.concatenate(([indices[0]], indices[gaps + 1]))
        ends = np.concatenate((indices[gaps], [indices[-1]]))

        current_runs: list[tuple[int, int, int]] = []
        for start, end in zip(starts.tolist(), ends.tolist(), strict=True):
            touching = {
                find(plabel)
                for pstart, pend, plabel in previous_runs
                if pstart <= end + 1 and pend >= start - 1
            }
            if touching:
                label = min(touching)
                for other in touching:
                    union(label, other)
                label = find(label)
            else:
                label = make_label()
            labels[y, start : end + 1] = label
            current_runs.append((start, end, label))
        previous_runs = current_runs

    raw_labels = np.unique(labels[labels > 0])
    remap = np.zeros(len(parent), dtype=np.int32)
    next_id = 1
    roots: dict[int, int] = {}
    for raw_label in raw_labels.tolist():
        root = find(raw_label)
        assigned = roots.get(root)
        if assigned is None:
            assigned = next_id
            roots[root] = assigned
            next_id += 1
        remap[raw_label] = assigned

    labels = remap[labels]
    return labels, next_id - 1


def _component_bounding_boxes(
    labels: np.ndarray, num_labels: int
) -> list[tuple[int, int, int, int]]:
    """`(min_row, max_row, min_col, max_col)` per label, 1-indexed."""
    rows, cols = np.nonzero(labels)
    flat_labels = labels[rows, cols]

    min_row = np.full(num_labels + 1, np.iinfo(np.int32).max, dtype=np.int64)
    max_row = np.full(num_labels + 1, -1, dtype=np.int64)
    min_col = np.full(num_labels + 1, np.iinfo(np.int32).max, dtype=np.int64)
    max_col = np.full(num_labels + 1, -1, dtype=np.int64)

    np.minimum.at(min_row, flat_labels, rows)
    np.maximum.at(max_row, flat_labels, rows)
    np.minimum.at(min_col, flat_labels, cols)
    np.maximum.at(max_col, flat_labels, cols)

    return [
        (int(min_row[i]), int(max_row[i]), int(min_col[i]), int(max_col[i]))
        for i in range(1, num_labels + 1)
    ]


def _bboxes_touch(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    a_min_row, a_max_row, a_min_col, a_max_col = a
    b_min_row, b_max_row, b_min_col, b_max_col = b
    return not (
        a_max_row < b_min_row
        or b_max_row < a_min_row
        or a_max_col < b_min_col
        or b_max_col < a_min_col
    )


def _dilate8(mask: np.ndarray) -> np.ndarray:
    """OR a boolean mask with its 8 unit-shifted copies (no scipy)."""
    padded = np.zeros((mask.shape[0] + 2, mask.shape[1] + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask
    out = np.zeros_like(mask)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy == 1 and dx == 1:
                continue
            out |= padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return out


def keep_main_subject(
    png: bytes,
    *,
    alpha_threshold: int = _DEBRIS_ALPHA_THRESHOLD,
    keep_ratio: float = _DEBRIS_KEEP_RATIO,
) -> bytes:
    """Drop everything the model painted outside the main character.

    `key_green_screen` only removes the green backdrop, so any non-green
    "effect" the model paints -- shards, smears, streaks -- survives as
    opaque or semi-opaque debris floating around the transparent subject.
    This keeps the largest 8-connected alpha component (the character) plus
    any component that is both large enough relative to it (`keep_ratio` of
    its area, e.g. a held prop or a detached hand) and spatially near it
    (its bounding box touches or overlaps the largest component's bounding
    box expanded by `_DEBRIS_BBOX_EXPANSION_RATIO` of the canvas). Everything
    else is cleared to alpha 0. Faint anti-aliased edge pixels below
    `alpha_threshold` are only cleared when they are not 8-adjacent to a kept
    pixel, so genuine edge feathering on the subject survives.
    """
    image = Image.open(BytesIO(png)).convert("RGBA")
    array = np.asarray(image)
    alpha = array[..., 3]
    mask = alpha > alpha_threshold
    if not mask.any():
        return png

    labels, num_labels = _label_components_8(mask)
    areas = np.bincount(labels.ravel(), minlength=num_labels + 1)
    bboxes = _component_bounding_boxes(labels, num_labels)

    largest_label = 1 + int(np.argmax(areas[1:]))
    largest_area = int(areas[largest_label])
    largest_bbox = bboxes[largest_label - 1]

    height, width = mask.shape
    expansion = round(_DEBRIS_BBOX_EXPANSION_RATIO * max(height, width))
    expanded_bbox = (
        largest_bbox[0] - expansion,
        largest_bbox[1] + expansion,
        largest_bbox[2] - expansion,
        largest_bbox[3] + expansion,
    )

    kept_labels = {largest_label}
    for label in range(1, num_labels + 1):
        if label == largest_label:
            continue
        if areas[label] >= keep_ratio * largest_area and _bboxes_touch(
            bboxes[label - 1], expanded_bbox
        ):
            kept_labels.add(label)

    kept_mask = np.isin(labels, list(kept_labels))
    if kept_mask.all():
        return png

    dilated_kept_mask = _dilate8(kept_mask)
    preserve_faint_edge = (alpha < alpha_threshold) & dilated_kept_mask
    clear = ~kept_mask & ~preserve_faint_edge

    new_alpha = np.where(clear, 0, alpha).astype(np.uint8)
    out = array.copy()
    out[..., 3] = new_alpha

    result = Image.fromarray(out)
    buffer = BytesIO()
    result.save(buffer, format="PNG")
    return buffer.getvalue()


_FRAMING_ALPHA_THRESHOLD = 8
_FRAMING_MAX_UPSCALE = 2.0
_FRAMING_FILL = 0.9


def normalise_subject_framing(
    png: bytes, *, canvas: int = 1024, fill: float = _FRAMING_FILL
) -> bytes:
    """Crop a keyed subject to its alpha bounding box, scale it to fill the
    canvas with margin, and paste it centered on a transparent square canvas.

    `fill` is chosen against `SUBJECT_POLICY_V1` (min_occupancy_ratio=0.35,
    max_occupancy_ratio=0.92) and the edge rule's "no opaque pixel may touch
    the outer canvas edge":

    - Edge margin: at `fill=0.9` the larger side lands at `0.9 * canvas`, so
      each side of a `canvas x canvas` frame carries a `(1 - 0.9) / 2 = 5%`
      transparent border (51px at canvas=1024) -- far more than the 1px the
      edge rule needs.
    - Occupancy, square subject: a subject whose bbox is as wide as it is
      tall occupies `fill**2 = 0.81` of the canvas once scaled -- safely
      under the 0.92 ceiling (margin 0.11).
    - Occupancy, tall/thin subject: modelling a slender full-body character
      as a fully-opaque bbox at a 1:2 width:height ratio (aspect=0.5), the
      scaled occupancy is `fill**2 * aspect = 0.81 * 0.5 = 0.405`, above the
      0.35 floor with a 0.055 margin. Thinner-than-~1:2.3 subjects
      (aspect < 0.35 / fill**2 ~= 0.43) can still trip the (warning-only,
      non-blocking) low-occupancy check -- preserving aspect ratio means
      reframing cannot manufacture occupancy for a genuinely narrow subject
      without distorting it.
    """
    image = Image.open(BytesIO(png)).convert("RGBA")
    alpha = np.asarray(image.getchannel("A"))
    mask_rows = np.where((alpha > _FRAMING_ALPHA_THRESHOLD).any(axis=1))[0]
    mask_cols = np.where((alpha > _FRAMING_ALPHA_THRESHOLD).any(axis=0))[0]
    if mask_rows.size == 0 or mask_cols.size == 0:
        return png

    min_y, max_y = int(mask_rows[0]), int(mask_rows[-1])
    min_x, max_x = int(mask_cols[0]), int(mask_cols[-1])
    cropped = image.crop((min_x, min_y, max_x + 1, max_y + 1))

    crop_width, crop_height = cropped.size
    target = fill * canvas
    scale = min(target / max(crop_width, crop_height), _FRAMING_MAX_UPSCALE)
    new_width = max(1, round(crop_width * scale))
    new_height = max(1, round(crop_height * scale))
    resized = cropped.resize((new_width, new_height), Image.Resampling.LANCZOS)

    framed = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    offset_x = round((canvas - new_width) / 2)
    offset_y = round((canvas - new_height) / 2)
    framed.paste(resized, (offset_x, offset_y), resized)

    buffer = BytesIO()
    framed.save(buffer, format="PNG")
    return buffer.getvalue()


class SubjectMachineUnavailable(TerminalSubjectProviderError):
    """None of the owner's machines can run this subject; the message says why."""


class SubjectPool(Protocol):
    """The same three-call surface the motion path uses (`MotionPool` in
    `studio/service.py`); `WorkerPoolService` satisfies this, tests substitute
    a fake."""

    async def submit(self, submission: PoolJobSubmission) -> PoolJobRecord: ...

    async def get(self, job_id: str) -> PoolJobRecord | None: ...

    async def cancel(self, job_id: str) -> bool: ...


class PoolSubjectGenerator:
    def __init__(
        self,
        pool: SubjectPool,
        assets: AssetStore,
        *,
        capability_id: str = SUBJECT_POOL_CAPABILITY_ID,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 900.0,
        steps: int = 20,
        house_tenants: frozenset[str] = frozenset(),
        enrolment: object | None = None,
    ) -> None:
        self._pool = pool
        self._revoked = revoked_by(enrolment)
        self._assets = assets
        self._capability_id = capability_id
        self._house_tenants = house_tenants
        self._poll_interval_seconds = poll_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._steps = steps

    async def generate_subject(self, request: SubjectGenerationRequest) -> str:
        existing = await self._assets.describe(request.output_key)
        if existing is not None:
            return request.output_key

        job_id = _pool_job_id(request.idempotency_key)
        # A retry of a job already in the pool polls it as submitted: its route
        # was chosen then, and a machine that has since gone away must not fail
        # a job that is running or already done.
        submitted = await self._pool.get(job_id)
        capability_id, requirements = self._capability_id, SUBJECT_POOL_REQUIREMENTS
        if submitted is None and capability_id == SUBJECT_POOL_CAPABILITY_ID:
            # Refused at submit too; this catches a machine removed since then,
            # so the job fails with the reason instead of queueing unleasable.
            try:
                capability_id, requirements = await subject_route(
                    self._pool, request.owner_id, house_tenants=self._house_tenants,
                    revoked=self._revoked,
                )
            except MachineCannotRun as exc:
                raise SubjectMachineUnavailable(exc.message) from exc
        raw_key = _raw_output_key(request.output_key)
        bound_references = request.reference_keys[: len(SUBJECT_REFERENCE_SLOTS)]
        payload: dict[str, object] = {
            "prompt": subject_prompt(request.direction, len(bound_references)),
            "steps": self._steps,
            "seed": _pool_seed(request.idempotency_key),
        }
        if bound_references:
            payload["images"] = dict(
                zip(SUBJECT_REFERENCE_SLOTS, bound_references, strict=False)
            )
        submission = PoolJobSubmission(
            job_id=job_id,
            idempotency_key=request.idempotency_key,
            capability_id=capability_id,
            input_keys=bound_references,
            output_key=raw_key,
            payload=payload,
            # An enrolled machine leases only its owner's work; None leaves the
            # job untenanted, which is what house machines lease.
            tenant_id=request.owner_id,
            requirements=requirements,
        )
        try:
            if submitted is None:
                await self._pool.submit(submission)
        except Exception as exc:
            raise RetryableSubjectProviderError(
                "subject pool submission failed"
            ) from exc

        record = await self._poll_until_terminal(
            job_id, house=request.owner_id is None or request.owner_id in self._house_tenants
        )
        if record.status is PoolJobStatus.CANCELLED:
            raise TerminalSubjectProviderError("subject pool job was cancelled")
        if record.status is PoolJobStatus.FAILED:
            error = record.error or "subject pool job failed"
            if record.retryable:
                raise RetryableSubjectProviderError(error)
            raise TerminalSubjectProviderError(error)

        raw_png = await self._assets.read_limited(raw_key, MAX_RAW_SUBJECT_BYTES)
        keyed_png = await asyncio.to_thread(key_green_screen, raw_png)
        subject_only_png = await asyncio.to_thread(keep_main_subject, keyed_png)
        framed_png = await asyncio.to_thread(
            normalise_subject_framing, subject_only_png
        )
        await self._assets.write_once(request.output_key, framed_png, "image/png")
        return request.output_key

    async def _poll_until_terminal(self, job_id: str, *, house: bool = True) -> PoolJobRecord:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            record = await self._pool.get(job_id)
            if record is not None and record.status in (
                PoolJobStatus.COMPLETED,
                PoolJobStatus.FAILED,
                PoolJobStatus.CANCELLED,
            ):
                return record
            if time.monotonic() >= deadline:
                if not house and record is not None and record.status is PoolJobStatus.QUEUED:
                    # None of the owner's machines picked it up: say so once, and
                    # cancel it so it never runs unseen after the person moved on.
                    with suppress(Exception):
                        await self._pool.cancel(job_id)
                    raise SubjectMachineUnavailable(
                        f"None of your machines picked this subject up within "
                        f"{round(self._timeout_seconds / 60)} minutes, so it was cancelled. "
                        "Check that a machine that can make subject images is online on "
                        "the Machines page, then generate it again."
                    )
                raise RetryableSubjectProviderError(
                    "subject pool job did not complete before the timeout"
                )
            await asyncio.sleep(self._poll_interval_seconds)
