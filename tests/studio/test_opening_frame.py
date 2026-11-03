import io

from PIL import Image

from magic_video_editor.studio.opening_frame import render_subject_opening_frame

_BACKDROP = (0, 177, 64)


def _subject_png(*, all_transparent: bool = False) -> bytes:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    if not all_transparent:
        # A 20x40 opaque red rectangle, off-center in the source canvas.
        for x in range(10, 30):
            for y in range(5, 45):
                image.putpixel((x, y), (255, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_renders_the_subject_centered_and_scaled_onto_the_backdrop() -> None:
    rendered = render_subject_opening_frame(
        _subject_png(), width=128, height=96, backdrop_rgb=_BACKDROP
    )

    output = Image.open(io.BytesIO(rendered))
    assert output.mode == "RGB"
    assert output.size == (128, 96)

    assert output.getpixel((0, 0)) == _BACKDROP
    assert output.getpixel((127, 0)) == _BACKDROP
    assert output.getpixel((0, 95)) == _BACKDROP
    assert output.getpixel((127, 95)) == _BACKDROP

    red_pixels = [
        (x, y)
        for x in range(128)
        for y in range(96)
        if output.getpixel((x, y)) == (255, 0, 0)
    ]
    assert red_pixels, "expected the rendered subject to be visible"
    xs = [x for x, _ in red_pixels]
    ys = [y for _, y in red_pixels]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    bbox_center_x = (min_x + max_x + 1) / 2
    bbox_center_y = (min_y + max_y + 1) / 2
    assert abs(bbox_center_x - 64) <= 1
    assert abs(bbox_center_y - 48) <= 1

    bbox_height = max_y - min_y + 1
    assert 78 <= bbox_height <= 84


def test_an_all_transparent_subject_yields_a_plain_backdrop() -> None:
    rendered = render_subject_opening_frame(
        _subject_png(all_transparent=True), width=64, height=64, backdrop_rgb=_BACKDROP
    )

    output = Image.open(io.BytesIO(rendered))
    assert output.mode == "RGB"
    assert output.size == (64, 64)
    for x in (0, 32, 63):
        for y in (0, 32, 63):
            assert output.getpixel((x, y)) == _BACKDROP


def test_attack_framing_leaves_extra_headroom_without_changing_reference_proportions():
    def bounds(fill):
        output = Image.open(io.BytesIO(render_subject_opening_frame(_subject_png(), width=96, height=96, backdrop_rgb=_BACKDROP, fit_fraction=fill)))
        from PIL import ImageChops
        return ImageChops.difference(output, Image.new("RGB", output.size, _BACKDROP)).getbbox()
    walk = bounds(.86)
    attack = bounds(.58)
    assert attack[1] > walk[1]
    assert attack[3] < walk[3]
    assert abs((attack[2] - attack[0]) / (attack[3] - attack[1]) - .5) < .02
