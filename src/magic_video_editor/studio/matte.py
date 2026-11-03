"""Opt-in cleanup of chroma-green spill near an existing transparent matte."""

import numpy as np
from PIL import Image, ImageFilter

MATTE_VERSION = "edge-green-v3"


def clean_green_edges(image: Image.Image) -> Image.Image:
    """Remove key-coloured fringe locally, preserving opaque interior skin.

    This is for chroma-green-derived RGBA images, not arbitrary green artwork.
    It neither guesses a body mask nor desaturates all green pixels. Only a
    three-pixel band next to transparency is eligible for key-colour removal;
    remaining partial-alpha edge spill is reduced without changing opacity.
    """
    rgba = np.array(image.convert("RGBA"), dtype=np.uint8)
    rgb = rgba[:, :, :3].astype(np.int16)
    alpha = rgba[:, :, 3]
    transparent = Image.fromarray(np.where(alpha < 160, 255, 0).astype(np.uint8))
    edge = np.asarray(transparent.filter(ImageFilter.MaxFilter(7))) > 0
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    key_coloured = (r < 90) & (g > 100) & (g - r > 50) & (g - b > 45)
    alpha[edge & key_coloured] = 0
    # Chroma/video compression can leave near-invisible islands far from the body.
    # Remove <=3% opacity speckles so they do not enlarge the registered canvas.
    alpha[alpha <= 8] = 0
    spill = edge & (alpha > 0) & (alpha < 250) & (g > np.maximum(r, b) + 12)
    rgba[:, :, 1][spill] = np.minimum(g[spill], np.maximum(r, b)[spill] + 10).astype(np.uint8)
    rgba[:, :, :3][alpha == 0] = 0
    return Image.fromarray(rgba)
