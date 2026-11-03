"""The in-app director: a described scene becomes a cast and a shot list.

Given a brief (a synopsis, a screenplay, or a session transcript), the director
returns characters with an appearance and a voice, and shots with their spoken
lines, opening frame, action and soundscape, chained as continuations. It only
drafts: nothing is saved or generated until the person accepts the plan.
Shots that carry more dialogue than one take can hold are split afterwards at
speaker turns and sentence boundaries, never mid-sentence.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from magic_video_editor.movies import dialogue
from magic_video_editor.movies.models import CharacterInput, DialogueLine, ShotInput
from magic_video_editor.movies.planner import PlanError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
# Takes packed beyond this dropped inner sentences; the accepted trailer's takes
# never carried more than about twenty words.
MAX_WORDS_PER_SHOT = 22
_SENTENCE_BREAK = re.compile(r"(?<=[.?!…])\s+(?=[A-Z\"“])")

DIRECTOR_INSTRUCTIONS = """You are the director of a short film generated shot by shot by a
video model that performs speech and lips together from each character's own voice
reference. Turn the brief into a cast and a shot list.

Cast: every character who speaks. `appearance` is a concrete, stable description
(face, hair, skin, build, clothing, distinguishing details) that must hold in every
frame. `voice` is timbre, accent and delivery. `possessive` is their pronoun form.
`audition_line` is one or two plain sentences in character (no names), 10-25 words.

Shots: story order. Each shot is 5-15 seconds of one continuous camera setup. A
conversation in one place is ONE setup and ONE chain: a single framing that holds
every character who speaks in it (a two-shot or group shot, never singles), kept
for the whole conversation, every shot `continuation_of` the one before. Within a
chain the `opening_prompt` is the IDENTICAL text from shot to shot — the same
people in the same places, the same framing — and changes only when an object
enters or leaves the frame (a map comes out, a pendant is handed over). Poses,
gestures, glances and who is speaking are action: they belong in `prompt`, never
in `opening_prompt`. Do not cover a conversation with close-ups, reverse angles
or cutaways. A new setup (`continuation_of` null) happens only when the
place or the time changes, such as an establishing shot before the scene or a
departure after it.
- `opening_prompt`: only what is visible at the shot's first frame (place, light,
  who is where, what they hold). No outcome, no motion.
- `prompt`: the camera and the action during the shot, restrained; no cuts.
- `soundscape`: the room tone under the voices, no music.
- `dialogue`: the exact words spoken, in order, at most two different speakers per
  shot and at most %(max_words)d words per shot in total. `delivery` is how the
  line is said; `pause_before` (seconds, 0-3) is a beat before it; `reaction` is
  what the listener visibly does after it. Use the brief's own words for lines;
  narration that nobody speaks on screen is not dialogue.
- `continuation_of`: the previous shot's id whenever the scene continues without
  a reset; null only when the setting or time changes.
