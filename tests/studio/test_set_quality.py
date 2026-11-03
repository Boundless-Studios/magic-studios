from io import BytesIO

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from magic_video_editor.studio import service
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    MotionState,
)
from magic_video_editor.studio.quality import (
    MAX_MOTION_DECODED_PIXELS,
    MotionMeasurementBudgetExceeded,
    assess_motion,
    assess_set,
    inspect_ffprobe_metadata,
    inspect_sprite_png,
)


def _candidate(
    candidate_id: str,
    *,
    width: int = 64,
    baseline: int | None = 52,
    duplicate_terminal_frame: bool = False,
    subject_revision_id: str | None = None,
) -> MotionCandidate:
    return MotionCandidate(
        candidate_id=candidate_id,
        artifact_key=f"outputs/{candidate_id}.png",
        sha256="a" * 64,
        subject_revision_id=subject_revision_id,
        metadata=MotionFrameMetadata(
            decodable=True,
            media_kind=MediaKind.SHEET,
            frame_count=4,
            width=width,
            height=64,
            constant_geometry=True,
            has_alpha=True,
            duplicate_terminal_frame=duplicate_terminal_frame,
            baseline_y=baseline,
            packing_estimate=ExportPackingEstimate(width=width * 4, height=64),
        ),
    )


def test_motion_quality_checks_static_sprite_invariants() -> None:
    candidate = _candidate("c1")
    assessment = assess_motion(candidate, LoopPolicy.LOOP)
    assert assessment.blocking_findings == ()

    broken = candidate.model_copy(
        update={
            "metadata": candidate.metadata.model_copy(
                update={"frame_count": 0, "has_alpha": False}
            )
        }
    )
    codes = {
        finding.code for finding in assess_motion(broken, LoopPolicy.LOOP).findings
    }
    assert {"motion_no_frames", "motion_missing_alpha"} <= codes


def test_loop_policies_reject_duplicate_terminal_only_for_loop() -> None:
    candidate = _candidate("c1", duplicate_terminal_frame=True)
    loop_codes = {f.code for f in assess_motion(candidate, LoopPolicy.LOOP).findings}
    once_codes = {f.code for f in assess_motion(candidate, LoopPolicy.ONCE).findings}
    ping_pong_codes = {
        f.code for f in assess_motion(candidate, LoopPolicy.PING_PONG).findings
    }
    assert "motion_duplicate_terminal_loop_frame" in loop_codes
    assert "motion_duplicate_terminal_loop_frame" not in once_codes | ping_pong_codes
    assert "motion_duplicate_ping_pong_terminal_frame" in ping_pong_codes


def test_set_quality_checks_selection_canvas_baseline_slots_and_texture_limit() -> None:
    idle = MotionState(
        specification=MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            semantic_slot="idle",
        ),
        candidates=(_candidate("idle-c"),),
        selected_candidate_id="idle-c",
    )
    walk = MotionState(
        specification=MotionSpecification(
            motion_id="walk",
            name="Walk",
            kind=MotionKind.CUSTOM,
            prompt="walk",
            semantic_slot="idle",
        ),
        candidates=(_candidate("walk-c", width=128, baseline=50),),
        selected_candidate_id="walk-c",
    )
    codes = {
        finding.code
        for finding in assess_set((idle, walk), max_texture_dimension=256).findings
    }
    assert "set_canvas_mismatch" in codes
    assert "set_baseline_mismatch" in codes
    assert "set_semantic_slot_duplicate" in codes
    assert "set_texture_limit_exceeded" in codes


def test_set_quality_reports_missing_required_selection() -> None:
    motion = MotionState(
        specification=MotionSpecification(
            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
        )
    )
    assert "set_required_selection_missing" in {
        f.code for f in assess_set((motion,)).findings
    }


def test_set_quality_flags_a_required_motions_stale_selected_candidate() -> None:
    motion = MotionState(
        specification=MotionSpecification(
            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
        ),
        candidates=(_candidate("idle-c", subject_revision_id="subject-1"),),
        selected_candidate_id="idle-c",
    )
    codes = {
        f.code
        for f in assess_set(
            (motion,), current_subject_revision_id="subject-2"
        ).findings
    }
    assert "motion_stale_subject" in codes


def test_set_quality_does_not_flag_a_candidate_matching_the_current_subject() -> None:
    motion = MotionState(
        specification=MotionSpecification(
            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
        ),
        candidates=(_candidate("idle-c", subject_revision_id="subject-1"),),
        selected_candidate_id="idle-c",
    )
    codes = {
        f.code
        for f in assess_set(
            (motion,), current_subject_revision_id="subject-1"
        ).findings
    }
    assert "motion_stale_subject" not in codes


def test_set_quality_does_not_flag_pre_lineage_candidates_with_no_subject_revision() -> None:
    motion = MotionState(
        specification=MotionSpecification(
            motion_id="idle", name="Idle", preset=MotionPreset.IDLE
        ),
        candidates=(_candidate("idle-c", subject_revision_id=None),),
        selected_candidate_id="idle-c",
    )
    codes = {
        f.code
        for f in assess_set(
            (motion,), current_subject_revision_id="subject-2"
        ).findings
    }
    assert "motion_stale_subject" not in codes


