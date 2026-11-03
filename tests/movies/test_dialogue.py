"""Dialogue shots: cast voices, line timing on H3's grid, and the take they submit."""

import struct
import wave
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies import dialogue
from magic_video_editor.movies.models import Character, DialogueLine
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


class Media:
    def __init__(self, assets):
        self.assets = assets

    async def duration_for_key(self, key):
        return 7.0

    async def final_frame(self, key):
        output = BytesIO()
        Image.new("RGB", (768, 448), (3, 4, 5)).save(output, "PNG")
        target = key + ".png"
        await self.assets.write_once(target, output.getvalue(), "image/png")
        return target

MARA = Character(
    character_id="mara",
    name="Mara",
    appearance="a weathered pilot with cropped grey hair",
    voice="low, unhurried alto with a soft northern accent",
    possessive="her",
)
IVO = Character(character_id="ivo", name="Ivo", possessive="his")
LINES = [
    DialogueLine(character_id="mara", text="An impact, long before us, broke the crust open."),
    DialogueLine(character_id="ivo", text="Then we go down.", delivery="quietly"),
]


class DialogueMedia(Media):
    async def audio_duration(self, content):
        with wave.open(BytesIO(content)) as w:
            return w.getnframes() / w.getframerate()


def _wav(seconds: float) -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * int(8000 * seconds))
    return out.getvalue()


# --- timing -----------------------------------------------------------------


def test_lines_are_timed_on_the_17k_plus_5_grid_with_a_quiet_tail():
    take = dialogue.plan(LINES)
    assert take.frames % 17 == 5
    assert take.frames >= dialogue.MIN_FRAMES
    assert take.seconds == take.frames / 24
    # Calibrated: 12 syllables and two commas take about 4.35 s.
    first = take.beats[0]
    assert first.start == pytest.approx(0.8)
    assert first.end - first.start == pytest.approx(4.35, abs=0.05)
    # A speaker change waits a turn gap; the take holds a tail after the last word.
    assert take.beats[1].start == pytest.approx(first.end + 0.8)
    assert take.seconds >= take.beats[1].end + 1.5


def test_a_continuation_absorbs_the_pinned_guide_frames_in_its_lead_in():
    plain = dialogue.plan(LINES)
    guided = dialogue.plan(LINES, continues=True)
    assert guided.beats[0].start == pytest.approx(
        0.8 + dialogue.GUIDE_FRAMES / 24, abs=1e-3
    )
    assert guided.frames >= plain.frames


def test_too_much_dialogue_for_one_take_is_refused_not_shortened():
    long_line = DialogueLine(character_id="mara", text="word " * 60)
    with pytest.raises(ValueError, match="split it"):
        dialogue.plan([long_line])


# --- prompt -----------------------------------------------------------------


def test_prompt_binds_each_speaker_to_their_own_voice_and_both_plates():
    take = dialogue.plan(LINES)
    prompt = dialogue.build_prompt(
        LINES,
        [MARA, IVO],
        take,
        style="grainy 16 mm",
        setting="a cramped cockpit",
        camera="Static two-shot.",
        soundscape="engine hum",
        continues=False,
    )
    assert "<Subject 1> is Mara, a weathered pilot with cropped grey hair in <Picture 1>." in prompt
    assert "<Audio 1> is the voice-timbre reference for <Subject 1> (S1)" in prompt
    assert "<Audio 2> is the voice-timbre reference for <Subject 2> (S2)" in prompt
    assert "<Picture 2> is the exact frame the target video ends on." in prompt
    assert "<d>[English] An impact, long before us, broke the crust open.</d>" in prompt
    assert "<Subject 2> (S2), quietly, in the voice referenced from <Audio 2>, says:" in prompt
    assert "<Subject 2>'s lips close on his final word, the jaw still." in prompt
    assert f"{take.seconds:.1f}-second video" in prompt
    assert "overall_soundscape:\nengine hum" in prompt
    assert "does not speak" not in prompt