Silent establishing shots are allowed (empty dialogue). Do not exceed
%(max_shots)d shots.%(length)s Return the breakdown by calling the tool.
"""

LENGTH_INSTRUCTION = """ The finished film should run about %d seconds. Every shot
costs about 2.5 seconds of quiet lead-in and tail on top of its speech, and speech
plays at roughly 2 words per second with pauses, so keep about %d words of
dialogue in total across about %d shots: choose the lines that carry the scene and
keep them word for word; drop the rest rather than paraphrasing or compressing."""

# Seconds a take spends not speaking (lead-in, tail, grid rounding).
SHOT_OVERHEAD_SECONDS = 2.5
WORDS_PER_SECOND = 1.9


def word_budget(target_seconds: int, shots: int) -> int:
    return max(20, int(WORDS_PER_SECOND * (target_seconds - SHOT_OVERHEAD_SECONDS * shots)))

BREAKDOWN_TOOL = {
    "name": "breakdown",
    "description": "The cast and shot list for the film.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["style", "characters", "shots"],
        "properties": {
            "style": {"type": "string", "description": "One line of visual direction for every shot."},
            "characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["character_id", "name", "appearance", "voice", "possessive", "audition_line"],
                    "properties": {
                        "character_id": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                        "name": {"type": "string"},
                        "appearance": {"type": "string"},
                        "voice": {"type": "string"},
                        "possessive": {"type": "string", "enum": ["their", "her", "his"]},
                        "audition_line": {"type": "string"},
                    },
                },
            },
            "shots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shot_id", "name", "opening_prompt", "prompt", "soundscape", "dialogue", "continuation_of"],
                    "properties": {
                        "shot_id": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                        "name": {"type": "string"},
                        "opening_prompt": {"type": "string"},
                        "prompt": {"type": "string"},
                        "soundscape": {"type": "string"},
                        "continuation_of": {"type": ["string", "null"]},
                        "dialogue": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["character_id", "text"],
                                "properties": {
                                    "character_id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "delivery": {"type": "string"},
                                    "pause_before": {"type": "number"},
                                    "reaction": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Breakdown:
    style: str
    characters: list[CharacterInput]
    shots: list[ShotInput]


class OpenRouterDirector:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = OPENROUTER_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("The director needs an API key")
        if not base_url.startswith("https://"):
            raise ValueError("The director endpoint must be HTTPS")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.transport = transport

    async def plan(
        self, *, brief: str, style: str, shot_count: int, target_seconds: int | None = None
    ) -> Breakdown:
        if not brief.strip() or not 1 <= shot_count <= 40:
            raise PlanError("Describe the film and choose between 1 and 40 shots.")
        if target_seconds is not None and not 10 <= target_seconds <= 600:
            raise PlanError("Choose a target length between 10 and 600 seconds.")
        estimated_shots = max(3, round(target_seconds / 12)) if target_seconds else 0
        length = (
            LENGTH_INSTRUCTION
            % (target_seconds, word_budget(target_seconds, estimated_shots), estimated_shots)
            if target_seconds
            else ""
        )
        system = DIRECTOR_INSTRUCTIONS % {
            "max_words": MAX_WORDS_PER_SHOT,
            "max_shots": shot_count,
            "length": length,
        }
        user = brief.strip() + (f"\n\nVisual direction to keep: {style.strip()}" if style.strip() else "")
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # A model treats the length as advice; the director measures the plan
        # with the timing the takes will use and sends one correction turn.
        for attempt in range(3):
            call, payload = await self._call(messages)
            breakdown = validate_breakdown(payload, shot_count)
            if target_seconds is None:
                return breakdown
            seconds = planned_seconds(breakdown)
            if seconds <= target_seconds * 1.15 or attempt == 2:
                return breakdown
            words = sum(len(line.text.split()) for s in breakdown.shots for line in s.dialogue)
            messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", "breakdown"),
                    "content": (
                        f"Measured: that breakdown runs about {seconds:.0f} seconds of speech "
                        f"({words} words in {len(breakdown.shots)} shots); the target is "
                        f"{target_seconds} seconds. Cut whole lines until at most "
                        f"{word_budget(target_seconds, len(breakdown.shots))} words remain, or "
                        "merge shots, keeping the kept lines word for word, and call the tool again."
                    ),
                }
            )
        return breakdown

    async def _call(self, messages: list[dict]) -> tuple[dict, dict]:
        try:
            async with asyncio.timeout(120):
                async with httpx.AsyncClient(timeout=110, trust_env=False, transport=self.transport) as client:
                    # OpenAI-compatible chat completions with a forced function
                    # call, as OpenRouter exposes for every tool-capable model.
                    response = await client.post(
                        self.base_url,
                        headers={
                            "authorization": f"Bearer {self.api_key}",
                            "content-type": "application/json",
                            "x-title": "Magic Video Studio director",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": 8000,
                            "temperature": 0.4,
                            "messages": messages,
                            "tools": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": BREAKDOWN_TOOL["name"],
                                        "description": BREAKDOWN_TOOL["description"],
                                        "parameters": BREAKDOWN_TOOL["input_schema"],
                                    },
                                }
                            ],
                            "tool_choice": {"type": "function", "function": {"name": "breakdown"}},
                        },
                    )
            if response.status_code != 200:
                raise PlanError("The director is unavailable. Try again or write the shots manually.")
            if len(response.content) > 512_000:
                raise PlanError("The director returned an oversized plan.")
            message = response.json()["choices"][0]["message"]
            call = next(
                c for c in message.get("tool_calls", []) if c["function"]["name"] == "breakdown"
            )
            arguments = call["function"]["arguments"]
            payload = json.loads(arguments) if isinstance(arguments, str) else arguments
        except PlanError:
            raise
        except Exception as exc:
            raise PlanError("The director is unavailable. Try again or write the shots manually.") from exc
        return call, payload


def planned_seconds(breakdown: Breakdown) -> float:
    """Spoken seconds the plan will occupy, with the takes' own timing."""
    total = 0.0
    for s in breakdown.shots:
        if s.dialogue:
            total += dialogue.plan(s.dialogue, continues=s.continuation_of is not None).seconds
    return total


