"""Reference entries for audio sets: free sounds, uploads, review and releases."""

from typing import Literal

from pydantic import BaseModel, Field

from magic_video_editor.api_reference.model import RouteDoc
from magic_video_editor.audio.freesound import FreeSound
from magic_video_editor.audio.models import AudioSet
from magic_video_editor.movies.models import Movie

SET_404 = "No audio set with this id, or it is not yours."
CANDIDATE_404 = "No audio set, cue or candidate with these ids, or it is not yours."
STALE = "`expected_revision` is stale (`detail.current_revision`)."
STORAGE_503 = "File storage is unavailable right now. Retry later."
LIMITS_507 = "Your storage allowance is used up."
FREESOUND_503 = "Free-sound search is not configured on this server."
FREESOUND_502 = "Freesound or its CDN did not answer."


class AudioCapabilities(BaseModel):
    account_tier: Literal["invited", "public"]
    free_sounds_available: bool = Field(description="Freesound search and import can be used.")
    upload_available: bool
    processing_available: bool = Field(
        description="Candidates get a loudness rendition and measurements. Off for public accounts."
    )
    replace_original_available: bool = Field(description="A preview can be replaced by its original.")
    send_to_movie_available: bool
    generation_available: bool = Field(description="Always false in this release.")
    licence_policies: list[str]
    max_cues: int
    max_candidates_per_cue: int
    max_file_bytes: int
    upload_types: list[str]


class AudioUploadGrant(BaseModel):
    asset_key: str
    upload_url: str
    upload_headers: dict[str, str]
    revision: int


class ReadinessProblem(BaseModel):
    cue_id: str | None
    message: str


class AudioReadiness(BaseModel):
    ready: bool
    problems: list[ReadinessProblem]
    files: int = Field(description="How many files a release would hold.")
    attribution_required: bool = Field(description="A selected file is CC-BY.")
    previews: int = Field(description="Selected files that are Freesound previews, not originals.")


