"""The studio's preset motion library.

Ports the reference game's slot -> animation_type table and slot -> prompt
template (`frontend/src/components/admin/animation-editor/videoLabHandoff.js:28-62`)
into typed, frozen definitions the studio owns directly. Purely neutral
vocabulary: how a preset's `semantic_slot` maps onto any one target's own
slot names is entirely that target adapter's own `TargetDescriptor.slots`
business now (see the reference adapter under `magic_video_editor.targets`
for a worked example of `walk` -> `move`, `hurt`/`trip` -> `trip` aliasing).

Prompts are templates, not finished copy -- `The character` is filled at
motion-add time from the project name (the manifest carries no subject display
name yet; see `StudioService._compile_preset_specification`).
"""

from magic_video_editor.studio.models import (
    AnimationType,
    FrozenModel,
    LoopPolicy,
    MotionPreset,
)


class PresetDefinition(FrozenModel):
    preset: MotionPreset
    display_name: str
    description: str
    semantic_slot: str
    loop_policy: LoopPolicy
    animation_type: AnimationType
    prompt_template: str
    default_action: str


_LOOPING_PRESETS = frozenset({MotionPreset.IDLE, MotionPreset.WALK, MotionPreset.RUN})

_DESCRIPTIONS: dict[MotionPreset, str] = {
    MotionPreset.IDLE: "Resting loop with subtle breathing motion.",
    MotionPreset.WALK: "Steady forward walk cycle.",
    MotionPreset.RUN: "Full sprint cycle.",
    MotionPreset.JUMP: "Jump up and land.",
    MotionPreset.ATTACK: "One decisive weapon swing.",
    MotionPreset.GUARD: "Braced block, held steady.",
    MotionPreset.CAST: "Channel and release a spell.",
    MotionPreset.HURT: "React to a hit and recover stance.",
    MotionPreset.DEATH: "Collapse from a fatal blow.",
    MotionPreset.TRIP: "Sweep low to trip an opponent.",
}

# The editable "what happens" line each preset starts from; the operator can replace it per
# motion ("draws and fires the bow") before anything renders.
_DEFAULT_ACTIONS: dict[MotionPreset, str] = {
    MotionPreset.IDLE: "breathes and shifts weight",
    MotionPreset.WALK: "walks in place",
    MotionPreset.RUN: "runs at a full sprint",
    MotionPreset.JUMP: "jumps up and lands",
    MotionPreset.ATTACK: "swings a weapon",
    MotionPreset.GUARD: "raises a guard",
    MotionPreset.CAST: "casts a spell with a flourish",
    MotionPreset.HURT: "flinches from a hit",
    MotionPreset.DEATH: "collapses from a fatal blow",
    MotionPreset.TRIP: "sweeps low to trip an opponent",
}

# Ported from `PROMPT_BY_SLOT` where the reference game's slot exists; the four studio-only
# presets (run, jump, hurt, death) are authored fresh in the same voice. `{action}` is the
# operator's own "what happens" line, defaulted from `_DEFAULT_ACTIONS`.
_PROMPT_TEMPLATES: dict[MotionPreset, str] = {
    MotionPreset.IDLE: "The character {action} — a subtle idle loop with minimal drift.",
    MotionPreset.WALK: "The character {action} — a clean, seamless walk cycle.",
    MotionPreset.RUN: "The character {action} — a clean, seamless run cycle.",
    MotionPreset.JUMP: "The character {action}: crouch, launch, airborne, landing recovery.",
    MotionPreset.ATTACK: "The character {action}: a short readable wind-up, one quick committed strike, brief follow-through, then naturally returns to the initial ready stance and holds. Complete the action within the clip, without a second attack. Maintain the reference facing, fixed camera and constant body size. Keep the complete character and weapon visible throughout with generous space around the full swing.",
    MotionPreset.GUARD: "The character {action} and braces behind it, holding the block steady.",
    MotionPreset.CAST: "The character {action}: hands rise, energy gathers, release.",
    MotionPreset.HURT: "The character {action}: stagger, then recover their stance.",
    MotionPreset.DEATH: "The character {action}: stagger, fall, come to rest.",
    MotionPreset.TRIP: "The character {action}: committed step, leg sweep, recovery.",
}

# `ANIMATION_TYPE_BY_SLOT` from the reference game: idle/move/trip/guard -> movement,
# attack -> attack, cast -> spellcast. The studio-only presets (run, jump, hurt, death)
# follow the same movement fallback the reference game uses for anything without its
# own type.
_ANIMATION_TYPES: dict[MotionPreset, AnimationType] = {
    MotionPreset.ATTACK: AnimationType.ATTACK,
    MotionPreset.CAST: AnimationType.SPELLCAST,
}

PRESET_DEFINITIONS: dict[MotionPreset, PresetDefinition] = {
    preset: PresetDefinition(
        preset=preset,
        display_name=preset.value.capitalize(),
        description=_DESCRIPTIONS[preset],
        semantic_slot=preset.value,
        loop_policy=LoopPolicy.LOOP if preset in _LOOPING_PRESETS else LoopPolicy.ONCE,
        animation_type=_ANIMATION_TYPES.get(preset, AnimationType.MOVEMENT),
        prompt_template=_PROMPT_TEMPLATES[preset],
        default_action=_DEFAULT_ACTIONS[preset],
    )
    for preset in MotionPreset
}

# The reference game's six, in the Video Lab's own order (`SLOT_BATCH` in
# videoLabSlotBatch.js), expressed as studio presets. "Add the recommended set" adds
# exactly these.
RECOMMENDED_PRESET_SET: tuple[MotionPreset, ...] = (
    MotionPreset.IDLE,
    MotionPreset.WALK,
    MotionPreset.ATTACK,
    MotionPreset.GUARD,
    MotionPreset.CAST,
    MotionPreset.HURT,
)
