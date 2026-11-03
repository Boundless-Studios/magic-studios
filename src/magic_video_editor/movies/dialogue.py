"""Dialogue takes: timing on H3's frame grid and the full-reference prompt.

Speech and lips are generated together by MiniMax H3 from each character's
own voice reference; nothing here dubs or replaces audio. The pacing constants
were calibrated on the accepted WYRM v5 native take, and the prompt layout is
the one that carried the v6 trailer's voices through every join.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magic_video_editor.movies.models import Character, DialogueLine

DIALOGUE_CAPABILITY = "video.minimax_h3.dialogue.v1"
FPS = 24
# H3's trained clip range on its 17k+5 frame grid (about 5.2 s to 15.1 s).
MIN_FRAMES = 124
MAX_FRAMES = 362
# The longest motion shot a person can request in half-second steps: 15.0 s
# is 360 frames, which rounds up to 362 on the grid; 15.5 s would need 379.
MAX_SHOT_SECONDS = 15.0
# Frames the graph pins from the predecessor at frame 0; they flash on decode,
# so the cut drops them and the take's lead-in absorbs them.
GUIDE_FRAMES = 5
STEPS = 40
# Published Ref2VA audio-reference limits.
VOICE_MIN_SECONDS = 2.0
VOICE_MAX_SECONDS = 15.0
VOICES_TOTAL_MAX_SECONDS = 15.0
MAX_SPEAKERS = 2
VOICE_SLOTS = ("voice_a", "voice_b")

# "An impact, long before us, broke the crust open." (12 syllables, two commas)
# was spoken in 4.30 s; 12 / 3.2 + 2 * 0.3 = 4.35 s.
SYLLABLES_PER_SECOND = 3.2
PAUSES = {
    ",": 0.3,
    ";": 0.4,
    ":": 0.4,
    "—": 0.35,
    ".": 0.7,
    "?": 0.7,
    "!": 0.6,
    "...": 0.8,
    "…": 0.8,
}
_PUNCTUATION = re.compile(r"\.\.\.|…|[,;:—.?!]")


def align_frames(frames: int) -> int:
    """Smallest count on H3's 17k+5 grid that is at least ``frames``."""
    n = max(5, int(frames))
    while n % 17 != 5:
        n += 1
    return n


def motion_frames(seconds: float) -> int:
    """Frames for a motion clip of ``seconds`` on H3's native 24 fps clock,
    rounded up to the grid so picture and native audio share one timeline."""
    return align_frames(max(17, math.ceil(seconds * FPS)))


def count_syllables(word: str) -> int:
    letters = re.sub(r"[^a-z]", "", word.lower())
    if not letters:
        return 0
    groups = len(re.findall(r"[aeiouy]+", letters))
    if letters.endswith("e") and not letters.endswith(("le", "ee", "ye")) and groups > 1:
        groups -= 1
    return max(1, groups)


def spoken_seconds(text: str) -> float:
    """First to last word, including internal pauses; the final mark's pause
    belongs to the following turn gap."""
    syllables = sum(count_syllables(w) for w in re.findall(r"[A-Za-z']+", text))
    internal = text.strip().rstrip("\"'”’ ").rstrip(".?!…")
    pauses = sum(PAUSES[m] for m in _PUNCTUATION.findall(internal))
    return syllables / SYLLABLES_PER_SECOND + pauses


@dataclass(frozen=True)
class Beat:
    character_id: str
    start: float
    end: float


@dataclass(frozen=True)
class TakePlan:
    frames: int
    seconds: float
    beats: tuple[Beat, ...]


