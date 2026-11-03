"""The in-app director: a described scene becomes a cast and chained shots, and
over-long shots are split at speaker turns and sentence boundaries."""

import json
from itertools import pairwise

import httpx
import pytest

from magic_video_editor.movies.director import (
    OpenRouterDirector,
    configured_director,
    split_overflowing,
    validate_breakdown,
)
from magic_video_editor.movies.models import CharacterInput, ShotInput
from magic_video_editor.movies.planner import PlanError

CAST = [
    {"character_id": "cap", "name": "Captain Thorne", "appearance": "grizzled, greying hair",
     "voice": "gravelly baritone", "possessive": "his", "audition_line": "Strangers are welcome and suspect."},
    {"character_id": "firkey", "name": "Firkey", "appearance": "wiry ranger in green", "voice": "quick tenor",
     "possessive": "their", "audition_line": "What have you been seeing, then?"},
]


def _breakdown(shots):
    return {"style": "misty autumn frontier, torchlight", "characters": CAST, "shots": shots}


def _shot(sid, lines, continuation=None):
    return {"shot_id": sid, "name": sid, "opening_prompt": "the gate at dusk", "prompt": "static two-shot",
            "soundscape": "wind and torches", "continuation_of": continuation, "dialogue": lines}


def _fake(payload):
    def handle(request):
        body = json.loads(request.content)
        assert body["tool_choice"] == {"type": "function", "function": {"name": "breakdown"}}
        assert body["tools"][0]["function"]["parameters"]["required"] == ["style", "characters", "shots"]
        assert request.headers["authorization"] == "Bearer k"
        assert request.url.host == "openrouter.ai"
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"name": "breakdown", "arguments": json.dumps(payload)}}]}}]})
    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_the_director_returns_cast_and_chained_shots():
    payload = _breakdown([
        _shot("gate", [{"character_id": "cap", "text": "What brings two armed folk here?"}]),
        _shot("reply", [{"character_id": "firkey", "text": "Oi, what you been seeing in the woods then?",
                         "delivery": "bluntly"}], "gate"),
    ])
    director = OpenRouterDirector("k", transport=_fake(payload))
    plan = await director.plan(brief="Two strangers meet the captain at the gate.", style="", shot_count=8)
    assert plan.style == "misty autumn frontier, torchlight"
    assert [c.character_id for c in plan.characters] == ["cap", "firkey"]
    assert plan.characters[0].audition_line == "Strangers are welcome and suspect."
    assert [s.shot_id for s in plan.shots] == ["gate", "reply"]
    assert plan.shots[1].continuation_of == "gate"
    assert plan.shots[1].dialogue[0].delivery == "bluntly"


@pytest.mark.asyncio
async def test_a_target_length_reaches_the_director_as_a_word_budget():
    seen = {}

    def handle(request):
        seen["system"] = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"name": "breakdown", "arguments": json.dumps(_breakdown([_shot("a", [])]))}}]}}]})

    director = OpenRouterDirector("k", transport=httpx.MockTransport(handle))
    await director.plan(brief="b", style="", shot_count=8, target_seconds=90)
    assert "about 90 seconds" in seen["system"] and "about 133 words" in seen["system"]


@pytest.mark.asyncio
async def test_an_overlong_plan_gets_one_measured_correction_turn():
    long_line = "We found their camps abandoned, tools scattered, but no signs of struggle. " * 3
    first = _breakdown([_shot(f"s{i}", [{"character_id": "cap", "text": long_line.strip()}]) for i in range(6)])
    second = _breakdown([_shot("s1", [{"character_id": "cap", "text": "What brings you here?"}])])
    turns = []

    def handle(request):
        body = json.loads(request.content)
        turns.append(body["messages"])
        payload = first if len(turns) == 1 else second
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"id": "call_1", "function": {"name": "breakdown", "arguments": json.dumps(payload)}}]}}]})

    director = OpenRouterDirector("k", transport=httpx.MockTransport(handle))
    plan = await director.plan(brief="b", style="", shot_count=12, target_seconds=20)
    assert len(turns) == 2
    correction = turns[1][-1]
    assert correction["role"] == "tool" and correction["tool_call_id"] == "call_1"
    assert "target is 20 seconds" in correction["content"] and "at most 20 words" in correction["content"]
    assert [s.shot_id for s in plan.shots] == ["s1"]
    with pytest.raises(PlanError, match="target length"):
        await director.plan(brief="b", style="", shot_count=8, target_seconds=5)


@pytest.mark.asyncio
async def test_an_unknown_speaker_or_bad_continuity_is_a_plan_error():
    bad = _breakdown([_shot("a", [{"character_id": "nobody", "text": "Hi."}])])
    with pytest.raises(PlanError, match="does not fit"):
        await OpenRouterDirector("k", transport=_fake(bad)).plan(brief="b", style="", shot_count=4)
    loop = _breakdown([_shot("a", [], "a")])
    with pytest.raises(PlanError):
        validate_breakdown(loop, 4)


@pytest.mark.asyncio
async def test_a_failed_api_call_is_a_plan_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(529, json={}))
    with pytest.raises(PlanError, match="unavailable"):
        await OpenRouterDirector("k", transport=transport).plan(brief="b", style="", shot_count=4)


def test_an_overlong_shot_is_split_at_turns_and_sentences_and_chained():
    cast = [CharacterInput.model_validate(c) for c in CAST]
    long_speech = ("He spits into the dirt and continues. Started about a moon ago, strange lights dancing "
                   "between the trees at night. Not fireflies, mind you. These glow with an eerie blue-green "
                   "light, like marsh gas but wrong somehow. They move with purpose, weaving patterns through "
                   "the branches. Old Henrik swears he saw them form shapes.")
    shot = ShotInput.model_validate(_shot("woods", [
        {"character_id": "firkey", "text": "Oi, what you been seeing in the woods then?"},
        {"character_id": "cap", "text": long_speech, "reaction": "Firkey narrows their eyes."},
    ]))
    after = ShotInput.model_validate(_shot("next", [{"character_id": "cap", "text": "One more thing."}], "woods"))
    result = split_overflowing([shot, after], cast)
    ids = [s.shot_id for s in result]
    assert ids[0] == "woods-1" and ids[-1] == "next" and len(ids) > 3
    # Chained in order, plate and directions inherited, the reaction on the last piece.
    for previous, current in pairwise(result):
        assert current.continuation_of == previous.shot_id
        assert current.opening_prompt == shot.opening_prompt
    assert result[-2].dialogue[-1].reaction == "Firkey narrows their eyes."
    assert all(not s.dialogue[0].reaction for s in result[1:-2])
    # Every piece fits one take and never cuts mid-sentence.
    from magic_video_editor.movies import dialogue
    for s in result:
        dialogue.plan(s.dialogue, continues=True)
        for line in s.dialogue:
            assert line.text.rstrip()[-1] in ".?!…"


def test_the_director_is_configured_from_the_environment():
    assert configured_director({}) is None
    director = configured_director({"MAGIC_VIDEO_EDITOR_DIRECTOR_API_KEY": "k", "MAGIC_VIDEO_EDITOR_DIRECTOR_MODEL": "openai/gpt-5"})
    assert director is not None and director.model == "openai/gpt-5"