def validate_breakdown(payload: dict, shot_count: int) -> Breakdown:
    try:
        characters = [CharacterInput.model_validate(c) for c in payload["characters"]]
        ids = {c.character_id for c in characters}
        if len(ids) != len(characters) or len(characters) > 8:
            raise ValueError("cast")
        shots = [
            ShotInput.model_validate({**s, "duration_seconds": 4})
            for s in payload["shots"]
        ]
        if not 1 <= len(shots) <= shot_count:
            raise ValueError("shot count")
        seen: set[str] = set()
        for s in shots:
            if s.shot_id in seen or (s.continuation_of is not None and s.continuation_of not in seen):
                raise ValueError("continuity")
            if {line.character_id for line in s.dialogue} - ids:
                raise ValueError("unknown speaker")
            seen.add(s.shot_id)
        shots = split_overflowing(shots, characters)
        style = str(payload.get("style", "")).strip()[:2000]
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        raise PlanError(
            "The suggested breakdown does not fit the generation limits. Try again or edit the plan manually."
        ) from exc
    return Breakdown(style, characters, shots)


def fits(lines: Sequence[DialogueLine], characters: Sequence[CharacterInput], continues: bool) -> bool:
    try:
        dialogue.speakers_for(lines, characters)  # type: ignore[arg-type]
        dialogue.plan(lines, continues=continues)
    except ValueError:
        return False
    return True


def split_overflowing(
    shots: Sequence[ShotInput], characters: Sequence[CharacterInput]
) -> list[ShotInput]:
    """Split any shot whose lines exceed one take into a chain of shots that
    each fit, cutting at speaker turns or sentence boundaries; later parts
    continue the earlier ones and inherit the plate and directions."""
    result: list[ShotInput] = []
    renamed: dict[str, str] = {}
    for shot in shots:
        continues = shot.continuation_of is not None
        continuation = renamed.get(shot.continuation_of or "", shot.continuation_of)
        if not shot.dialogue or fits(shot.dialogue, characters, continues):
            result.append(shot.model_copy(update={"continuation_of": continuation}))
            renamed[shot.shot_id] = shot.shot_id
            continue
        pieces: list[DialogueLine] = []
        for line in shot.dialogue:
            if fits([line], characters, True):
                pieces.append(line)
                continue
            parts = [p for p in _SENTENCE_BREAK.split(line.text.strip()) if p]
            if len(parts) == 1:
                raise ValueError(f"line cannot fit one take: {line.text!r}")
            for i, part in enumerate(parts):
                piece = line.model_copy(
                    update={
                        "text": part,
                        "pause_before": line.pause_before if i == 0 else 0,
                        "reaction": line.reaction if i == len(parts) - 1 else "",
                    }
                )
                if not fits([piece], characters, True):
                    raise ValueError(f"sentence cannot fit one take: {part!r}")
                pieces.append(piece)
        groups: list[list[DialogueLine]] = []
        for line in pieces:
            speakers = {p.character_id for p in (groups[-1] if groups else [])} | {line.character_id}
            if groups and len(speakers) <= 2 and fits([*groups[-1], line], characters, True):
                groups[-1].append(line)
            else:
                groups.append([line])
        previous = continuation
        for index, group in enumerate(groups, 1):
            sid = f"{shot.shot_id}-{index}"
            result.append(
                shot.model_copy(
                    update={
                        "shot_id": sid,
                        "name": f"{shot.name} ({index}/{len(groups)})",
                        "dialogue": group,
                        "continuation_of": previous,
                    }
                )
            )
            previous = sid
        renamed[shot.shot_id] = previous
    return result


def configured_director(values) -> OpenRouterDirector | None:
    key = values.get("MAGIC_VIDEO_EDITOR_DIRECTOR_API_KEY")
    if not key:
        return None
    return OpenRouterDirector(
        key,
        model=values.get("MAGIC_VIDEO_EDITOR_DIRECTOR_MODEL") or DEFAULT_MODEL,
        base_url=values.get("MAGIC_VIDEO_EDITOR_DIRECTOR_URL") or OPENROUTER_URL,
    )
