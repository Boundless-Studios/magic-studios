import asyncio

import pytest

from magic_video_editor.contracts import RuntimeLifecycle
from magic_video_editor.plugins import ffmpeg


async def test_processor_satisfies_the_runtime_lifecycle_it_is_registered_under() -> None:
    # The runtime stops every started component at shutdown. A component
    # without `stop` turns every worker shutdown into a raised exception
    # (Sentry MAGIC-VIDEO-EDITOR-BACKEND-1), so the contract is asserted here
    # rather than discovered in production.
    processor = ffmpeg.FfmpegVideoProcessor()

    # RuntimeLifecycle is a plain Protocol (not runtime_checkable), so the
    # contract is asserted structurally: both halves exist and are awaitable.
    lifecycle: RuntimeLifecycle = processor
    assert callable(lifecycle.start) and callable(lifecycle.stop)
    assert await lifecycle.stop() is None


async def test_build_derivatives_packages_alpha_video_and_sprite_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bytes, int]] = []

    async def build_alpha(source: bytes) -> bytes:
        calls.append(("alpha", source, 0))
        return b"alpha-webm"

    async def pack_sheet(source: bytes, frame_count: int) -> ffmpeg.SpriteSheetPack:
        calls.append(("sprite", source, frame_count))
        return ffmpeg.SpriteSheetPack(
            png=b"sprite-png",
            metadata=ffmpeg.SpriteSheetMetadata(
                frame_count=32,
                grid_columns=8,
                grid_rows=4,
                frame_width=512,
                frame_height=512,
                frame_duration_ms=140,
            ),
        )

    monkeypatch.setattr(ffmpeg, "_build_alpha_video", build_alpha)
    monkeypatch.setattr(ffmpeg, "_pack_sprite_sheet", pack_sheet)

    result = await ffmpeg.FfmpegVideoProcessor().build_derivatives(
        b"source-mp4", {"alpha": True, "sprite": True}
    )

    assert result.alpha_webm == b"alpha-webm"
    assert result.sprite_sheet_png == b"sprite-png"
    assert result.sprite_metadata == ffmpeg.SpriteSheetMetadata(
        frame_count=32,
        grid_columns=8,
        grid_rows=4,
        frame_width=512,
        frame_height=512,
        frame_duration_ms=140,
    )
    assert calls == [
        ("alpha", b"source-mp4", 0),
        ("sprite", b"source-mp4", 32),
    ]


async def test_build_derivatives_runs_only_requested_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_alpha(_source: bytes) -> bytes:
        raise AssertionError("alpha encoder ran for a sprite-only job")

    async def pack_sheet(_source: bytes, _frame_count: int) -> ffmpeg.SpriteSheetPack:
        return ffmpeg.SpriteSheetPack(
            png=b"sprite-png",
            metadata=ffmpeg.SpriteSheetMetadata(32, 8, 4, 512, 512, 125),
        )

    monkeypatch.setattr(ffmpeg, "_build_alpha_video", unexpected_alpha)
    monkeypatch.setattr(ffmpeg, "_pack_sprite_sheet", pack_sheet)

    result = await ffmpeg.FfmpegVideoProcessor().build_derivatives(
        b"source-mp4", {"alpha": False, "sprite": True}
    )

    assert result.alpha_webm is None
    assert result.sprite_sheet_png == b"sprite-png"
    assert result.sprite_metadata is not None


def test_sprite_timing_preserves_source_duration() -> None:
    assert ffmpeg._frame_duration_ms(60.0, 32) == 1875


def test_alpha_encode_budget_rejects_work_above_packaging_ceiling() -> None:
    maximum = ffmpeg.MAX_ALPHA_ENCODE_SECONDS
    oversized_pixel_seconds = (
        (maximum + 1 - ffmpeg.ALPHA_ENCODE_FIXED_SECONDS)
        * 1_000_000
        / ffmpeg.ALPHA_ENCODE_SECONDS_PER_MEGAPIXEL_SECOND
    )

    with pytest.raises(ValueError, match="too large to package"):
        ffmpeg.alpha_encode_budget_seconds(oversized_pixel_seconds)


async def test_alpha_builder_rejects_oversized_clip_before_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maximum = ffmpeg.MAX_ALPHA_ENCODE_SECONDS
    oversized_pixel_seconds = (
        (maximum + 1 - ffmpeg.ALPHA_ENCODE_FIXED_SECONDS)
        * 1_000_000
        / ffmpeg.ALPHA_ENCODE_SECONDS_PER_MEGAPIXEL_SECOND
    )

    async def probe(_source):
        return oversized_pixel_seconds

    async def unexpected_encoder(*_args, **_kwargs):
        raise AssertionError("encoder started for a rejected workload")

    monkeypatch.setattr(ffmpeg, "_probe_video_metrics", probe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_encoder)

    with pytest.raises(ffmpeg.TerminalVideoError, match="too large to package"):
        await ffmpeg._build_alpha_video(b"oversized-source")