def test_speakers_keep_cast_order_and_are_capped_at_two():
    cast = [IVO, MARA]
    assert [c.character_id for c in dialogue.speakers_for(LINES, cast)] == ["ivo", "mara"]
    with pytest.raises(ValueError, match="cast"):
        dialogue.speakers_for(LINES, [MARA])
    third = DialogueLine(character_id="zed", text="Hi.")
    zed = Character(character_id="zed", name="Zed")
    with pytest.raises(ValueError, match="at most 2"):
        dialogue.speakers_for([*LINES, third], [MARA, IVO, zed])


# --- service ----------------------------------------------------------------


async def setup():
    pool = Pool()
    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, pool)
    service.media = DialogueMedia(assets)
    movie = await service.create("owner", "Film")
    voices = {}
    for cid, seconds in (("mara", 4.0), ("ivo", 3.0)):
        grant = await service.upload(movie.movie_id, "owner", "audio/wav")
        await assets.write_once(grant["asset_key"], _wav(seconds), "audio/wav")
        voices[cid] = grant["asset_key"]
    movie = await service.get(movie.movie_id, "owner")
    movie = await service.characters(
        movie.movie_id,
        "owner",
        movie.revision,
        [
            {**MARA.model_dump(exclude={"voice_seconds"}), "voice_asset_key": voices["mara"]},
            {**IVO.model_dump(exclude={"voice_seconds"}), "voice_asset_key": voices["ivo"]},
        ],
    )
    return service, pool, movie


async def story(service, movie, shots):
    return await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief="Story",
        style="grainy 16 mm",
        shots=shots,
        batch_size=3,
    )


async def plate(service, movie, sid):
    b = BytesIO()
    Image.new("RGB", (1024, 512), (10, 20, 30)).save(b, "PNG")
    grant = await service.upload(movie.movie_id, "owner", "image/png")
    await service.assets.write_once(grant["asset_key"], b.getvalue(), "image/png")
    movie = await service.get(movie.movie_id, "owner")
    return await service.opening(
        movie.movie_id, "owner", movie.revision, sid, grant["asset_key"]
    )


def dialogue_shot(sid, lines, continuation_of=None):
    return {
        "shot_id": sid,
        "name": sid,
        "prompt": "Static two-shot.",
        "opening_prompt": "a cramped cockpit",
        "soundscape": "engine hum",
        "duration_seconds": 2,
        "continuation_of": continuation_of,
        "dialogue": [line.model_dump() for line in lines],
    }


@pytest.mark.asyncio
async def test_voice_references_are_frozen_with_their_measured_length():
    service, _, movie = await setup()
    mara = movie.characters[0]
    assert mara.voice_asset_key.startswith(f"outputs/movies/{movie.movie_id}/voices/")
    assert mara.voice_asset_key.endswith(".wav")
    assert mara.voice_seconds == pytest.approx(4.0, abs=0.05)
    # Re-saving the cast keeps a frozen key without re-reading the upload.
    again = await service.characters(
        movie.movie_id,
        "owner",
        movie.revision,
        [c.model_dump(exclude={"voice_seconds"}) for c in movie.characters],
    )
    assert again.characters == movie.characters


@pytest.mark.asyncio
async def test_a_voice_outside_h3_limits_is_refused():
    service, _, movie = await setup()
    grant = await service.upload(movie.movie_id, "owner", "audio/wav")
    await service.assets.write_once(grant["asset_key"], _wav(1.0), "audio/wav")
    movie = await service.get(movie.movie_id, "owner")
    with pytest.raises(MovieError, match="2-15 seconds"):
        await service.characters(
            movie.movie_id,
            "owner",
            movie.revision,
            [{"character_id": "x", "name": "X", "voice_asset_key": grant["asset_key"]}],
        )


@pytest.mark.asyncio
async def test_lines_must_name_cast_members_and_fit_one_take():
    service, _, movie = await setup()
    with pytest.raises(MovieError, match="cast"):
        await story(
            service,
            movie,
            [dialogue_shot("a", [DialogueLine(character_id="nobody", text="Hi.")])],
        )
    with pytest.raises(MovieError, match="split it"):
        await story(
            service,
            movie,
            [dialogue_shot("a", [DialogueLine(character_id="mara", text="word " * 60)])],
        )


