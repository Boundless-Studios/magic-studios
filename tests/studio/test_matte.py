import numpy as np
from PIL import Image

from magic_video_editor.studio.matte import clean_green_edges


def test_chroma_cleanup_removes_neon_fringe_but_preserves_opaque_green_interior():
    pixels = np.zeros((32, 32, 4), dtype=np.uint8)
    pixels[4:28, 4:28] = [100, 145, 45, 255]  # real green skin
    pixels[4:28, 3] = [0, 177, 64, 220]       # screen spill on boundary
    pixels[12:20, 12:20] = [0, 177, 64, 255] # green interior must not be globally removed
    output = np.asarray(clean_green_edges(Image.fromarray(pixels)))
    assert not output[4:28, 3, 3].any()
    assert np.array_equal(output[8:24, 8:24], pixels[8:24, 8:24])


def test_partial_edge_despill_preserves_alpha_and_non_green_channels():
    pixels = np.zeros((9, 9, 4), dtype=np.uint8)
    pixels[4, 4] = [110, 180, 90, 150]
    output = np.asarray(clean_green_edges(Image.fromarray(pixels)))
    assert list(output[4, 4]) == [110, 120, 90, 150]


def test_opaque_green_art_is_unchanged_without_transparent_edge():
    image = Image.new('RGBA', (16, 16), (0, 177, 64, 255))
    assert clean_green_edges(image).tobytes() == image.tobytes()


def test_normalized_cleanup_is_explicit_and_records_matte_version():
    import io
    from magic_video_editor.studio.models import NormalizationSpec
    from magic_video_editor.studio.normalization import pack_normalized_sheet
    from magic_video_editor.studio.matte import MATTE_VERSION
    image = Image.new('RGBA', (32, 32))
    image.paste((110, 150, 55, 255), (8, 8, 24, 24))
    image.putpixel((7, 12), (0, 177, 64, 220))
    source = io.BytesIO(); image.save(source, format='PNG')
    arguments = dict(scale=1, source_anchor_x=16, source_anchor_y=24,
                     target_anchor_x=16, target_anchor_y=24, padding=0)
    unchanged = Image.open(io.BytesIO(pack_normalized_sheet([source.getvalue()], 32, NormalizationSpec(**arguments))))
    cleaned = Image.open(io.BytesIO(pack_normalized_sheet([source.getvalue()], 32, NormalizationSpec(**arguments, cleanup_green_edges=True))))
    assert unchanged.getpixel((7, 12))[3] == 220
    assert cleaned.getpixel((7, 12))[3] == 0
    assert cleaned.getpixel((16, 16)) == unchanged.getpixel((16, 16))
    assert cleaned.info['studio_matte_version'] == MATTE_VERSION


def test_cleanup_discards_near_transparent_compression_speckles():
    image = Image.new('RGBA', (32, 32))
    image.putpixel((0, 0), (0, 80, 30, 7))
    image.putpixel((16, 16), (110, 145, 50, 255))
    output = clean_green_edges(image)
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((16, 16)) == image.getpixel((16, 16))
