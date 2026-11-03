from magic_video_editor.studio.models import MotionGenerationPreset, MotionGenerationSettings


def test_preset_defaults_match_the_reference_video_lab() -> None:
    # Ported from the reference video lab's own preset defaults.
    assert MotionGenerationSettings.PRESET_DEFAULTS == {
        MotionGenerationPreset.QUICK: {"steps": 16, "width": 864, "height": 480},
        MotionGenerationPreset.STANDARD: {"steps": 20, "width": 1344, "height": 768},
        MotionGenerationPreset.HIGH: {"steps": 28, "width": 1344, "height": 768},
        MotionGenerationPreset.CINEMATIC: {"steps": 32, "width": 1344, "height": 768},
    }


def test_for_preset_seeds_steps_and_size_from_the_preset() -> None:
    settings = MotionGenerationSettings.for_preset(MotionGenerationPreset.HIGH)
    assert settings.steps == 28
    assert settings.width == 1344
    assert settings.height == 768
    assert settings.preset is MotionGenerationPreset.HIGH


def test_for_preset_lets_an_explicit_override_win() -> None:
    settings = MotionGenerationSettings.for_preset(MotionGenerationPreset.HIGH, width=999)
    assert settings.width == 999
    assert settings.steps == 28