@pytest.mark.asyncio
async def test_a_dialogue_take_submits_to_the_dialogue_capability_with_voices_and_plates():
    service, pool, movie = await setup()
    movie = await story(
        service,
        movie,
        [dialogue_shot("a", LINES), dialogue_shot("b", LINES[1:], continuation_of="a")],
    )
    with pytest.raises(MovieError, match="plate image"):
        await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r0")
    movie = await plate(service, movie, "a")
    movie = await plate(service, movie, "b")
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a", "b"], "r1"
    )
    a = movie.shots[0].takes[0]
    assert a.mode == "dialogue"
    assert a.length_frames % 17 == 5
    assert a.duration_seconds == a.length_frames / 24
    assert a.status == "running"
    submission = pool.calls[-1]
    assert submission.capability_id == "video.minimax_h3.dialogue.v1"
    payload = submission.payload
    assert payload["fps"] == 24
    assert payload["steps"] == 40
    assert payload["length"] == a.length_frames
    assert payload["width"] == 768 and payload["height"] == 448
    assert payload["audios"] == {
        "voice_a": movie.characters[0].voice_asset_key,
        "voice_b": movie.characters[1].voice_asset_key,
    }
    assert set(payload["images"]) == {"opening_frame", "end_plate"}
    assert set(submission.input_keys) == {
        *payload["images"].values(),
        *payload["audios"].values(),
    }
    for key in payload["images"].values():
        frame = await service.assets.read_limited(key, 20 * 1024 * 1024)
        assert Image.open(BytesIO(frame)).size == (768, 448)
    assert "<Audio 1> is the voice-timbre reference for <Subject 1>" in payload["prompt"]
    assert "<d>[English] Then we go down.</d>" in payload["prompt"]
    # The continuation waits for a selected predecessor; once one lands it
    # opens on that take's final frame and still lands on its own plate.
    b = movie.shots[1].takes[0]
    assert b.status == "waiting"
    pool.jobs[a.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.select(movie.movie_id, "owner", movie.revision, "a", a.take_id)
    movie = await service.reconcile(movie.movie_id, "owner")
    b = movie.shots[1].takes[0]
    assert b.status == "running"
    follow = pool.calls[-1].payload
    assert follow["images"]["opening_frame"] == b.provider_frame_key
    assert b.opening_asset_key == a.asset_key + ".png"
    assert follow["images"]["end_plate"] == b.plate_frame_key
    assert follow["audios"] == {"voice_a": movie.characters[1].voice_asset_key}
    assert "final moment of the preceding shot" in follow["prompt"]


@pytest.mark.asyncio
async def test_changing_a_voice_invalidates_the_takes_that_speak_with_it():
    service, _, movie = await setup()
    movie = await story(service, movie, [dialogue_shot("a", LINES[1:])])
    movie = await plate(service, movie, "a")
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r1")
    grant = await service.upload(movie.movie_id, "owner", "audio/wav")
    await service.assets.write_once(grant["asset_key"], _wav(5.0), "audio/wav")
    movie = await service.get(movie.movie_id, "owner")
    cast = [c.model_dump(exclude={"voice_seconds"}) for c in movie.characters]
    cast[1]["voice_asset_key"] = grant["asset_key"]
    movie = await service.characters(movie.movie_id, "owner", movie.revision, cast)
    assert movie.shots[0].takes[0].stale
    with pytest.raises(MovieError, match="Remove a character's lines"):
        await service.characters(movie.movie_id, "owner", movie.revision, cast[:1])


@pytest.mark.asyncio
async def test_the_built_cut_hard_cuts_around_dialogue_and_skips_guide_frames():
    from magic_video_editor.movies.build import advance, selected_cut, start_build

    service, pool, movie = await setup()
    movie = await story(
        service,
        movie,
        [dialogue_shot("a", LINES), dialogue_shot("b", LINES[1:], continuation_of="a")],
    )
    movie = await plate(service, movie, "a")
    movie = await plate(service, movie, "b")
    movie = await start_build(service, movie.movie_id, "owner", movie.revision, "build")
    for _ in range(4):
        for take in [t for s in movie.shots for t in s.takes if t.status == "running"]:
            pool.jobs[take.job_id].status = "completed"
        movie = await service.reconcile(movie.movie_id, "owner")
        movie = await advance(service, movie)
    assert [c.in_seconds for c in movie.cut] == [0, pytest.approx(5 / 24)]
    # A chained dialogue join gets a six-frame dissolve; the last clip has none.
    assert [c.transition_seconds for c in movie.cut] == [pytest.approx(6 / 24), 0]
    # Spoken shots keep their sound; only lineless shots are muted.
    assert [c.mute for c in movie.cut] == [False, False]
    # Review starts an unsaved movie from this same cut.
    assert [c.model_dump() for c in selected_cut(movie)] == [c.model_dump() for c in movie.cut]


@pytest.mark.asyncio
async def test_reviewed_dialogue_continuity_serializes_lines_and_preserves_provenance():
    service, pool, movie = await setup()
    movie = await story(service, movie, [dialogue_shot("a", LINES)])
    movie = await plate(service, movie, "a")
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "review-dialogue")
    take = movie.shots[0].takes[0]
    pool.jobs[take.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    original = movie.shots[0].takes[0].model_dump()
    movie.shots[0].takes[0].stale = True
    movie = await service.save(movie, movie.revision)
    movie = await service.accept_take(movie.movie_id, "owner", movie.revision, "a", take.take_id)
    reviewed = movie.shots[0].takes[0]
    assert not reviewed.stale
    assert reviewed.reviewed_spec_digest
    assert reviewed.prompt == original["prompt"]
    assert reviewed.voice_asset_keys == original["voice_asset_keys"]
    assert reviewed.job_id == original["job_id"]
    assert len(pool.calls) == 1

@pytest.mark.asyncio
async def test_attach_voice_preserves_cast_and_invalidates_speaking_takes():
    service, _, movie = await setup()
    movie = await story(service, movie, [dialogue_shot("a", LINES[1:])])
    movie = await plate(service, movie, "a")
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "r1")
    original = movie.characters[0].model_copy(deep=True)
    grant = await service.upload(movie.movie_id, "owner", "audio/wav")
    await service.assets.write_once(grant["asset_key"], _wav(5.0), "audio/wav")
    updated = await service.attach_voice(
        movie.movie_id, "owner", grant["revision"], "ivo", grant["asset_key"]
    )
    assert updated.characters[0] == original
    assert updated.characters[1].voice_seconds == 5.0
    assert updated.characters[1].voice_asset_key.startswith("outputs/movies/")
    assert updated.shots[0].takes[0].stale
    with pytest.raises(MovieError) as conflict:
        await service.attach_voice(
            movie.movie_id, "owner", grant["revision"], "ivo", grant["asset_key"]
        )
    assert conflict.value.status == 409


@pytest.mark.asyncio
async def test_attach_voice_checks_owner_character_grant_and_duration():
    service, _, movie = await setup()
    for owner, cid, key, status in [
        ("other", "mara", "foreign", 404),
        ("owner", "missing", "foreign", 404),
        ("owner", "mara", "foreign", 403),
    ]:
        with pytest.raises(MovieError) as error:
            await service.attach_voice(movie.movie_id, owner, movie.revision, cid, key)
        assert error.value.status == status
    grant = await service.upload(movie.movie_id, "owner", "audio/wav")
    await service.assets.write_once(grant["asset_key"], _wav(1.0), "audio/wav")
    with pytest.raises(MovieError, match="2-15 seconds"):
        await service.attach_voice(
            movie.movie_id, "owner", grant["revision"], "mara", grant["asset_key"]
        )