def plan(
    lines: Sequence[DialogueLine],
    *,
    continues: bool = False,
    lead_in: float = 0.8,
    turn_gap: float = 0.8,
    sentence_gap: float = 0.6,
    tail: float = 1.5,
) -> TakePlan:
    """Allocate one take around natural delivery; grid slack extends the quiet
    tail and nothing is shortened to fit. Raises ValueError when the lines need
    more than one take."""
    if not lines:
        raise ValueError("Write at least one line of dialogue")
    t = lead_in + (GUIDE_FRAMES / FPS if continues else 0.0)
    beats = []
    previous = None
    for line in lines:
        if previous is not None:
            t += turn_gap if line.character_id != previous.character_id else sentence_gap
        t += max(0.0, line.pause_before)
        spoken = spoken_seconds(line.text)
        beats.append(Beat(line.character_id, round(t, 3), round(t + spoken, 3)))
        t += spoken
        previous = line
    frames = align_frames(max(MIN_FRAMES, math.ceil((t + tail) * FPS)))
    if frames > MAX_FRAMES:
        raise ValueError(
            f"{t + tail:.1f}s of dialogue does not fit one {MAX_FRAMES / FPS:.0f}-second "
            "take; split it into two shots at a conversational beat"
        )
    return TakePlan(frames, frames / FPS, tuple(beats))


def speakers_for(
    lines: Sequence[DialogueLine], characters: Sequence[Character]
) -> list[Character]:
    """The shot's speaking characters in cast order, so a character keeps the
    same reference label on every take."""
    spoken = {line.character_id for line in lines}
    cast = [c for c in characters if c.character_id in spoken]
    missing = spoken - {c.character_id for c in cast}
    if missing:
        raise ValueError("Every line must belong to a character in the cast")
    if len(cast) > MAX_SPEAKERS:
        raise ValueError(f"Use at most {MAX_SPEAKERS} speaking characters per shot")
    return cast


def check_voices(speakers: Sequence[Character]) -> None:
    total = 0.0
    for c in speakers:
        if not c.voice_asset_key or c.voice_seconds is None:
            raise ValueError(f"Add a voice reference for {c.name}")
        total += c.voice_seconds
    if total > VOICES_TOTAL_MAX_SECONDS:
        raise ValueError(
            f"Voice references total {total:.1f}s; together they must stay under "
            f"{VOICES_TOTAL_MAX_SECONDS:g}s"
        )


def _pause_phrase(seconds: float) -> str:
    if seconds < 0.45:
        return "a brief beat"
    if seconds < 0.9:
        return "a short pause"
    if seconds < 1.5:
        return "a full, unhurried breath"
    return "a long, thoughtful pause"


