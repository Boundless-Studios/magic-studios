"""Calibrated animation framing without changing scale between poses."""

import io
import math

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from magic_video_editor.studio.models import NormalizationSpec


def pack_normalized_sheet(
    frames: list[bytes], cell_size: int, specification: NormalizationSpec
) -> bytes:
    """Apply one affine transform, expanding every cell equally to retain alpha.

    Anchors are measured once on comparable resting poses. They are not inferred
    from each frame's silhouette (which would resize the body as weapons move).
    The requested cell grows symmetrically and its shifted pivot is embedded.
    """
    if not frames:
        raise ValueError("cannot normalize an empty animation")
    if not 1 <= cell_size <= 4096:
        raise ValueError("normalization cell size must be between 1 and 4096")
    if max(specification.target_anchor_x, specification.target_anchor_y) > cell_size:
        raise ValueError("target anchor must lie inside the requested cell")
    images: list[Image.Image] = []
    scale = specification.scale
    tx = specification.target_anchor_x - scale * specification.source_anchor_x
    ty = specification.target_anchor_y - scale * specification.source_anchor_y
    overflow = 0.0
    geometry: tuple[int, int] | None = None
    for content in frames:
        with Image.open(io.BytesIO(content)) as opened:
            if 'A' not in opened.getbands() and 'transparency' not in opened.info:
                raise ValueError("normalization requires transparent frames; derive alpha first")
            frame = opened.convert("RGBA")
        if geometry is not None and frame.size != geometry:
            raise ValueError("normalization requires consistent source geometry")
        geometry = frame.size
        if specification.source_anchor_x > frame.width or specification.source_anchor_y > frame.height:
            raise ValueError("source anchor must lie inside each source frame")
        if specification.cleanup_green_edges:
            from magic_video_editor.studio.matte import clean_green_edges

            frame = clean_green_edges(frame)
        images.append(frame)
        bounds = frame.getchannel("A").getbbox()
        if bounds is not None:
            left, top, right, bottom = bounds
            # Include resampling support as well as the requested visual margin.
            pad = specification.padding + math.ceil(2 * scale)
            overflow = max(overflow, pad - (left * scale + tx),
                           pad - (top * scale + ty),
                           right * scale + tx + pad - cell_size,
                           bottom * scale + ty + pad - cell_size)
    expansion = max(0, math.ceil(overflow))
    output_size = cell_size + 2 * expansion
    if output_size > 4096 or output_size * output_size * len(images) > 64_000_000:
        raise ValueError("normalized animation exceeds pixel budget; reduce scale or frame count")
    tx += expansion
    ty += expansion
    packed = Image.new("RGBA", (output_size * len(images), output_size))
    for index, frame in enumerate(images):
        transformed = frame.transform(
            (output_size, output_size), Image.Transform.AFFINE,
            (1 / scale, 0, -tx / scale, 0, 1 / scale, -ty / scale),
            resample=Image.Resampling.BICUBIC,
        )
        if specification.cleanup_green_edges:
            # Resampling opaque green against transparency can create new spill.
            transformed = clean_green_edges(transformed)
        # Copy RGBA directly: using the image as a mask squares its alpha.
        packed.paste(transformed, (index * output_size, 0))
    info = PngInfo()
    info.add_text("studio_frame_count", str(len(images)))
    info.add_text("studio_pivot_x", str(round(specification.target_anchor_x + expansion)))
    info.add_text("studio_pivot_y", str(round(specification.target_anchor_y + expansion)))
    info.add_text("studio_normalization", specification.model_dump_json())
    if specification.cleanup_green_edges:
        from magic_video_editor.studio.matte import MATTE_VERSION

        info.add_text("studio_matte_version", MATTE_VERSION)
    buffer = io.BytesIO()
    packed.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()
