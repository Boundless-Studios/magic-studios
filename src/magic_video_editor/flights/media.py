"""Scene-flight recipe constants, executor support checks and seam measurement."""

from __future__ import annotations

import asyncio
import tempfile
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from magic_video_editor.cpu_worker import MAX_VIDEO_BYTES, VIDEO_INPUT, _run

# The same registered H3 FL2VA capability and canvas Movie Studio uses on both
# the desktop presets and the standard 24 GB worker: 768x448 is the nearest
# canvas to 16:9 whose sides are multiples of H3's required 32 px, and both
# machine paths already run it. A 1536x864 plate is cover-cropped to it
# (about 3.6% of the width), identically for the first and the last frame.
CAPABILITY_ID = "video.minimax_h3.text_to_video.v1"
WIDTH = 768
HEIGHT = 448
FPS = 24
STEPS = 20
# H3's trained clip range is 124-362 frames on its 17k+5 grid at 24 fps:
# 5 s -> 124 frames (about 5.2 s), 15 s -> 362 frames.
DEFAULT_SECONDS = 5.0
MIN_SECONDS = 5.0
MAX_SECONDS = 15.0
LAST_FRAME_SLOT = "last_frame"

_DIFF_SIZE = (96, 56)


def template_pins_last_frame(template: Any) -> bool:
    return any(slot.name == LAST_FRAME_SLOT for slot in template.image_slots)


def registry_supports_scene_flights(registry: Any) -> bool:
    """Whether an executor's template for the flight capability binds a last
    frame. Without the slot, the plugin rejects the job as an unbound input."""
    template = registry.template(CAPABILITY_ID)
    return template is not None and template_pins_last_frame(template)


def pinned_pool_supports_scene_flights() -> bool:
    """Hosted workers run the pinned outbound-gpu-worker-pool templates. Its
    standard H3 template binds only ``first_frame`` today, so hosted flights
    stay off until a pin whose template declares ``last_frame``."""
    from outbound_gpu_worker_pool.comfy import TemplateRegistry

    directory = Path(str(files("outbound_gpu_worker_pool") / "templates"))
    return registry_supports_scene_flights(TemplateRegistry.from_directory(directory))


def _grey(content: bytes) -> list[int]:
    with Image.open(BytesIO(content)) as image:
        return list(image.convert("L").resize(_DIFF_SIZE, Image.Resampling.BILINEAR).getdata())


def frame_diff(a: bytes, b: bytes) -> float:
    """Mean absolute greyscale difference, 0 (identical) to 1."""
    left, right = _grey(a), _grey(b)
    total = sum(abs(x - y) for x, y in zip(left, right, strict=True))
    return round(total / (255.0 * len(left)), 5)


def _first_frame(content: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="flight-frame-") as tmp:
        source = Path(tmp) / "source.mp4"
        out = Path(tmp) / "first.png"
        source.write_bytes(content)
        _run([*VIDEO_INPUT, "-i", str(source), "-an", "-frames:v", "1", str(out)])
        return out.read_bytes()


async def seam_diffs(
    assets: Any, video_key: str, last_frame_key: str, opening_key: str, closing_key: str
) -> tuple[float, float]:
    """How closely the clip starts on the origin and ends on the destination."""
    video = await assets.read_limited(video_key, MAX_VIDEO_BYTES)
    first = await asyncio.to_thread(_first_frame, video)
    last = await assets.read_limited(last_frame_key, 20 * 1024 * 1024)
    opening = await assets.read_limited(opening_key, 20 * 1024 * 1024)
    closing = await assets.read_limited(closing_key, 20 * 1024 * 1024)
    return (
        await asyncio.to_thread(frame_diff, first, opening),
        await asyncio.to_thread(frame_diff, last, closing),
    )
