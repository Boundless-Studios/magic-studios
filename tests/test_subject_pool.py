import time
from datetime import UTC, datetime
from io import BytesIO

import numpy as np
import pytest
from outbound_gpu_worker_pool.contracts import JobRecord as PoolJobRecord
from outbound_gpu_worker_pool.contracts import JobStatus as PoolJobStatus
from outbound_gpu_worker_pool.contracts import JobSubmission as PoolJobSubmission
from PIL import Image

from magic_video_editor.contracts import SubjectGenerationRequest
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.plugins.subject_http import (
    RetryableSubjectProviderError,
    TerminalSubjectProviderError,
)
from magic_video_editor.plugins.subject_pool import (
    SUBJECT_POOL_CAPABILITY_ID,
    SUBJECT_REFERENCE_SLOTS,
    PoolSubjectGenerator,
    keep_main_subject,
    key_green_screen,
    normalise_subject_framing,
    subject_prompt,
)
from magic_video_editor.studio.quality import (
    SubjectFindingCode,
    assess_subject_png,
)


class _Pool:
    def __init__(self) -> None:
        self.submissions: list[PoolJobSubmission] = []
        self.records: dict[str, PoolJobRecord] = {}
        self.fail_submit = False

    async def submit(self, submission: PoolJobSubmission) -> PoolJobRecord:
        if self.fail_submit:
            raise RuntimeError("pool unreachable")
        self.submissions.append(submission)
        record = self._record(submission.job_id, PoolJobStatus.QUEUED)
        self.records[submission.job_id] = record
        return record

    async def get(self, job_id: str) -> PoolJobRecord | None:
        return self.records.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        return True

    # A job has no pool record until it is submitted; the generator looks first.
    def complete(self, job_id: str) -> None:
        if job_id in self.records:
            self.records[job_id] = self._record(job_id, PoolJobStatus.COMPLETED)

    def fail(self, job_id: str, *, retryable: bool, error: str = "boom") -> None:
        if job_id not in self.records:
            return
        record = self._record(job_id, PoolJobStatus.FAILED)
        self.records[job_id] = _replace_record(record, retryable=retryable, error=error)

    def _record(self, job_id: str, status: PoolJobStatus) -> PoolJobRecord:
        return PoolJobRecord(
            job_id=job_id,
            idempotency_key=f"{job_id}-key",
            capability_id=SUBJECT_POOL_CAPABILITY_ID,
            status=status,
            input_keys=(),
            output_key="outputs/studio/project-1/subject.raw.png",
            attempts=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


def _replace_record(record: PoolJobRecord, **updates: object) -> PoolJobRecord:
    from dataclasses import replace

    return replace(record, **updates)


def _solid_png(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _request(
    output_key: str = "outputs/studio/project-1/subject.png",
    reference_keys: tuple[str, ...] = (
        "sources/studio/project-1/subject-references/a.png",
    ),
    direction: str | None = "a grumpy dwarf blacksmith",
) -> SubjectGenerationRequest:
    return SubjectGenerationRequest(
        reference_keys=reference_keys,
        output_key=output_key,
        direction=direction,
        idempotency_key="idem-key-1",
    )


def test_subject_prompt_with_direction() -> None:
    prompt = subject_prompt("a grumpy dwarf blacksmith")
    assert "a grumpy dwarf blacksmith" in prompt
    assert "chroma key green screen" in prompt
    assert len(prompt) < 2000


def test_subject_prompt_without_direction_uses_default() -> None:
    for empty in (None, "", "   "):
        prompt = subject_prompt(empty)
        assert "an original character" in prompt
        assert len(prompt) < 2000


def test_subject_prompt_truncates_long_direction() -> None:
    prompt = subject_prompt("x" * 5000)
    assert len(prompt) <= 2000


def test_subject_prompt_with_one_reference_and_direction() -> None:
    prompt = subject_prompt("a grumpy dwarf blacksmith", reference_count=1)
    assert prompt.startswith(
        "Render the exact same character shown in the reference image, keeping "
        "the face, hair, outfit, colours and props identical. "
    )
    assert "a grumpy dwarf blacksmith" in prompt
    assert len(prompt) < 2000


def test_subject_prompt_with_multiple_references_pluralizes() -> None:
    prompt = subject_prompt("a grumpy dwarf blacksmith", reference_count=3)
    assert prompt.startswith(
        "Render the exact same character shown in the reference images, keeping"
    )
    assert len(prompt) < 2000


def test_subject_prompt_with_reference_and_no_direction_uses_reference_default() -> (
    None
):
    for empty in (None, "", "   "):
        prompt = subject_prompt(empty, reference_count=1)
        assert "the character from the reference" in prompt
        assert "an original character" not in prompt
        assert len(prompt) < 2000


def test_subject_prompt_truncates_long_direction_with_references() -> None:
    prompt = subject_prompt("x" * 5000, reference_count=2)
    assert len(prompt) <= 2000
    assert prompt.startswith(
        "Render the exact same character shown in the reference images"
    )


def test_job_id_and_seed_are_deterministic() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id, _pool_seed

    assert _pool_job_id("key-a") == _pool_job_id("key-a")
    assert _pool_job_id("key-a") != _pool_job_id("key-b")
    assert _pool_seed("key-a") == _pool_seed("key-a")
    assert _pool_seed("key-a") != _pool_seed("key-b")


async def test_submission_carries_the_owner_and_the_card_it_needs() -> None:
    """A machine someone enrolled must lease only that person's work, and a
    machine too small for the model must never be handed the job at all."""
    from magic_video_editor.plugins.subject_pool import (
        SUBJECT_POOL_REQUIREMENTS,
        _pool_job_id,
    )

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()
    request = SubjectGenerationRequest(
        reference_keys=request.reference_keys,
        output_key=request.output_key,
        direction=request.direction,
        idempotency_key=request.idempotency_key,
        owner_id="iap:accounts.google.com:12345",
    )

    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = _solid_png(
        (32, 32), (20, 224, 53)
    )
    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    await generator.generate_subject(request)

    submission = pool.submissions[0]
    assert submission.tenant_id == "iap:accounts.google.com:12345"
    assert submission.requirements == SUBJECT_POOL_REQUIREMENTS
    assert submission.requirements.min_vram_mb == 16_000


async def test_a_request_with_no_owner_stays_untenanted() -> None:
    """Untenanted is what house machines lease; it must not become the string
    "None" or an empty tenant, either of which would match nothing."""
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()

    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = _solid_png(
        (32, 32), (20, 224, 53)
    )
    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    await generator.generate_subject(request)

    assert pool.submissions[0].tenant_id is None


async def test_generate_subject_success_path() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()

    raw_png = _solid_png((32, 32), (20, 224, 53))
    # A real submit()/get() round trip: seed the raw asset and mark the pool
    # job completed before generate_subject's poll loop next calls get().
    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = raw_png

    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    output_key = await generator.generate_subject(request)

    assert output_key == request.output_key
    assert len(pool.submissions) == 1
    submission = pool.submissions[0]
    assert set(submission.payload.keys()) == {"prompt", "steps", "seed", "images"}
    assert submission.capability_id == SUBJECT_POOL_CAPABILITY_ID
    assert submission.input_keys == request.reference_keys
    assert submission.payload["images"] == {
        "ref_1": "sources/studio/project-1/subject-references/a.png"
    }
    assert submission.output_key == "outputs/studio/project-1/subject.raw.png"
    assert request.output_key in assets.assets


async def test_generate_subject_binds_multiple_references_in_order() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    references = tuple(
        f"sources/studio/project-1/subject-references/{name}.png"
        for name in ("a", "b", "c")
    )
    request = _request(reference_keys=references)

    raw_png = _solid_png((32, 32), (20, 224, 53))
    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = raw_png

    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    await generator.generate_subject(request)

    submission = pool.submissions[0]
    assert submission.input_keys == references
    assert submission.payload["images"] == dict(
        zip(SUBJECT_REFERENCE_SLOTS, references)
    )


async def test_generate_subject_binds_only_first_three_references() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    references = tuple(
        f"sources/studio/project-1/subject-references/{name}.png"
        for name in ("a", "b", "c", "d")
    )
    request = _request(reference_keys=references)

    raw_png = _solid_png((32, 32), (20, 224, 53))
    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = raw_png

    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    await generator.generate_subject(request)

    submission = pool.submissions[0]
    assert submission.input_keys == references[:3]
    assert submission.payload["images"] == dict(
        zip(SUBJECT_REFERENCE_SLOTS, references[:3])
    )


async def test_generate_subject_with_no_references_omits_images() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request(reference_keys=())

    raw_png = _solid_png((32, 32), (20, 224, 53))
    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = raw_png

    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    await generator.generate_subject(request)

    submission = pool.submissions[0]
    assert submission.input_keys == ()
    assert "images" not in submission.payload
    assert set(submission.payload.keys()) == {"prompt", "steps", "seed"}


async def test_generate_subject_idempotent_when_output_exists() -> None:
    pool = _Pool()
    assets = MemoryAssetStore()
    request = _request()
    assets.assets[request.output_key] = _solid_png((4, 4), (0, 177, 64))
    generator = PoolSubjectGenerator(pool, assets)

    output_key = await generator.generate_subject(request)

    assert output_key == request.output_key
    assert pool.submissions == []


async def test_generate_subject_retryable_failure() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()
    job_id = _pool_job_id(request.idempotency_key)

    original_get = pool.get

    async def get_then_fail(job_id_arg: str) -> PoolJobRecord | None:
        pool.fail(job_id, retryable=True, error="transient")
        return await original_get(job_id_arg)

    pool.get = get_then_fail  # type: ignore[method-assign]

    with pytest.raises(RetryableSubjectProviderError):
        await generator.generate_subject(request)


async def test_generate_subject_terminal_failure() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()
    job_id = _pool_job_id(request.idempotency_key)

    original_get = pool.get

    async def get_then_fail(job_id_arg: str) -> PoolJobRecord | None:
        pool.fail(job_id, retryable=False, error="rejected")
        return await original_get(job_id_arg)

    pool.get = get_then_fail  # type: ignore[method-assign]

    with pytest.raises(TerminalSubjectProviderError):
        await generator.generate_subject(request)


async def test_a_retry_polls_the_submitted_job_instead_of_submitting_again() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    job_id = _pool_job_id("idem-key-1")
    pool.records[job_id] = pool._record(job_id, PoolJobStatus.QUEUED)
    pool.fail(job_id, retryable=False, error="rejected")
    generator = PoolSubjectGenerator(pool, MemoryAssetStore(), poll_interval_seconds=0.0)

    with pytest.raises(TerminalSubjectProviderError, match="rejected"):
        await generator.generate_subject(_request())
    assert pool.submissions == []


async def test_generate_subject_timeout_is_retryable() -> None:
    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(
        pool, assets, poll_interval_seconds=0.0, timeout_seconds=0.0
    )
    request = _request()

    with pytest.raises(RetryableSubjectProviderError):
        await generator.generate_subject(request)


async def test_an_owners_subject_no_machine_picks_up_is_cancelled_with_the_reason() -> None:
    from dataclasses import replace as replace_request

    from magic_video_editor.plugins.subject_pool import SubjectMachineUnavailable

    pool = _Pool()
    cancelled: list[str] = []

    async def cancel(job_id: str) -> bool:
        cancelled.append(job_id)
        return True

    pool.cancel = cancel  # type: ignore[method-assign]
    generator = PoolSubjectGenerator(
        pool, MemoryAssetStore(), poll_interval_seconds=0.0, timeout_seconds=900.0
    )
    generator._timeout_seconds = 0.0
    request = replace_request(_request(), owner_id="user-1")

    with pytest.raises(SubjectMachineUnavailable, match="picked this subject up within 0 minutes"):
        await generator.generate_subject(request)
    assert cancelled == [pool.submissions[0].job_id]


def test_key_green_screen_keys_background_and_subject() -> None:
    size = 64
    image = Image.new("RGBA", (size, size), (20, 224, 53, 255))
    for y in range(24, 40):
        for x in range(24, 40):
            image.putpixel((x, y), (220, 30, 30, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    keyed = Image.open(BytesIO(key_green_screen(buffer.getvalue()))).convert("RGBA")

    assert keyed.getpixel((2, 2))[3] == 0
    assert keyed.getpixel((30, 30))[3] == 255


def test_key_green_screen_accepts_olive_backdrop() -> None:
    size = 64
    image = Image.new("RGBA", (size, size), (85, 145, 56, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    keyed = Image.open(BytesIO(key_green_screen(buffer.getvalue()))).convert("RGBA")
    assert keyed.getpixel((2, 2))[3] == 0


def test_key_green_screen_accepts_narrow_margin_olive_backdrop() -> None:
    # Margin below the retired 60 threshold but above the current 40
    # threshold: G=140, max(R, B)=95, margin=45.
    size = 64
    image = Image.new("RGBA", (size, size), (95, 140, 60, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    keyed = Image.open(BytesIO(key_green_screen(buffer.getvalue()))).convert("RGBA")
    assert keyed.getpixel((2, 2))[3] == 0


def test_key_green_screen_rejects_non_green_backdrop() -> None:
    size = 64
    image = Image.new("RGBA", (size, size), (128, 128, 128, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    with pytest.raises(TerminalSubjectProviderError):
        key_green_screen(buffer.getvalue())


def test_key_green_screen_despills_fringe_pixel() -> None:
    size = 16
    image = Image.new("RGBA", (size, size), (20, 224, 53, 255))
    image.putpixel((8, 8), (60, 200, 80, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    keyed = Image.open(BytesIO(key_green_screen(buffer.getvalue()))).convert("RGBA")
    r, g, b, a = keyed.getpixel((8, 8))
    assert 0 < a < 255
    assert g <= max(r, b)


def _rgba_png(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _array_to_png(array: "np.ndarray") -> bytes:
    buffer = BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def _debris_scene(size: int = 1024) -> "np.ndarray":
    """A keyed subject with debris the model might have painted around it.

    - A large opaque figure at the centre (the subject to keep).
    - A small blob 300px away from the figure (too small and too far: dropped).
    - A medium blob touching the figure's expanded bounding box (a held prop
      or detached hand: kept).
    - Faint alpha=20 smears scattered across the canvas, below the alpha
      threshold and not touching the figure (dropped).
    """
    array = np.zeros((size, size, 4), dtype=np.uint8)

    # Large opaque figure, centred.
    array[300:700, 400:600, :3] = 200
    array[300:700, 400:600, 3] = 255

    # Small blob far away (figure's right edge is at column 600; this blob's
    # left edge is at 900, well past the figure's expanded bbox and small
    # relative to the figure's ~80,000px area).
    array[300:320, 900:920, :3] = 200
    array[300:320, 900:920, 3] = 255

    # Medium blob touching the figure's expanded bounding box (figure right
    # edge at column 600, expansion is 4% of 1024 ~= 41px, so a blob starting
    # at column 630 is within the expanded box).
    array[400:600, 630:660, :3] = 200
    array[400:600, 630:660, 3] = 255

    # Faint smears well away from the figure and any kept debris.
    for y, x in ((50, 50), (950, 100), (100, 950)):
        array[y : y + 20, x : x + 20, 3] = 20
        array[y : y + 20, x : x + 20, :3] = 180

    return array


def test_keep_main_subject_keeps_largest_component() -> None:
    array = _debris_scene()
    result = np.asarray(
        Image.open(BytesIO(keep_main_subject(_array_to_png(array)))).convert("RGBA")
    )

    assert result[500, 500, 3] == 255  # centre of the large figure


def test_keep_main_subject_drops_distant_small_blob() -> None:
    array = _debris_scene()
    result = np.asarray(
        Image.open(BytesIO(keep_main_subject(_array_to_png(array)))).convert("RGBA")
    )

    assert result[310, 910, 3] == 0


def test_keep_main_subject_keeps_nearby_blob_touching_expanded_bbox() -> None:
    array = _debris_scene()
    result = np.asarray(
        Image.open(BytesIO(keep_main_subject(_array_to_png(array)))).convert("RGBA")
    )

    assert result[500, 645, 3] == 255


def test_keep_main_subject_drops_faint_smears() -> None:
    array = _debris_scene()
    result = np.asarray(
        Image.open(BytesIO(keep_main_subject(_array_to_png(array)))).convert("RGBA")
    )

    assert result[60, 60, 3] == 0
    assert result[960, 110, 3] == 0
    assert result[110, 960, 3] == 0


def test_keep_main_subject_preserves_anti_aliased_edge() -> None:
    array = _debris_scene()
    # A faint, below-threshold edge pixel directly adjacent to the kept
    # figure -- the anti-aliasing feather a real render would have.
    array[500, 600, :3] = 200
    array[500, 600, 3] = 20

    result = np.asarray(
        Image.open(BytesIO(keep_main_subject(_array_to_png(array)))).convert("RGBA")
    )

    assert result[500, 600, 3] == 20


def test_keep_main_subject_returns_input_unchanged_when_all_kept() -> None:
    array = np.zeros((64, 64, 4), dtype=np.uint8)
    array[16:48, 16:48, :3] = 200
    array[16:48, 16:48, 3] = 255
    png = _array_to_png(array)

    assert keep_main_subject(png) == png


def test_keep_main_subject_runs_fast_on_1024_random_noise() -> None:
    rng = np.random.default_rng(1234)
    array = np.zeros((1024, 1024, 4), dtype=np.uint8)
    array[..., :3] = 128
    array[..., 3] = rng.integers(0, 256, size=(1024, 1024), dtype=np.uint8)
    png = _array_to_png(array)

    start = time.monotonic()
    keep_main_subject(png)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0


def test_normalise_subject_framing_recenters_and_pads_edge_touching_subject() -> None:
    canvas = 1024
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    # An opaque rectangle touching the top edge (min_y == 0).
    box = (300, 0, 700, 500)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            image.putpixel((x, y), (200, 40, 40, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    normalised = Image.open(
        BytesIO(normalise_subject_framing(buffer.getvalue(), canvas=canvas))
    ).convert("RGBA")

    assert normalised.size == (canvas, canvas)
    alpha = normalised.getchannel("A")
    bbox = alpha.getbbox()
    assert bbox is not None
    min_x, min_y, max_x, max_y = bbox

    # 1px transparent border on every side.
    assert min_x >= 1
    assert min_y >= 1
    assert max_x <= canvas - 1
    assert max_y <= canvas - 1

    # Centred within +-1px.
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    assert abs(center_x - canvas / 2) <= 1
    assert abs(center_y - canvas / 2) <= 1

    # Larger side (width: 400 vs height: 500 -> height is larger) hits fill * canvas.
    expected_larger_side = 0.9 * canvas
    actual_larger_side = max(max_x - min_x, max_y - min_y)
    assert abs(actual_larger_side - expected_larger_side) <= 2


def test_normalise_subject_framing_caps_upscale_at_2x() -> None:
    tiny = _rgba_png((40, 40), (200, 40, 40, 255))

    normalised = Image.open(BytesIO(normalise_subject_framing(tiny))).convert("RGBA")

    bbox = normalised.getchannel("A").getbbox()
    assert bbox is not None
    min_x, min_y, max_x, max_y = bbox
    width = max_x - min_x
    height = max_y - min_y
    assert max(width, height) <= 80 + 2  # 40 * 2.0 upscale cap, +rounding slack
    assert max(width, height) > 40  # actually upscaled


def test_normalise_subject_framing_returns_all_transparent_input_unchanged() -> None:
    transparent = _rgba_png((64, 64), (0, 0, 0, 0))

    assert normalise_subject_framing(transparent) == transparent


async def test_generate_subject_produces_framing_that_passes_assessment() -> None:
    from magic_video_editor.plugins.subject_pool import _pool_job_id

    pool = _Pool()
    assets = MemoryAssetStore()
    generator = PoolSubjectGenerator(pool, assets, poll_interval_seconds=0.0)
    request = _request()

    # A green-screen studio render with a tall, edge-touching character
    # silhouette (full raw height, half the raw width -- aspect 0.5)
    # reproduces the production defect (subjects drawn to the canvas edge)
    # that assess_subject_png blocks on before normalisation. Large enough
    # that the framing scale stays under the 2x upscale cap.
    raw_size = 512
    raw = Image.new("RGBA", (raw_size, raw_size), (20, 224, 53, 255))
    for y in range(raw_size):
        for x in range(128, 384):
            raw.putpixel((x, y), (210, 60, 60, 255))
    buffer = BytesIO()
    raw.save(buffer, format="PNG")

    job_id = _pool_job_id(request.idempotency_key)
    assets.assets["outputs/studio/project-1/subject.raw.png"] = buffer.getvalue()

    original_get = pool.get

    async def get_then_complete(job_id_arg: str) -> PoolJobRecord | None:
        pool.complete(job_id)
        return await original_get(job_id_arg)

    pool.get = get_then_complete  # type: ignore[method-assign]

    output_key = await generator.generate_subject(request)
    stored_png = assets.assets[output_key]

    assessment = assess_subject_png(stored_png)
    codes = {finding.code for finding in assessment.findings}
    assert SubjectFindingCode.SUBJECT_TOUCHES_CANVAS_EDGE not in codes
    assert SubjectFindingCode.SUBJECT_OCCUPANCY_TOO_LOW not in codes
