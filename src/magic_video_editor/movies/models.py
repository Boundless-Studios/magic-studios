from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from magic_video_editor.movies.dialogue import MAX_SHOT_SECONDS
from magic_video_editor.movies.soundtracks import AudioTrack


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, validate_default=True
    )


class DialogueLine(Model):
    character_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=600)
    delivery: str = Field(default="", max_length=200)
    pause_before: float = Field(default=0, ge=0, le=3)
    reaction: str = Field(default="", max_length=300)


class MotionBeat(Model):
    """One timed physical event inside a motion shot ("at 1.5 s the hull shudders once")."""

    at_seconds: float = Field(ge=0, le=MAX_SHOT_SECONDS)
    action: str = Field(min_length=1, max_length=300)


class CharacterInput(Model):
    character_id: str = Field(
        min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    name: str = Field(min_length=1, max_length=120)
    appearance: str = Field(default="", max_length=600)
    voice: str = Field(default="", max_length=300)
    possessive: Literal["their", "her", "his"] = "their"
    # An uploaded 2-15 s clip of the character's own voice; frozen on save.
    voice_asset_key: str | None = None
    # What the character says when auditioned: a short solo take from the
    # voice description whose audio becomes the reference.
    audition_line: str = Field(
        default="Here I am. This is how I sound when I speak plainly, and take my time.",
        min_length=1,
        max_length=240,
    )


class Character(CharacterInput):
    voice_seconds: float | None = None
    # A composed reference sheet of the character; every storyboard frame they
    # appear in is drawn from it.
    portrait_asset_key: str | None = None
    portrait_appearance: str | None = None


class Take(Model):
    select_when_ready: bool = False
    take_id: str
    request_id: str
    status: Literal[
        "waiting", "submitting", "running", "succeeded", "failed", "cancelled"
    ] = "waiting"
    job_id: str
    tenant_id: str | None = None
    completed_by_worker_id: str | None = None
    completed_at: str | None = None
    output_sha256: str | None = None
    output_byte_length: int | None = None
    asset_key: str
    final_frame_key: str | None = None
    predecessor_take_id: str | None = None
    opening_asset_key: str | None = None
    # Missing fields describe already-submitted legacy jobs; never change retries.
    provider_width: int = 768
    provider_height: int = 432
    provider_fps: int = 16
    provider_frame_key: str | None = None
    prompt: str
    duration_seconds: float
    # Dialogue takes speak the shot's lines with each character's own voice
    # reference and land on the shot's plate; missing fields mean a motion take.
    mode: Literal["motion", "dialogue", "flight"] = "motion"
    length_frames: int | None = None
    # Scene-flight legs pin the last frame too: the destination image as
    # granted, and the crop frozen to the provider canvas.
    closing_asset_key: str | None = None
    closing_frame_key: str | None = None
    # Recorded at request time so a re-roll or audit sees what actually ran;
    # missing on motion/dialogue takes, whose recipe is fixed in `submit`.
    seed: int | None = None
    steps: int | None = None
    capability_id: str | None = None
    executor_preset: str | None = None
    # Mean absolute greyscale difference (0-1) between the clip's first/last
    # decoded frames and the pinned images; measured, never a gate.
    first_frame_diff: float | None = None
    last_frame_diff: float | None = None
    plate_asset_key: str | None = None
    plate_frame_key: str | None = None
    voice_asset_keys: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    stale: bool = False
    cancel_requested: bool = False
    reviewed_for_predecessor_take_id: str | None = None
    reviewed_spec_digest: str | None = None


class ShotInput(Model):
    shot_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    opening_prompt: str = Field(default="", max_length=8000)
    duration_seconds: float = Field(
        default=4, ge=1.5, le=MAX_SHOT_SECONDS, multiple_of=0.5
    )
    continuation_of: str | None = None
    # Spoken lines make this a dialogue shot: its length follows the lines,
    # and it ends on its own plate image.
    dialogue: list[DialogueLine] = Field(default_factory=list, max_length=12)
    soundscape: str = Field(default="", max_length=400)
    # Motion direction for H3 image-to-video: one camera move (or none), what in the
    # opening frame must stay exactly as it is, and timed physical beats.
    camera: str = Field(default="", max_length=300)
    still: str = Field(default="", max_length=400)
    beats: list[MotionBeat] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _beats_inside_the_shot(self):
        if any(b.at_seconds >= self.duration_seconds for b in self.beats):
            raise ValueError("Each motion beat must happen inside the shot")
        return self


class Shot(ShotInput):
    opening_asset_key: str | None = None
    # Scene flights only: the image the leg must land on, and wording the
    # model should avoid (H3 has no negative conditioning, so it is folded
    # into the prompt).
    closing_asset_key: str | None = None
    negative_prompt: str = Field(default="", max_length=1000)
    opening_approved: bool = False
    takes: list[Take] = Field(default_factory=list)
    selected_take_id: str | None = None


class Clip(Model):
    shot_id: str
    take_id: str
    in_seconds: float = Field(default=0, ge=0)
    out_seconds: float = Field(gt=0)
    transition_seconds: float = Field(default=0, ge=0, le=1)
    # A shot with no lines carries no voice: whatever the model vocalised is
    # replaced with silence at assembly (ambience comes from cues).
    mute: bool = False

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        # Keep historical hard-cut digests and immutable releases unchanged.
        if not self.transition_seconds:
            data.pop("transition_seconds", None)
        if not self.mute:
            data.pop("mute", None)
        return data


class Join(Model):
    # The cut between clip `index` and the next, measured on the assembled movie.
    index: int
    at_seconds: float
    diff: float
    limit: float


class Assembly(Model):
    tenant_id: str | None = None
    request_id: str
    status: Literal["pending", "succeeded", "failed"] = "pending"
    asset_key: str
    error: str | None = None
    cut_digest: str
    clips: list[Clip]
    soundtracks: list[AudioTrack] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)


class Release(Model):
    release_id: str
    name: str
    asset_key: str
    cut_digest: str
    source_revision: int


class MovieBuild(Model):
    request_id: str
    status: Literal["generating", "assembling", "succeeded", "failed"] = "generating"
    plan_digest: str
    error: str | None = None
    # Pin a take per shot before proceeding; reloads never spend a second take.
    takes: dict[str, str] = Field(default_factory=dict)
    requests: list[str] = Field(default_factory=list)


class Movie(Model):
    movie_id: str
    owner_id: str
    name: str
    # A scene flight reuses the movie store, takes and reconciliation; its
    # shots are frame-pinned legs edited through /studio/scene-flights.
    kind: Literal["movie", "scene_flight"] = "movie"
    revision: int = 1
    brief: str = ""
    style: str = ""
    batch_size: int = 3
    compute_target: Literal["shared", "my_machines"] = "shared"
    characters: list[Character] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    cut: list[Clip] = Field(default_factory=list)
    soundtracks: list[AudioTrack] = Field(default_factory=list)
    assembly: Assembly | None = None
    build: MovieBuild | None = None
    releases: list[Release] = Field(default_factory=list)
    requests: dict[str, list[str]] = Field(default_factory=dict)
    upload_keys: list[str] = Field(default_factory=list)
    compositions: dict[str, dict] = Field(default_factory=dict)
