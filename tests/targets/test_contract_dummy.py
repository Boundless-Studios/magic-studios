"""A minimal, deliberately non-reference-adapter dummy adapter proves the
contract suite is adapter-agnostic: it is not reaching into any one
adapter's specific behavior."""

from collections.abc import Mapping

from magic_video_editor.targets import (
    Handoff,
    DeliveryRequest,
    DeliveryResult,
    NeutralPackage,
    TargetDescriptor,
    TargetSlot,
)

from .contract import TargetAdapterContract

_DESCRIPTOR = TargetDescriptor(
    system="dummy",
    display_name="Dummy Target",
    entity_kinds=("widget",),
    slots=(
        TargetSlot(name="spin", accepts=("spin", "twirl"), winner="spin"),
        TargetSlot(name="rest", accepts=("rest",), winner="rest"),
    ),
)


class DummyTargetAdapter:
    descriptor = _DESCRIPTOR

    def slot_for(self, semantic_slot: str) -> TargetSlot | None:
        return self.descriptor.slot_for(semantic_slot)

    def export_files(self, package: NeutralPackage) -> Mapping[str, bytes]:
        return {
            "package.json": (
                f'{{"release_id":"{package.release_id}",'
                f'"motion_count":{len(package.motions)}}}'
            ).encode()
        }

    async def deliver(self, delivery: DeliveryRequest) -> DeliveryResult:
        return DeliveryResult.delivered({"received_release_id": delivery.release_id})

    def parse_handoff(self, params: Mapping[str, str]) -> Handoff | None:
        entity_kind = params.get("entity_kind")
        entity_id = params.get("entity_id")
        display_name = params.get("name")
        if not entity_kind or not entity_id or not display_name:
            return None
        if entity_kind not in self.descriptor.entity_kinds:
            return None
        slot_param = params.get("slot")
        semantic_slot: str | None = None
        if slot_param:
            slot = self.slot_for(slot_param)
            if slot is None:
                return None
            semantic_slot = slot_param
        return Handoff(
            entity_kind=entity_kind,
            entity_id=entity_id,
            display_name=display_name,
            semantic_slot=semantic_slot,
        )


class TestDummyAdapterContract(TargetAdapterContract):
    def adapter(self) -> DummyTargetAdapter:
        return DummyTargetAdapter()
