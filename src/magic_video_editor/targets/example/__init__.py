"""The Example Engine target adapter -- this repository's reference
implementation of `magic_video_editor.targets.TargetAdapter`. Registered
under the `magic_video_editor.targets` entry-point group as `example`; a
private, downstream game studio's own adapter is installed and discovered
the exact same way (see the README's "Installing a private target adapter"
section).
"""

from magic_video_editor.targets.example.adapter import (
    ExampleTargetAdapter,
    create_adapter,
)

__all__ = ["ExampleTargetAdapter", "create_adapter"]
