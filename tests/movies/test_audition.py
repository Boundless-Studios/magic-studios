"""Casting a voice from its description: a solo H3 take whose audio becomes the
character's reference."""

import shutil
import struct
import subprocess
import wave
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.service import MovieError, MovieService
from magic_video_editor.movies.store import MemoryMovieStore


class Pool:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    async def submit(self, request):
        self.calls.append(request)
        self.jobs.setdefault(
            str(request.job_id),
            SimpleNamespace(status="queued", output_key=request.output_key),
        )

    async def get(self, job_id):
        return self.jobs.get(str(job_id))

    async def cancel(self, job_id):
        return True


class Media(MovieMedia):
    """Skips ffmpeg: the 'video' is a WAV and extraction returns it unchanged."""

    async def extract_audio(self, content):
        return content

    async def audio_duration(self, content):
        with wave.open(BytesIO(content)) as w:
            return w.getnframes() / w.getframerate()


def _wav(seconds):
    out = BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * int(8000 * seconds))
    return out.getvalue()


async def setup(portrait=False):
    pool = Pool()
    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, pool, media=Media(assets))
    movie = await service.create("owner", "Film")
    movie = await service.characters(
        movie.movie_id, "owner", movie.revision,
        [{"character_id": "cap", "name": "Captain Thorne", "appearance": "grizzled veteran, greying hair",
          "voice": "gravelly, weathered baritone", "possessive": "his"}],
    )
    if portrait:
        data = BytesIO()
        Image.new("RGB", (1024, 1024), "blue").save(data, "PNG")
        await assets.write_once("outputs/portrait.png", data.getvalue(), "image/png")
        movie.characters[0].portrait_asset_key = "outputs/portrait.png"
        movie = await service.save(movie, movie.revision)
    return service, pool, movie


@pytest.mark.asyncio
async def test_an_audition_take_becomes_the_voice_reference():
    service, pool, movie = await setup(portrait=True)
    movie = await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud1")
    submission = pool.calls[-1]
    assert submission.capability_id == "video.minimax_h3.text_to_video.v1"
    # H3's native clock, sized on its 17k+5 grid like a motion take (#203).
    assert submission.payload["fps"] == 24 and submission.payload["length"] == 226
    assert submission.payload["length"] % 17 == 5
    assert "in a gravelly, weathered baritone, says: <d>[English] Here I am." in submission.payload["prompt"]
    assert "<Subject 1> is Captain Thorne, grizzled veteran, greying hair." in submission.payload["prompt"]
    frame = submission.payload["images"]["first_frame"]
    assert submission.input_keys == (frame,)
    assert Image.open(BytesIO(await service.assets.read(frame))).size == (768, 448)
    await service.assets.write_once(submission.output_key, _wav(5.0), "video/mp4")
    pool.jobs[str(submission.job_id)].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    cap = movie.characters[0]
    assert movie.compositions["aud1"]["status"] == "succeeded"
    assert cap.voice_asset_key.startswith(f"outputs/movies/{movie.movie_id}/voices/")
    assert cap.voice_seconds == pytest.approx(5.0, abs=0.01)


@pytest.mark.asyncio
async def test_an_audition_needs_a_voice_description_and_rejects_a_too_short_take():
    service, pool, movie = await setup()
    cast = [c.model_dump(include={"character_id", "name", "appearance", "possessive"}) for c in movie.characters]
    movie = await service.characters(movie.movie_id, "owner", movie.revision, cast)
    with pytest.raises(MovieError, match="voice first"):
        await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud0")
    cast[0]["voice"] = "soft tenor"
    movie = await service.characters(movie.movie_id, "owner", movie.revision, cast)
    movie = await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud1")
    submission = pool.calls[-1]
    assert "images" in submission.payload and submission.payload["images"] == {}
    await service.assets.write_once(submission.output_key, _wav(1.0), "video/mp4")
    pool.jobs[str(submission.job_id)].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.compositions["aud1"]["status"] == "failed"
    assert "audition again" in movie.compositions["aud1"]["error"]
    assert movie.characters[0].voice_asset_key is None


def test_extract_audio_trims_leading_and_trailing_silence(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    from magic_video_editor.cpu_worker import _audio_duration, _extract_audio

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=256x144:r=16:d=6",
         "-f", "lavfi", "-i", "sine=frequency=300:r=48000:d=6",
         "-af", "volume='if(between(t,1.5,4.5),1,0)':eval=frame",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)],
        check=True,
    )
    wav = _extract_audio(clip.read_bytes())
    assert wav[:4] == b"RIFF"
    assert 2.6 < _audio_duration(wav) < 3.4


@pytest.mark.asyncio
async def test_an_audition_submitted_before_the_native_clock_resubmits_unchanged():
    """A pending audition persisted without a recipe keeps its original 16 fps,
    145-frame request, so an idempotent resubmission cannot conflict."""
    service, pool, movie = await setup()
    movie = await service.audition(movie.movie_id, "owner", movie.revision, "cap", "aud1")
    intent = movie.compositions["aud1"]
    assert (intent["fps"], intent["length"]) == (24, 226)
    del intent["fps"], intent["length"]
    movie = await service.save(movie, movie.revision)

    await service.reconcile(movie.movie_id, "owner")

    resubmitted = pool.calls[-1]
    assert resubmitted.job_id == pool.calls[0].job_id
    assert (resubmitted.payload["fps"], resubmitted.payload["length"]) == (16, 145)


def test_the_audition_fits_the_native_template_and_the_voice_limit():
    from magic_video_editor.movies import dialogue

    frames = dialogue.motion_frames(MovieService.AUDITION_SECONDS)
    assert frames <= dialogue.MAX_FRAMES
    assert dialogue.VOICE_MIN_SECONDS <= frames / dialogue.FPS <= dialogue.VOICE_MAX_SECONDS
