from magic_video_editor.studio.models import AnimationType, LoopPolicy, MotionPreset
from magic_video_editor.studio.presets import PRESET_DEFINITIONS, RECOMMENDED_PRESET_SET


def test_every_preset_has_a_definition() -> None:
    assert set(PRESET_DEFINITIONS) == set(MotionPreset)


def test_preset_animation_types_match_the_neutral_vocabulary() -> None:
    # Slot aliasing (which studio presets share a target's own slot) is
    # entirely target-adapter business now -- see tests/targets/contract.py
    # and each adapter's own tests. Only animation_type stays a studio-owned,
    # neutral fact about a preset.
    expectations = {
        MotionPreset.IDLE: AnimationType.MOVEMENT,
        MotionPreset.WALK: AnimationType.MOVEMENT,
        MotionPreset.ATTACK: AnimationType.ATTACK,
        MotionPreset.GUARD: AnimationType.MOVEMENT,
        MotionPreset.CAST: AnimationType.SPELLCAST,
        MotionPreset.HURT: AnimationType.MOVEMENT,
        MotionPreset.TRIP: AnimationType.MOVEMENT,
        MotionPreset.RUN: AnimationType.MOVEMENT,
        MotionPreset.JUMP: AnimationType.MOVEMENT,
        MotionPreset.DEATH: AnimationType.MOVEMENT,
    }
    for preset, animation_type in expectations.items():
        assert PRESET_DEFINITIONS[preset].animation_type is animation_type


def test_only_idle_walk_run_loop_the_rest_play_once() -> None:
    looping = {MotionPreset.IDLE, MotionPreset.WALK, MotionPreset.RUN}
    for preset, definition in PRESET_DEFINITIONS.items():
        expected = LoopPolicy.LOOP if preset in looping else LoopPolicy.ONCE
        assert definition.loop_policy is expected, preset


def test_prompt_templates_use_generic_character_and_authored_action() -> None:
    for definition in PRESET_DEFINITIONS.values():
        assert "{subject_name}" not in definition.prompt_template
        assert "{action}" in definition.prompt_template
        assert definition.default_action
        composed = definition.prompt_template.format(
            subject_name="Ember", action=definition.default_action
        )
        assert composed.startswith(f"The character {definition.default_action}")
        assert "Ember" not in composed


def test_recommended_preset_set_mirrors_the_reference_slot_batch_order() -> None:
    assert RECOMMENDED_PRESET_SET == (
        MotionPreset.IDLE,
        MotionPreset.WALK,
        MotionPreset.ATTACK,
        MotionPreset.GUARD,
        MotionPreset.CAST,
        MotionPreset.HURT,
    )
