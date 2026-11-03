"""The Example Engine reference adapter runs the shared contract suite --
network-free by construction, since `deliver` only records to an in-memory
list."""

from magic_video_editor.targets.example.adapter import ExampleTargetAdapter

from .contract import TargetAdapterContract


class TestExampleAdapterContract(TargetAdapterContract):
    def adapter(self) -> ExampleTargetAdapter:
        return ExampleTargetAdapter()
