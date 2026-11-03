"""Render an approved subject as a motion-generation opening frame.

Motion generation optionally binds a `first_frame` image slot on the pool's
video template. Without an approved composition (an opening pose still),
the studio has nothing to bind there, so the video model receives a bare
text prompt and invents its own character. This module flattens the
approved subject onto a flat backdrop at the motion canvas size instead, so
the opening frame is always the user's own subject.
"""

import io
import math

from PIL import Image

# The subject is centered inside a box this fraction of the canvas, both
# axes; motions include jumps and falls, so it is vertically centered too
# rather than pinned to the bottom.
_FIT_FRACTION = 0.86
_MAX_UPSCALE = 2.0
_ALPHA_THRESHOLD = 8


def render_subject_opening_frame(
    subject_png: bytes,
    *,
    width: int,
    height: int,
    backdrop_rgb: tuple[int, int, int],
    fit_fraction: float = _FIT_FRACTION,
) -> bytes:
    """Flatten `subject_png` (an RGBA PNG) onto a `width` x `height` opaque
    `backdrop_rgb` canvas, cropped to its alpha bounding box and scaled to
    fit within `fit_fraction` of the canvas (upscaled up to `_MAX_UPSCALE`x
    when the subject is smaller than that box), centered on both axes.
    Returns an RGB PNG (no alpha) as bytes.
    """
    if not math.isfinite(fit_fraction) or not 0.3 <= fit_fraction <= 0.9:
        raise ValueError("reference fill must be between 0.3 and 0.9")
    subject = Image.open(io.BytesIO(subject_png)).convert("RGBA")
    alpha = subject.split()[-1]
    bbox = alpha.point(lambda value: 255 if value > _ALPHA_THRESHOLD else 0).getbbox()
    cropped = subject.crop(bbox) if bbox is not None else subject

    box_width = width * fit_fraction
    box_height = height * fit_fraction
    scale = min(box_width / cropped.width, box_height / cropped.height)
    scale = min(scale, _MAX_UPSCALE)
    new_width = max(1, round(cropped.width * scale))
    new_height = max(1, round(cropped.height * scale))
    resized = cropped.resize((new_width, new_height), Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), backdrop_rgb)
    offset = ((width - new_width) // 2, (height - new_height) // 2)
    canvas.paste(resized, offset, mask=resized.split()[-1])

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()