def build_prompt(
    lines: Sequence[DialogueLine],
    speakers: Sequence[Character],
    take: TakePlan,
    *,
    style: str,
    setting: str,
    camera: str,
    soundscape: str,
    continues: bool,
) -> str:
    """The H3 full-reference layout. ``<Picture 1>`` is the concrete opening
    frame (the predecessor's final moment when the shot continues one), which
    the graph also pins at frame 0; ``<Picture 2>`` is the plate the shot
    resolves onto, pinned at the last frame — locking both ends keeps the
    background from drifting. Voices are ``<Audio n>`` in speaker order; speaker
    IDs follow the order of vocal events, as the H3 guide requires."""
    subject = {c.character_id: f"<Subject {i}>" for i, c in enumerate(speakers, 1)}
    voice = {c.character_id: f"<Audio {i}>" for i, c in enumerate(speakers, 1)}
    by_id = {c.character_id: c for c in speakers}
    speaker_ids: dict[str, str] = {}
    for line in lines:
        speaker_ids.setdefault(line.character_id, f"S{len(speaker_ids) + 1}")

    definitions = ["subject_definitions:"]
    for c in speakers:
        look = f", {c.appearance}" if c.appearance else ""
        definitions.append(f"{subject[c.character_id]} is {c.name}{look} in <Picture 1>.")
    definitions.append("<Picture 2> is the exact frame the target video ends on.")
    for c in speakers:
        voice_description = f", {c.name}'s {c.voice}" if c.voice else ""
        definitions.append(
            f"{voice[c.character_id]} is the voice-timbre reference for "
            f"{subject[c.character_id]} ({speaker_ids[c.character_id]}){voice_description}."
        )

    if continues:
        opening = (
            "The shot begins from <Picture 1>, the final moment of the preceding shot, "
            "and carries that moment forward without restarting"
        )
    else:
        opening = "The shot begins from <Picture 1>"
    voices = "; ".join(
        f"{subject[cid]} ({sid}) speaks with the voice timbre of {voice[cid]}"
        for cid, sid in speaker_ids.items()
    )
    summary = (
        f"[keyframe completion + reference generation + audio reference] {opening} "
        f"and ends on <Picture 2>; {voices}."
    )

    retention = ["retention_analysis:"]
    for c in speakers:
        retention.append(
            f"{subject[c.character_id]} (appears in [Shot 1]): fully_preserved - "
            f"{c.name}'s identity, face, hair, clothing and position are retained."
        )
    if continues:
        retention.append(
            "<Picture 1> ([Shot 1] first frame): fully_preserved - the preceding shot's "
            "final moment is the concrete opening frame; framing, poses, lighting and "
            "setting continue without a reset."
        )
    else:
        retention.append(
            "<Picture 1> ([Shot 1] first frame): fully_preserved - use it as the "
            "concrete opening frame."
        )
    retention.append(
        "<Picture 2> ([Shot 1] last frame): fully_preserved - the shot resolves onto "
        "this exact frame."
    )
    for c in speakers:
        retention.append(
            f"{voice[c.character_id]}: reference - {subject[c.character_id]}'s dialogue "
            f"follows {voice[c.character_id]}'s voice timbre, accent and delivery without "
            "copying the original signal or its words."
        )

    place = f": {setting}." if setting else "."
    if continues:
        sentences = [
            "[Shot 1] The shot begins from <Picture 1>, the final moment of the preceding "
            f"shot, in the same place{place}",
            "The conversation is already under way from the preceding shot; nothing "
            "restarts or resets.",
        ]
    else:
        sentences = [f"[Shot 1] The shot begins from <Picture 1>{place}"]
    if camera:
        sentences.append(camera)
    previous = None
    for line, beat in zip(lines, take.beats):
        if previous is None:
            # The model stretched "a brief beat" into five seconds of dead air;
            # name the moment the first line starts.
            lead = (
                f"After {_pause_phrase(beat.start)} of quiet, "
                if beat.start >= 1.5
                else f"Within the first {'second' if beat.start < 1.0 else 'moment'}, "
            )
        elif line.pause_before > 0:
            lead = f"After {_pause_phrase(line.pause_before)}, "
        else:
            lead = ""
        delivery = f" {line.delivery}," if line.delivery else ""
        verb = (
            "continues"
            if previous is not None and previous.character_id == line.character_id
            else "says"
        )
        cid = line.character_id
        sentences.append(
            f"{lead}{subject[cid]} ({speaker_ids[cid]}),{delivery} in the voice referenced "
            f"from {voice[cid]}, {verb}: <d>[English] {line.text}</d>"
        )
        if line.reaction:
            sentences.append(line.reaction)
        previous = line
    last = by_id[lines[-1].character_id]
    # Phrased as description, not as a quotable sentence: the model has read
    # "Exactly as his voice stops" aloud when it looked like a line.
    sentences.append(
        f"{subject[last.character_id]}'s lips close on {last.possessive} final word, "
        "the jaw still."
    )
    sentences.append(
        "By the end the scene settles back into <Picture 2> exactly: the same poses, "
        "framing, expressions and lighting, and the final frame matches it."
    )
    sentences.append(
        "Only the active speaker's lips move; the listener's lips stay closed. No other "
        "words, narration or singing occur."
    )
    # "Holds on the final moment" produced literal freeze-frames of up to two
    # seconds before every cut; the hold must stay alive.
    sentences.append(
        "Through the end of the "
        f"{take.seconds:.1f}-second video the shot stays on the quiet final moment, "
        "alive and continuous: breathing, small shifts of weight and gaze, the mist "
        "and torchlight moving; never a frozen frame."
    )
    return "\n".join(
        [
            *definitions,
            "",
            "summary:",
            summary,
            "",
            *retention,
            "",
            "detailed_description:",
            f"The target video is {style}." if style else "The target video is cinematic.",
            " ".join(sentences),
            "",
            "overall_soundscape:",
            soundscape or "Natural room tone of the setting; no music.",
            "",
            "non_diegetic_music:",
            "N/A",
        ]
    )
