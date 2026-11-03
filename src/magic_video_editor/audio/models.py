"""An audio set is one revisioned document: cues, their candidates, reviews and releases.

See docs/design/audio-studio.md. A review is bound to the candidate's file hash, and
a release is a frozen pack that is never edited.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CueKind = Literal["line", "sfx", "ambience", "music"]
# `cc0` is CINDER's rule: nothing shipped carries an attribution duty. `cc0_cc_by`
# also admits CC-BY, whose credits the release writes for the owner.
LicencePolicy = Literal["cc0", "cc0_cc_by"]
MAX_CUES = 64
MAX_CANDIDATES_PER_CUE = 16


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class CueInput(Model):
    cue_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    kind: CueKind = "sfx"
    # What the sound is; free-sound searches start from it.
    prompt: str = Field(default="", max_length=1000)
    # The words of a voice line.
    text: str = Field(default="", max_length=600)
    target_seconds: float | None = Field(default=None, gt=0, le=600)
    loop: bool = False
    # How many variants the release ships for this cue.
    keep: int = Field(default=1, ge=1, le=MAX_CANDIDATES_PER_CUE)


class Cue(CueInput):
    selected: list[str] = Field(default_factory=list)


class Licence(Model):
    # CC0-1.0, CC-BY-4.0 or CC-BY-3.0 for a free sound; LicenseRef-Owner-Upload for
    # the owner's own file, whose rights are theirs to vouch for.
    spdx: str
    licence_url: str = ""
    author: str = ""
    source: Literal["freesound", "upload"]
    source_id: str = ""
    source_url: str = ""
    retrieved_at: str
    # Freesound serves its high-quality preview without OAuth; the owner may
    # replace it with the original they download themselves.
    variant: Literal["preview", "original"] = "original"
    attribution_required: bool = False


class Measurement(Model):
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    centroid_hz: float | None = None


class Review(Model):
    outcome: Literal["keep", "reject"]
    note: str = Field(default="", max_length=500)
    reason: str = Field(default="", max_length=80)
    # The file this verdict was given on; a replaced file needs a new review.
    sha256: str
    actor_id: str
    # A signed-in person or an API key (an agent). Releases repeat it, so an
    # agent's batch verdict is never presented as listening by a person.
    actor_kind: Literal["session", "api_key"]
    at: str


class Candidate(Model):
    candidate_id: str
    cue_id: str
    origin: Literal["freesound", "upload"]
    asset_key: str
    sha256: str
    byte_length: int
    media_type: Literal["audio/mpeg", "audio/wav"]
    duration_seconds: float | None = None
    measured: Measurement | None = None
    # The loudness-normalised copy (44.1 kHz 16-bit WAV); the original is never changed.
    rendition_key: str | None = None
    licence: Licence
    # What the source said about it: a free sound's name, uploader, tags and
    # preview URL, or an upload's file name. Earlier files of a replaced preview.
    provenance: dict[str, Any] = Field(default_factory=dict)
    review: Review | None = None
    created_at: str


class AudioRelease(Model):
    release_id: str
    name: str
    asset_key: str
    manifest_sha256: str
    source_revision: int
    created_at: str
    file_count: int
    attribution_required: bool


class AudioSet(Model):
    set_id: str
    owner_id: str
    name: str
    brief: str = ""
    revision: int = 1
    licence_policy: LicencePolicy = "cc0"
    cues: list[Cue] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    releases: list[AudioRelease] = Field(default_factory=list)
    upload_keys: list[str] = Field(default_factory=list)