def test_pillow_metadata_detects_duplicate_terminal_frame_and_alpha() -> None:
    first = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
    second = Image.new("RGBA", (16, 16), (0, 255, 0, 128))
    output = BytesIO()
    first.save(output, format="PNG", save_all=True, append_images=[second, first])

    metadata = inspect_sprite_png(output.getvalue())

    assert metadata.frame_count == 3
    assert metadata.has_alpha is True
    assert metadata.duplicate_terminal_frame is True
    assert metadata.constant_geometry is True
    assert metadata.media_kind is MediaKind.SHEET


def test_pillow_metadata_reads_declared_horizontal_sprite_sheet_frames() -> None:
    sheet = Image.new("RGBA", (64, 16), (0, 0, 0, 0))
    for index, color in enumerate(("red", "green", "blue", "yellow")):
        sheet.paste(Image.new("RGBA", (16, 16), color), (index * 16, 0))
    png_info = PngInfo()
    png_info.add_text("studio_frame_count", "4")
    output = BytesIO()
    sheet.save(output, format="PNG", pnginfo=png_info)

    metadata = inspect_sprite_png(output.getvalue())

    assert metadata.frame_count == 4
    assert metadata.width == 64
    assert metadata.height == 16
    assert metadata.duplicate_terminal_frame is False
    assert metadata.packing_estimate.width == 64


def test_pillow_metadata_rejects_excessive_declared_sprite_frames() -> None:
    import pytest

    png_info = PngInfo()
    png_info.add_text("studio_frame_count", "2001")
    output = BytesIO()
    Image.new("RGBA", (2001, 1)).save(output, format="PNG", pnginfo=png_info)

    with pytest.raises(ValueError, match="frame count"):
        inspect_sprite_png(output.getvalue())


def test_ffprobe_metadata_detects_frame_geometry_and_alpha() -> None:
    metadata = inspect_ffprobe_metadata(
        {
            "streams": [{"width": 64, "height": 96, "pix_fmt": "yuva444p"}],
            "frames": [
                {"width": 64, "height": 96},
                {"width": 64, "height": 96},
            ],
        }
    )

    assert metadata.frame_count == 2
    assert metadata.constant_geometry is True
    assert metadata.has_alpha is True
    assert metadata.media_kind is MediaKind.VIDEO

    gray = inspect_ffprobe_metadata(
        {"streams": [{"width": 64, "height": 96, "pix_fmt": "gray"}], "frames": []}
    )
    rgba = inspect_ffprobe_metadata(
        {"streams": [{"width": 64, "height": 96, "pix_fmt": "rgba"}], "frames": []}
    )
    assert gray.has_alpha is False
    assert rgba.has_alpha is True

    duplicate = inspect_ffprobe_metadata(
        {
            "streams": [{"width": 64, "height": 96, "pix_fmt": "yuva444p"}],
            "frames": [{}, {}, {}],
        },
        frame_hashes=("aaa", "bbb", "aaa"),
    )
    assert duplicate.duplicate_terminal_frame is True


def test_ffprobe_metadata_rejects_hostile_frame_count_before_decode() -> None:
    import pytest

    with pytest.raises(ValueError, match="frame count"):
        inspect_ffprobe_metadata(
            {
                "streams": [
                    {
                        "width": 64,
                        "height": 64,
                        "pix_fmt": "rgba",
                        "nb_read_frames": "999999",
                    }
                ]
            }
        )


def test_ffprobe_metadata_accepts_the_default_preset_output() -> None:
    """Production regression: a 512x512, 107-frame h264 mp4 from the studio's own
    default generation preset is 28,049,408 decoded pixels -- comfortably inside the
    budget, so it must be reported decodable rather than rejected."""
    metadata = inspect_ffprobe_metadata(
        {
            "streams": [
                {
                    "width": 512,
                    "height": 512,
                    "pix_fmt": "yuv420p",
                    "nb_read_frames": "107",
                }
            ],
            "frames": [],
        }
    )

    assert metadata.decodable is True
    assert metadata.frame_count == 107


def test_ffprobe_metadata_rejects_decoded_pixels_over_the_budget_distinctly() -> None:
    import pytest

    with pytest.raises(MotionMeasurementBudgetExceeded):
        inspect_ffprobe_metadata(
            {
                "streams": [
                    {
                        "width": 8000,
                        "height": 8000,
                        "pix_fmt": "rgba",
                        "nb_read_frames": "2000",
                    }
                ],
                "frames": [],
            }
        )


def test_motion_measurement_budget_exceeded_is_a_value_error() -> None:
    assert issubclass(MotionMeasurementBudgetExceeded, ValueError)


def test_studio_pool_bounds_fit_inside_the_motion_decode_budget() -> None:
    largest_dimension = service._DIMENSION_BOUNDS[1]
    longest_clip = service._LENGTH_BOUNDS[1]
    assert largest_dimension**2 * longest_clip <= MAX_MOTION_DECODED_PIXELS


def test_different_strip_frame_counts_do_not_mean_different_cell_canvases():
    motions = []
    for name, count in (("walk", 12), ("attack", 16)):
        candidate = _candidate(name)
        candidate = candidate.model_copy(update={"metadata": candidate.metadata.model_copy(update={
            "frame_count": count, "width": 256 * count, "height": 256,
            "packing_estimate": ExportPackingEstimate(width=256 * count, height=256),
        })})
        motions.append(MotionState(specification=MotionSpecification(motion_id=name, name=name, preset=MotionPreset(name)),
                                   candidates=(candidate,), selected_candidate_id=name))
    assert "set_canvas_mismatch" not in {f.code for f in assess_set(tuple(motions)).findings}
