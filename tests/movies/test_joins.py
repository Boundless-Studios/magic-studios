"""The seam gate: every cut of an assembled movie is measured and reported."""

import shutil
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from magic_video_editor.cpu_worker import ASSEMBLY_RENDER, JOIN_LIMIT, _join_diffs
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.models import Clip
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore


def _clip(path, colors, seconds=2):
    """One clip per colour, concatenated with hard cuts, 24 fps."""
    parts = []
    for i, color in enumerate(colors):
        part = path.parent / f"{path.stem}-{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=192x108:r=24:d={seconds}",
             "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(part)],
            check=True,
        )
        parts.append(part)
    listing = path.parent / f"{path.stem}.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(path)], check=True)
    return path.read_bytes()


def test_join_diffs_spike_on_a_hard_cut_and_stay_flat_within_a_clip(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    content = _clip(tmp_path / "cut.mp4", ["gray", "gray", "white"])
    within, same, cut = _join_diffs(content, [1.0, 2.0, 4.0])
    assert within < 0.002 and same < 0.002
    assert cut > 0.4  # grey (128) to white (255) is 0.498


def test_join_times_follow_trims_and_dissolves():
    clips = [
        Clip(shot_id="a", take_id="a", in_seconds=0, out_seconds=4, transition_seconds=0.5),
        Clip(shot_id="b", take_id="b", in_seconds=0.2083, out_seconds=3.2083),
        Clip(shot_id="c", take_id="c", in_seconds=0, out_seconds=2),
    ]
    assert MovieService.join_times(clips) == [3.5, 6.5]


@pytest.mark.asyncio
async def test_assembly_records_every_join_and_flags_the_ones_over_the_limit():
    class Pool:
        def __init__(self):
            self.jobs, self.calls = {}, []

        async def submit(self, request):
            self.calls.append(request)
            self.jobs[str(request.job_id)] = SimpleNamespace(status="queued", output_key=request.output_key)

        async def get(self, job_id):
            return self.jobs.get(str(job_id))

    class Media:
        def __init__(self, assets):
            self.assets, self.measured = assets, []

        async def duration_for_key(self, key):
            return 4.0

        async def final_frame(self, key):
            out = BytesIO()
            Image.new("RGB", (768, 448)).save(out, "PNG")
            await self.assets.write_once(key + ".png", out.getvalue(), "image/png")
            return key + ".png"

        async def assemble(self, sources, key, soundtracks=None):
            await self.assets.write_once(key, b"mp4", "video/mp4")
            return key

        async def join_report(self, key, times):
            self.measured.append((key, times))
            return [0.009, 0.0123]

    pool = Pool()
    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, pool, media=Media(assets))
    movie = await service.create("owner", "Film")
    movie = await service.story(movie.movie_id, "owner", movie.revision, brief="b", style="s", batch_size=3,
                                shots=[{"shot_id": s, "name": s, "prompt": "p", "opening_prompt": "o"} for s in "abc"])
    out = BytesIO()
    Image.new("RGB", (16, 16)).save(out, "PNG")
    for sid in "abc":
        grant = await service.upload(movie.movie_id, "owner", "image/png")
        await assets.write_once(grant["asset_key"], out.getvalue(), "image/png")
        movie = await service.get(movie.movie_id, "owner")
        movie = await service.opening(movie.movie_id, "owner", movie.revision, sid, grant["asset_key"])
    movie = await service.generate(movie.movie_id, "owner", movie.revision, list("abc"), "r1")
    for s in movie.shots:
        pool.jobs[s.takes[0].job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    for s in movie.shots:
        movie = await service.select(movie.movie_id, "owner", movie.revision, s.shot_id, s.takes[0].take_id)
    clips = [{"shot_id": s.shot_id, "take_id": s.takes[0].take_id, "in_seconds": 0, "out_seconds": 4} for s in movie.shots]
    movie = await service.cut(movie.movie_id, "owner", movie.revision, clips)
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "asm1")
    assert movie.assembly.status == "succeeded"
    # The renderer is part of the output's name, so a changed renderer never reuses an old file;
    # the cut digest (what release checks) does not change with it.
    assert movie.assembly.asset_key.endswith(f"/cuts/{movie.assembly.cut_digest}-{ASSEMBLY_RENDER}.mp4")
    assert service.media.measured == [(movie.assembly.asset_key, [4.0, 8.0])]
    assert [(j.index, j.at_seconds, j.diff, j.limit) for j in movie.assembly.joins] == [
        (0, 4.0, 0.009, JOIN_LIMIT),
        (1, 8.0, 0.0123, JOIN_LIMIT),
    ]
    assert [j.diff > j.limit for j in movie.assembly.joins] == [False, True]


@pytest.mark.asyncio
async def test_a_build_accepts_a_shot_that_already_has_an_imported_take():
    from magic_video_editor.movies.build import start_build

    class Media:
        def __init__(self, assets):
            self.assets = assets

        async def duration(self, content):
            return 3.0

        async def final_frame(self, key):
            await self.assets.write_once(key + ".png", b"png", "image/png")
            return key + ".png"

    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, SimpleNamespace(), media=Media(assets))
    movie = await service.create("owner", "Film")
    movie = await service.story(movie.movie_id, "owner", movie.revision, brief="b", style="s", batch_size=1,
                                shots=[{"shot_id": "end", "name": "Ending", "prompt": "p", "opening_prompt": ""}])
    grant = await service.upload(movie.movie_id, "owner", "video/mp4")
    await assets.write_once(grant["asset_key"], b"mp4", "video/mp4")
    movie = await service.get(movie.movie_id, "owner")
    movie = await service.import_clip(movie.movie_id, "owner", movie.revision, "end", grant["asset_key"])
    assert movie.shots[0].takes[0].status == "succeeded" and not movie.shots[0].opening_asset_key
    movie = await start_build(service, movie.movie_id, "owner", movie.revision, "build1")
    assert movie.build is not None and movie.build.status in {"generating", "assembling", "succeeded"}
