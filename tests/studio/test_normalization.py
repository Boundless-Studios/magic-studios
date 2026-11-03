import io

import pytest
from PIL import Image, ImageDraw

from magic_video_editor.studio.exports import pack_cell_sheet
from magic_video_editor.studio.models import NormalizationSpec
from magic_video_editor.studio.quality import inspect_sprite_png


def tile(weapon_x: int, *, alpha: int = 255) -> bytes:
    frame = Image.new('RGBA', (100, 100))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((40, 30, 59, 79), fill=(200, 60, 30, alpha))
    draw.rectangle((weapon_x, 20, weapon_x + 5, 40), fill=(80, 80, 80, alpha))
    out = io.BytesIO()
    frame.save(out, format='PNG')
    return out.getvalue()


def spec(**overrides) -> NormalizationSpec:
    return NormalizationSpec(**(dict(scale=2, source_anchor_x=50, source_anchor_y=80,
        target_anchor_x=50, target_anchor_y=90, padding=8) | overrides))


def test_fixed_body_scale_and_pivot_survive_wide_weapon_motion():
    content = pack_cell_sheet([tile(5), tile(85)], 100, normalization=spec())
    metadata = inspect_sprite_png(content)
    assert metadata.height > 100
    assert metadata.pivot_x is not None and metadata.pivot_y is not None
    sheet = Image.open(io.BytesIO(content))
    size = metadata.height
    bodies = []
    for i in range(2):
        frame = sheet.crop((i * size, 0, (i + 1) * size, size))
        bounds = frame.getchannel('A').getbbox()
        assert bounds is not None
        assert min(bounds[:2]) >= 8
        assert max(bounds[2:]) <= size - 8
        pixels = frame.load()
        bodies.append([(x, y) for y in range(size) for x in range(size)
                       if pixels[x, y][0] > 150 and pixels[x, y][3] > 200])
    assert bodies[0] == bodies[1]
    assert len({x for x, _ in bodies[0]}) == 40


def test_partial_alpha_is_preserved_in_both_packers():
    for normalization in (None, spec(scale=1, source_anchor_x=50, source_anchor_y=80,
                                      target_anchor_x=50, target_anchor_y=80)):
        content = pack_cell_sheet([tile(10, alpha=128)], 100, normalization=normalization)
        image = Image.open(io.BytesIO(content))
        assert image.getpixel((50, 50))[3] == 128


def test_opt_in_cleanup_removes_resampled_spill_without_changing_geometry_or_alpha():
    import numpy as np
    from magic_video_editor.studio.matte import clean_green_edges
    from magic_video_editor.studio.normalization import pack_normalized_sheet

    image = Image.new('RGBA', (32, 32))
    image.paste((108, 160, 108, 255), (8, 8, 24, 24))
    # The prepass preserves this opaque green; only resampling introduces spill.
    assert clean_green_edges(image).tobytes() == image.tobytes()
    source = io.BytesIO()
    image.save(source, format='PNG')
    normalization = spec(scale=.65, source_anchor_x=16, source_anchor_y=24,
                         target_anchor_x=16, target_anchor_y=24, padding=0)
    disabled_bytes = pack_normalized_sheet([source.getvalue()] * 2, 32, normalization)
    disabled = Image.open(io.BytesIO(disabled_bytes))
    enabled = Image.open(io.BytesIO(pack_normalized_sheet(
        [source.getvalue()] * 2, 32,
        normalization.model_copy(update={'cleanup_green_edges': True}),
    )))
    expected_cell = image.transform(
        (32, 32), Image.Transform.AFFINE,
        (1 / .65, 0, -(16 - .65 * 16) / .65,
         0, 1 / .65, -(24 - .65 * 24) / .65),
        resample=Image.Resampling.BICUBIC,
    )
    assert disabled.size == enabled.size == (64, 32)
    assert disabled.crop((0, 0, 32, 32)).tobytes() == expected_cell.tobytes()
    assert disabled_bytes == pack_normalized_sheet([source.getvalue()] * 2, 32, normalization)
    assert 'studio_matte_version' not in disabled.info
    for key in ('studio_pivot_x', 'studio_pivot_y', 'studio_frame_count'):
        assert enabled.info[key] == disabled.info[key]
    expected_alpha = np.asarray(disabled)[:, :, 3].copy()
    expected_alpha[expected_alpha <= 8] = 0  # Existing near-transparent speckle policy.
    assert np.array_equal(np.asarray(enabled)[:, :, 3], expected_alpha)
    assert disabled.getpixel((11, 13)) == (107, 160, 107, 97)
    assert enabled.getpixel((11, 13)) == (107, 117, 107, 97)
    assert enabled.getpixel((16, 18)) == disabled.getpixel((16, 18)) == (108, 160, 108, 255)
    assert enabled.crop((0, 0, 32, 32)).tobytes() == enabled.crop((32, 0, 64, 32)).tobytes()


def test_opaque_input_rejected_before_padding_hides_background():
    out = io.BytesIO()
    Image.new('RGB', (100, 100), 'green').save(out, format='PNG')
    with pytest.raises(ValueError, match='derive alpha'):
        pack_cell_sheet([out.getvalue()], 100, normalization=spec())


def test_excessive_expansion_is_bounded():
    with pytest.raises(ValueError, match='pixel budget'):
        pack_cell_sheet([tile(10)], 4096, normalization=spec(scale=8))


def test_large_cells_are_measured_by_frame_geometry_not_strip_width():
    content = pack_cell_sheet([tile(10)] * 16, 600)
    metadata = inspect_sprite_png(content)
    assert metadata.width == 9600
    assert metadata.height == 600


def test_reference_registration_matches_exact_bytes_and_keeps_original_pixels():
    from magic_video_editor.studio.models import DerivationRecipe, MotionCandidate, MotionState, MotionSpecification
    from magic_video_editor.studio.exports import resolve_registered_pivots, register_sheet_pivot
    reference = MotionCandidate(candidate_id='walk-reference', artifact_key='walk.png', sha256='a'*64)
    normalization = spec(reference_candidate_id=reference.candidate_id, reference_sha256=reference.sha256)
    attack = MotionCandidate(candidate_id='attack', artifact_key='attack.png', sha256='b'*64,
                              recipe=DerivationRecipe(op='normalize', normalization=normalization))
    def motion(candidate):
        return MotionState(specification=MotionSpecification(motion_id=candidate.candidate_id, name=candidate.candidate_id, preset="walk"),
                           candidates=(candidate,), selected_candidate_id=candidate.candidate_id)
    pivots = resolve_registered_pivots((motion(reference), motion(attack)))
    assert pivots['walk-reference'] == (50, 90)
    original = tile(10)
    registered = register_sheet_pivot(original, *pivots['walk-reference'])
    assert Image.open(io.BytesIO(original)).tobytes() == Image.open(io.BytesIO(registered)).tobytes()
    assert Image.open(io.BytesIO(registered)).info['studio_pivot_y'] == '90'
    with pytest.raises(ValueError, match='exact selected'):
        resolve_registered_pivots((motion(reference.model_copy(update={'sha256': 'c'*64})), motion(attack)))
