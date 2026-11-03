"""Exercise the shipping FFmpeg, not the host runner's different version."""

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from magic_video_editor.cpu_worker import _assemble

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    sources = []
    for index, color in enumerate(("red", "blue", "green")):
        source = root / f"{index}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=120x80:r=16:d=4.5625",
                "-c:v",
                "libx264",
                str(source),
            ],
            check=True,
        )
        sources.append(
            (
                source.read_bytes(),
                SimpleNamespace(
                    in_seconds=0,
                    out_seconds=4.5625,
                    transition_seconds=0.5,
                ),
            )
        )
    content = _assemble(sources)
    output = root / "movie.mp4"
    output.write_bytes(content)
    # Each 4.5625 s source at 16 fps normalizes to 110 frames at 24 fps; two
    # joins overlap 12 frames each: 306 frames of picture. Each clip's sound is
    # fitted to its picture, so the movie's sound matches its picture within a
    # frame — never short, never longer by AAC tail padding.
    streams = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,duration",
                "-of",
                "json",
                str(output),
            ]
        )
    )["streams"]
    lengths = {s["codec_type"]: float(s["duration"]) for s in streams}
    duration = lengths["video"]
    assert abs(duration - 306 / 24) < 0.05, lengths
    assert abs(lengths["audio"] - lengths["video"]) <= 1 / 24, lengths
    pixel = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(104 / 24),
            "-i",
            str(output),
            "-frames:v",
            "1",
            "-vf",
            "crop=2:2:384:216",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    red, green, blue = pixel[:3]
    assert 80 < red < 180 and green < 15 and 80 < blue < 180, list(pixel[:3])
    print(
        f"Shipping FFmpeg: three-shot dissolve movie {duration:.3f}s; blended pixel {list(pixel[:3])}"
    )
