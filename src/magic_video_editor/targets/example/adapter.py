"""The Example Engine target adapter: the studio's reference implementation
of `TargetAdapter`. It is deliberately network-free -- `deliver` records the
package it received to an in-memory list and returns a summary -- so it can
be used directly in tests and local/E2E development without a second live
service. A real target adapter (a private, downstream package installed via
`TARGET_ADAPTER_PACKAGES`; see the README's "Installing a private target
adapter" section) would `deliver` over HTTP with the same retryable/terminal
rigor the studio's other HTTP plugins use.
"""

import json
import zipfile
from collections.abc import Mapping
from io import BytesIO

from magic_video_editor.targets import (
    Handoff,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryResult,
    NeutralPackage,
    TargetDescriptor,
    TargetSlot,
)

_SCHEMA_VERSION = 1

DESCRIPTOR = TargetDescriptor(
    system="example",
    display_name="Example Engine",
    entity_kinds=("character", "prop"),
    package_version="1",
    slots=(
        TargetSlot(name="idle", accepts=("idle",), winner="idle"),
        TargetSlot(name="walk", accepts=("walk",), winner="walk"),
        TargetSlot(name="attack", accepts=("attack",), winner="attack"),
    ),
)


class ExampleTargetAdapter:
    """Descriptor, neutral-package projection, and an in-memory "delivery" --
    the pattern a third-party adapter follows, minus the network call.
    """

    descriptor: TargetDescriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.deliveries: list[dict[str, object]] = []

    def slot_for(self, semantic_slot: str) -> TargetSlot | None:
        return self.descriptor.slot_for(semantic_slot)

    def export_files(self, package: NeutralPackage) -> Mapping[str, bytes]:
        strips: list[dict[str, object]] = []
        for motion in sorted(package.motions, key=lambda item: item.motion_id):
            slot = self.slot_for(motion.semantic_slot)
            strips.append(
                {
                    "motion_id": motion.motion_id,
                    "slot": slot.name if slot is not None else None,
                    "animation_type": motion.animation_type,
                    "sheet": motion.sheet_member_path,
                    "sha256": motion.sha256,
                    "frame_width": motion.frame_width,
                    "pivot_x": motion.pivot_x,
                    "pivot_y": motion.pivot_y,
                    "frame_height": motion.frame_height,
                    "frames": list(motion.frames),
                    "columns": motion.columns,
                    "rows": 1,
                    "frame_duration_ms": motion.frame_duration_ms,
                    "loop_mode": motion.loop_mode,
                    # Facing is baked into the exported pixels, so the runtime
                    # flip stays off -- otherwise the sprite is mirrored twice.
                    "flip": False,
                }
            )
        members: dict[str, bytes] = {
            "package.json": _canonical_json(
                {
                    "format": {
                        "name": self.descriptor.system,
                        "version": self.descriptor.package_version,
                    },
                    "schema_version": _SCHEMA_VERSION,
                    "source_package_id": package.release_id,
                    "strips": strips,
                }
            )
        }
        bindings = self._bindings(package)
        if bindings is not None:
            members["bindings.json"] = _canonical_json(bindings)
        return members

    def _bindings(self, package: NeutralPackage) -> list[dict[str, object]] | None:
        """`None` -- not an empty list -- when the project carries no bound
        external subject: there is nothing this package could bind to."""
        subject = package.external_subject
        if subject is None:
            return None
        bindings: list[dict[str, object]] = []
        for motion in sorted(package.motions, key=lambda item: item.motion_id):
            slot = self.slot_for(motion.semantic_slot)
            if slot is None:
                continue
            bindings.append(
                {
                    "entity_kind": subject.entity_kind,
                    "entity_id": subject.entity_id,
                    "slot": slot.name,
                    "overrides": {
                        "frame_duration_ms": motion.frame_duration_ms,
                        "loop_mode": motion.loop_mode,
                        "flip": False,
                    },
                }
            )
        return bindings

    async def deliver(self, delivery: DeliveryRequest) -> DeliveryResult:
        package_path = f"{delivery.system}/package.json"
        bindings_path = f"{delivery.system}/bindings.json"
        try:
            with zipfile.ZipFile(BytesIO(delivery.archive)) as archive:
                names = archive.namelist()
                if package_path not in names:
                    return DeliveryResult.failed(
                        f"archive is missing {package_path}", retryable=False
                    )
                package = json.loads(archive.read(package_path))
                bindings = (
                    json.loads(archive.read(bindings_path))
                    if bindings_path in names
                    else []
                )
        except zipfile.BadZipFile as exc:
            return DeliveryResult.failed(str(exc), retryable=False)
        except ValueError:
            return DeliveryResult.failed(
                "example package.json was not valid JSON", retryable=False
            )
        strips = package.get("strips", [])
        self.deliveries.append(
            {
                "release_id": delivery.release_id,
                "package": package,
                "bindings": bindings,
            }
        )
        summary = f"{len(strips)} motions, {len(bindings)} bindings"
        return DeliveryResult.delivered(
            DeliveryReceipt(
                summary=summary,
                details={"motion_count": len(strips), "binding_count": len(bindings)},
            )
        )

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


def create_adapter() -> ExampleTargetAdapter:
    return ExampleTargetAdapter()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