ROUTES: dict[tuple[str, str], RouteDoc] = {
    ("GET", "/studio/audio/capabilities"): RouteDoc(
        tag="Audio",
        response=AudioCapabilities,
        summary="Get what audio sets can do",
        description=(
            "Which candidate sources this account can use, whether candidates are "
            "measured and rendered, and the limits on cues, candidates and files."
        ),
    ),
    ("POST", "/studio/audio"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Create an audio set",
        description="Creates an empty audio set called `name`, with the CC0-only licence policy.",
    ),
    ("GET", "/studio/audio"): RouteDoc(
        tag="Audio",
        response=list[AudioSet],
        summary="List your audio sets",
        description="Every audio set you own, newest first, each at its current revision.",
    ),
    ("GET", "/studio/audio/{id}"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Get an audio set",
        description="The set at its current revision: cues, candidates with their reviews, and releases.",
        errors={404: SET_404},
    ),
    ("PUT", "/studio/audio/{id}"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Save an audio set's details and cues",
        description=(
            "Replaces the name, brief, licence policy and the cues, at most 64. A cue "
            "has an id, a name, a `kind` (`line`, `sfx`, `ambience` or `music`), an "
            "optional prompt, line text and target length, a loop flag and `keep`, the "
            "number of variants a release ships.\n\n"
            "`licence_policy` is `cc0` (no attribution duty, ever) or `cc0_cc_by`, which "
            "also admits CC-BY sounds and writes their credits into releases. Removing "
            "a cue removes its candidates; a selection keeps only what the new policy "
            "and `keep` allow."
        ),
        errors={404: SET_404, 409: STALE, 422: "Cue ids repeat, or a field is out of range."},
    ),
    ("DELETE", "/studio/audio/{id}"): RouteDoc(
        tag="Audio",
        summary="Delete an audio set",
        description=(
            "Permanently deletes the set with every candidate, release and file. "
            "`confirm_name` must repeat the set's exact name."
        ),
        errors={404: SET_404, 422: "`confirm_name` does not match (`deletion_not_confirmed`).", 503: STORAGE_503},
    ),
    ("GET", "/studio/audio/{id}/free-sounds"): RouteDoc(
        tag="Audio",
        response=list[FreeSound],
        summary="Search free sounds",
        description=(
            "Searches Freesound for `q`, between `min_seconds` and `max_seconds`, 15 "
            "results a `page`. Results are filtered to the set's licence policy and "
            "checked again one by one: CC0, plus CC-BY when the set allows it. "
            "Non-commercial and Sampling+ sounds are never returned.\n\n"
            "Nothing is stored. `preview_url` streams Freesound's MP3 preview from its "
            "CDN; import a result to keep it."
        ),
        errors={
            404: SET_404,
            422: "The shortest length is not below the longest.",
            429: "Too many searches from this account; wait a minute.",
            502: FREESOUND_502,
            503: FREESOUND_503,
        },
    ),
    ("POST", "/studio/audio/{id}/cues/{cue_id}/import"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Import a free sound as a candidate",
        description=(
            "Fetches the sound `source_id` from Freesound, checks its licence again "
            "against the set's policy, downloads its high-quality MP3 preview and "
            "adds it to the cue as a candidate. The licence, author and source as "
            "Freesound reported them now are recorded with the file's SHA-256.\n\n"
            "The candidate is marked a `preview`. Replace it with the original you "
            "download yourself through `POST …/candidates/{candidate_id}/original`."
        ),
        errors={
            404: CANDIDATE_404,
            409: f"{STALE} Or the cue already has this sound, or holds 16 candidates.",
            422: "The sound's licence is not one the set allows, or it is not a valid MP3.",
            429: "Freesound is busy, or you have started too much work.",
            502: FREESOUND_502,
            503: FREESOUND_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/audio/{id}/uploads"): RouteDoc(
        tag="Audio",
        response=AudioUploadGrant,
        summary="Grant an audio upload",
        description=(
            "Grants a storage key on this set for one file of `content_type` "
            "`audio/mpeg` or `audio/wav`. PUT the bytes to `upload_url` with the same "
            "`Content-Type` and any `upload_headers`; that request needs no Studio "
            "authorization. Then pass `asset_key` to `POST …/cues/{cue_id}/upload` "
            "or `POST …/candidates/{candidate_id}/original`."
        ),
        errors={
            404: SET_404,
            409: "The set changed during the grant; retry.",
            422: "The content type is not WAV or MP3.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/audio/{id}/cues/{cue_id}/upload"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Add an uploaded file as a candidate",
        description=(
            "Adds a granted upload to the cue as a candidate. It is stored under its "
            "SHA-256 and, where the account may use processing, measured and given a "
            "loudness rendition. Uploads are your own files; their rights are yours."
        ),
        errors={
            403: "The upload was not granted to this set.",
            404: CANDIDATE_404,
            409: f"{STALE} Or the cue already has this file, or holds 16 candidates.",
            422: "The file is not a valid WAV or MP3, or is longer than an hour.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/audio/{id}/candidates/{candidate_id}/original"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Replace a preview with its original",
        description=(
            "Swaps a Freesound preview candidate's file for a granted upload of the "
            "original. It must be as long as the sound Freesound reported, within half "
            "a second or 2%. The candidate keeps its licence record, becomes an "
            "`original`, and needs a new review, since its file changed."
        ),
        errors={
            403: "The upload was not granted, or public accounts cannot use this yet.",
            404: CANDIDATE_404,
            409: f"{STALE} Or the candidate is not a Freesound preview.",
            422: "The file is not valid audio, or its length does not match.",
            503: "Audio processing or storage is unavailable.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/audio/{id}/candidates/{candidate_id}/review"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Keep or reject a candidate",
        description=(
            "Records `outcome` (`keep` or `reject`) with an optional `note` and "
            "`reason`, bound to the candidate's current file hash. The review records "
            "whether a signed-in person or an API key gave it, and releases repeat "
            "that. Rejecting a candidate removes it from its cue's selection.\n\n"
            "Send `sha256`, the candidate's file hash as you read it, so that a verdict "
            "on a file replaced since (a preview swapped for its original) is refused "
            "instead of applied to the new file."
        ),
        errors={404: CANDIDATE_404, 409: f"{STALE} Or `sha256` no longer matches (`candidate_file_changed`)."},
    ),
    ("PUT", "/studio/audio/{id}/cues/{cue_id}/selection"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Select a cue's variants",
        description=(
            "Sets the cue's shipped variants, in order, at most `keep`. Each must be a "
            "candidate of this cue that you kept, with a licence the set allows."
        ),
        errors={
            404: CANDIDATE_404,
            409: f"{STALE} Or a candidate is not kept or not allowed.",
            422: "Too many candidates, a repeat, or one from another cue.",
        },
    ),
    ("GET", "/studio/audio/{id}/readiness"): RouteDoc(
        tag="Audio",
        response=AudioReadiness,
        summary="Check whether the set can be released",
        description=(
            "Lists what blocks a release: cues without their variants selected, or "
            "selected files no longer kept or allowed. Also says whether a release "
            "would need attribution and how many files are previews."
        ),
        errors={404: SET_404},
    ),
    ("POST", "/studio/audio/{id}/release"): RouteDoc(
        tag="Audio",
        response=AudioSet,
        summary="Release an audio set",
        description=(
            "Freezes the selection into a zip called `name`: `audio/` (loudness "
            "renditions, or the originals when there are none), `originals/`, a "
            "`manifest.json` with every file's licence, author, source, hashes, "
            "measurements, processing and review, and a `CREDITS.md`.\n\n"
            "A release never changes. Download it with "
            "`GET /studio/audio/{id}/releases/{release_id}/download`."
        ),
        errors={
            404: SET_404,
            409: f"{STALE} Or the set is not ready, or a release with this name exists.",
            413: "The selection is larger than 256 MB.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/audio/{id}/candidates/{candidate_id}/send-to-movie"): RouteDoc(
        tag="Audio",
        response=Movie,
        summary="Send a candidate to a movie",
        description=(
            "Adds a kept candidate (its rendition when there is one) to one of your "
            "movies as a soundtrack cue at the start, through the movie's own "
            "soundtrack checks. `movie_revision` is the movie's current revision. "
            "Returns the movie. Shape the cue on the movie afterwards.\n\n"
            "A sound whose licence requires attribution (CC-BY) is refused, because "
            "movie releases carry no credits yet."
        ),
        errors={
            403: "Public accounts cannot use movie soundtracks yet.",
            404: "No audio set, candidate or movie with these ids, or it is not yours.",
            409: (
                "The candidate is not kept, its licence requires attribution "
                "(`attribution_required`), or `movie_revision` is stale (`detail.current_revision`)."
            ),
            429: None,
            507: LIMITS_507,
            422: "The movie already has eight cues, or the audio is not valid.",
            503: "Movies or storage are unavailable.",
        },
    ),
    ("GET", "/studio/audio/{id}/releases/{release_id}/download"): RouteDoc(
        tag="Audio",
        file="The release zip.",
        summary="Download an audio release",
        description="Streams one of the set's releases as `audio-<release_id>.zip`. Supports Range requests.",
        errors={404: "No set or release with these ids, or it is not yours.", 416: None, 503: STORAGE_503},
    ),
    ("GET", "/studio/audio/{id}/assets/{key}"): RouteDoc(
        tag="Audio",
        file="The stored file.",
        summary="Get an audio set file",
        description=(
            "Returns one of the set's files by key: a candidate's original or "
            "rendition, or a release. Audio is served inline. Supports Range requests."
        ),
        errors={404: "No such file in this set, or it is not yours.", 416: None, 503: STORAGE_503},
    ),
}
