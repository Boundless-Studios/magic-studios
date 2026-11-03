"""The reference Example Engine adapter is discovered the same way a
private, third-party adapter would be: through the
`magic_video_editor.targets` entry-point group declared in `pyproject.toml`.
"""

from magic_video_editor.targets import TargetRegistry


def test_from_entry_points_discovers_the_installed_example_adapter() -> None:
    registry = TargetRegistry.from_entry_points()

    assert "example" in registry
    adapter = registry.get("example")
    assert adapter is not None
    assert adapter.descriptor.system == "example"
    assert "example" in registry.systems()
