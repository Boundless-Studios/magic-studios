"""CPU-decoded previews for local Studio, independent of browser video seeking."""
import asyncio
import base64
import json
import subprocess
from fractions import Fraction
from io import BytesIO

from PIL import Image

from magic_video_editor.plugins.ffmpeg import extract_video_frames


async def build_frame_previews(source: bytes, count: int) -> dict:
    frames = await extract_video_frames(source, count)

    def encode():
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
             '-show_entries', 'stream=avg_frame_rate,nb_read_frames', '-of', 'json', 'pipe:0'],
            input=source, capture_output=True, check=True, timeout=60,
        )
        stream = json.loads(probe.stdout)['streams'][0]
        duration = int(stream['nb_read_frames']) / float(Fraction(stream['avg_frame_rate']))
        images, poses = [], []
        for frame in frames:
            with Image.open(BytesIO(frame)) as opened:
                image = opened.convert('RGBA')
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format='PNG')
                images.append('data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
                pose = []
                for y in range(16):
                    for x in range(16):
                        r, g, b, a = image.getpixel((int((x + .5) * image.width / 16), int((y + .5) * image.height / 16)))
                        pose.extend(c / 255 * a / 255 * (2 if y >= 8 else 1) for c in (r, g, b))
                poses.append(pose)
        return {'frames': images, 'poses': poses, 'duration': duration}

    return await asyncio.to_thread(encode)


async def first_frame_poster(source: bytes) -> bytes:
    """A real first-frame image for cards whose browser decoder is still starting."""
    import tempfile
    from pathlib import Path
    from magic_video_editor.plugins.ffmpeg import alpha_video_decoder_args

    def render():
        with tempfile.TemporaryDirectory(prefix='magic-poster-') as directory:
            path = Path(directory) / 'source'
            path.write_bytes(source)
            return subprocess.run(
                ['ffmpeg', '-v', 'error', *alpha_video_decoder_args(source), '-i', str(path),
                 '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=384:384:force_original_aspect_ratio=decrease',
                 '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'],
                capture_output=True, check=True, timeout=30,
            ).stdout
    return await asyncio.to_thread(render)
