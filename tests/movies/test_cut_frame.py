"""An assembled cut fills its 768x432 frame: takes are 768x448, so they are
cropped to the frame, never fitted inside it with black bands."""

import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from magic_video_editor.cpu_worker import _assemble


def _white_take(path, width=768, height=448, seconds=1):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}:r=24:d={seconds}",
         "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_a_768x448_take_fills_the_frame_with_no_black_bands(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    clip = SimpleNamespace(in_seconds=0, out_seconds=1, transition_seconds=0, mute=True)
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(_assemble([(_white_take(tmp_path / "take.mp4"), clip)]))
    frame = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(cut), "-frames:v", "1", str(frame)], check=True)
    image = Image.open(frame).convert("L")
    assert image.size == (768, 432)
    for x in (0, 4, 763, 767):
        column = [image.getpixel((x, y)) for y in range(0, 432, 24)]
        assert min(column) > 200, f"column {x} has a dark band: {min(column)}"


def _frame(tmp_path, content):
    clip = SimpleNamespace(in_seconds=0, out_seconds=1, transition_seconds=0, mute=True)
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(_assemble([(content, clip)]))
    frame = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(cut), "-frames:v", "1", str(frame)], check=True)
    return Image.open(frame).convert("L")


def test_an_imported_portrait_clip_keeps_its_whole_picture(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    image = _frame(tmp_path, _white_take(tmp_path / "portrait.mp4", width=432, height=768))
    assert image.size == (768, 432)
    # Fitted, not cropped: the full height survives in the middle and the sides are padded.
    assert min(image.getpixel((384, y)) for y in range(0, 432, 12)) > 200
    assert max(image.getpixel((x, 216)) for x in (0, 40, 727, 767)) < 40


def test_an_extremely_narrow_source_is_framed_without_a_huge_intermediate_frame(tmp_path):
    """Cover-scaling a 4x4000 sliver to 768 wide makes a 768x768000 intermediate (gigabytes);
    the frame filter must bound both dimensions. Measured as the peak memory of one FFmpeg run
    in a fresh process, so other tests' children cannot mask it."""
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    sliver = tmp_path / "sliver.mp4"
    _white_take(sliver, width=4, height=4000)
    probe = (
        "import resource, subprocess, sys\n"
        "from magic_video_editor.cpu_worker import FRAME_FILTER\n"
        "subprocess.run(['ffmpeg', '-v', 'error', '-i', sys.argv[1], '-vf', FRAME_FILTER, '-f', 'null', '-'], check=True)\n"
        "peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss\n"
        "print(peak if sys.platform == 'darwin' else peak * 1024)\n"
    )
    peak = int(subprocess.run([sys.executable, "-c", probe, str(sliver)], capture_output=True, text=True, check=True).stdout)
    assert peak < 400 * 1024 * 1024, f"FFmpeg peaked at {peak / 2**20:.0f} MiB"
    assert _frame(tmp_path, sliver.read_bytes()).size == (768, 432)
