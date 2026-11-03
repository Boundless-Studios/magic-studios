"""Reusable pytest contract suite for `magic_video_editor.targets.TargetAdapter`
implementations. Any adapter -- in-repo or third-party -- proves it satisfies
the interface by subclassing `TargetAdapterContract` and overriding
`adapter()` to return a network-free instance of its own adapter.

A third-party package runs this suite in its own CI by depending on this
repository's `tests/` package and importing it:

    from tests.targets.contract import TargetAdapterContract

    class TestMyAdapterContract(TargetAdapterContract):
        def adapter(self):
            return MyTargetAdapter(client=FakeHttpClient())
"""

from magic_video_editor.targets import (
    DeliveryRequest,
    DeliveryResult,
    NeutralMotion,
    NeutralPackage,
    TargetAdapter,
    TargetConfig,
)


def _motion(*, motion_id: str = "motion-a", semantic_slot: str) -> NeutralMotion:
    return NeutralMotion(
        motion_id=motion_id,
        semantic_slot=semantic_slot,
        animation_type="movement",
        candidate_id="candidate-1",
        sha256="0" * 64,
        frame_duration_ms=100,
        frames=(0, 1, 2),
        columns=3,
        frame_width=64,
        frame_height=64,
        loop_mode="loop",
        ping_pong=False,
        sheet_member_path=f"motions/{motion_id}.png",
        sheet_bytes=b"sheet-bytes",
    )


def _package(adapter: TargetAdapter) -> NeutralPackage:
    slot_name = adapter.descriptor.slots[0].accepts[0]
    return NeutralPackage(
        release_id="release-1",
        project_id="project-1",
        manifest_digest="manifest-digest",
        release_digest="release-digest",
        motions=(_motion(semantic_slot=slot_name),),
    )


class TargetAdapterContract:
    """Override `adapter()`; every `test_*` method below runs against it."""

    def adapter(self) -> TargetAdapter:
        raise NotImplementedError("subclasses must return a TargetAdapter instance")

    def test_descriptor_declares_at_least_one_entity_kind_and_slot(self) -> None:
        adapter = self.adapter()
        assert adapter.descriptor.system
        assert adapter.descriptor.entity_kinds
        assert adapter.descriptor.slots

    def test_slot_for_is_defined_for_every_declared_neutral_slot(self) -> None:
        adapter = self.adapter()
        for slot in adapter.descriptor.slots:
            for neutral in slot.accepts:
                resolved = adapter.slot_for(neutral)
                assert resolved is not None, f"{neutral!r} must resolve to a slot"
                assert neutral in resolved.accepts

    def test_slot_for_returns_none_explicitly_for_an_unknown_slot(self) -> None:
        adapter = self.adapter()
        known = {
            neutral for slot in adapter.descriptor.slots for neutral in slot.accepts
        }
        unknown = "definitely-not-a-declared-slot"
        assert unknown not in known
        assert adapter.slot_for(unknown) is None

    def test_slot_collisions_resolve_to_the_declared_winner(self) -> None:
        adapter = self.adapter()
        for slot in adapter.descriptor.slots:
            for neutral in slot.accepts:
                resolved = adapter.slot_for(neutral)
                assert resolved is not None
                assert resolved.winner == slot.winner

    def test_export_files_is_deterministic_for_a_fixed_package(self) -> None:
        adapter = self.adapter()
        package = _package(adapter)
        first = dict(adapter.export_files(package))
        second = dict(adapter.export_files(package))
        assert first == second

    def test_export_files_only_writes_relative_target_namespaced_paths(self) -> None:
        adapter = self.adapter()
        package = _package(adapter)
        members = adapter.export_files(package)
        assert members
        for member_path in members:
            assert member_path
            assert not member_path.startswith("/")
            assert ".." not in member_path.split("/")

    async def test_deliver_is_idempotent_on_release_id_and_never_raises(self) -> None:
        adapter = self.adapter()
        request = DeliveryRequest(
            release_id="release-1",
            system=adapter.descriptor.system,
            archive=b"archive-bytes",
            config=TargetConfig(url="https://target.example/import", token="token"),
        )
        first = await adapter.deliver(request)
        second = await adapter.deliver(request)
        assert isinstance(first, DeliveryResult)
        assert isinstance(second, DeliveryResult)

    async def test_a_delivery_failure_leaves_the_delivery_request_unchanged(
        self,
    ) -> None:
        adapter = self.adapter()
        archive = b"archive-bytes"
        request = DeliveryRequest(
            release_id="release-1",
            system=adapter.descriptor.system,
            archive=archive,
            config=TargetConfig(url="https://target.example/import", token="token"),
        )
        await adapter.deliver(request)
        assert request.archive == archive
        assert request.release_id == "release-1"

    def test_parse_handoff_rejects_an_unknown_entity_kind(self) -> None:
        adapter = self.adapter()
        assert (
            adapter.parse_handoff(
                {
                    "target": adapter.descriptor.system,
                    "entity_kind": "definitely-not-a-declared-kind",
                    "entity_id": "entity-1",
                    "name": "Display Name",
                }
            )
            is None
        )

    def test_parse_handoff_rejects_an_unknown_slot(self) -> None:
        adapter = self.adapter()
        assert (
            adapter.parse_handoff(
                {
                    "target": adapter.descriptor.system,
                    "entity_kind": adapter.descriptor.entity_kinds[0],
                    "entity_id": "entity-1",
                    "name": "Display Name",
                    "slot": "definitely-not-a-declared-slot",
                }
            )
            is None
        )

    def test_parse_handoff_never_invents_values_it_was_not_given(self) -> None:
        adapter = self.adapter()
        assert adapter.parse_handoff({"target": adapter.descriptor.system}) is None

    def test_parse_handoff_accepts_a_known_entity_kind_and_slot(self) -> None:
        adapter = self.adapter()
        slot_name = adapter.descriptor.slots[0].accepts[0]
        intent = adapter.parse_handoff(
            {
                "target": adapter.descriptor.system,
                "entity_kind": adapter.descriptor.entity_kinds[0],
                "entity_id": "entity-1",
                "name": "Display Name",
                "slot": slot_name,
            }
        )
        assert intent is not None
        assert intent.entity_kind == adapter.descriptor.entity_kinds[0]
        assert intent.entity_id == "entity-1"
        assert intent.display_name == "Display Name"
        assert intent.semantic_slot == slot_name
