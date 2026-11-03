"""The Example Engine's own slot vocabulary lives entirely on the adapter's
descriptor -- a straight, one-to-one mapping, unlike a real target that may
alias several neutral slots onto one of its own (see `TargetSlot.accepts`
and the contract's collision test for that case)."""

from magic_video_editor.targets.example.adapter import DESCRIPTOR


def test_example_slots_map_one_to_one_to_the_matching_neutral_slot() -> None:
    for semantic_slot in ("idle", "walk", "attack"):
        slot = DESCRIPTOR.slot_for(semantic_slot)
        assert slot is not None
        assert slot.name == semantic_slot
        assert slot.winner == semantic_slot


def test_studio_only_slots_have_no_example_mapping() -> None:
    for semantic_slot in ("run", "jump", "death", "guard", "cast", "hurt", "trip"):
        assert DESCRIPTOR.slot_for(semantic_slot) is None
