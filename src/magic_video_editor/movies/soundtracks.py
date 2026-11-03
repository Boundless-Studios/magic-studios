"""Reviewed music and narration cues, frozen into the assembly snapshot."""

from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from magic_video_editor.movies.models import Movie
    from magic_video_editor.movies.service import MovieService

MAX_AUDIO_BYTES = 50 * 1024 * 1024


class AudioTrack(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, validate_default=True
    )
    asset_key: str
    offset_seconds: float = Field(default=0, ge=0, le=3600)
    trim_start_seconds: float = Field(default=0, ge=0, le=3600)
    gain_db: float = Field(default=0, ge=-60, le=12)
    # Shape: where the cue stops in the movie, how it enters and leaves, and a
    # swell (gain ramp over the final seconds before it ends) for a crescendo.
    end_seconds: float | None = Field(default=None, gt=0, le=3600)
    fade_in_seconds: float = Field(default=0, ge=0, le=30)
    fade_out_seconds: float = Field(default=0, ge=0, le=30)
    swell_db: float = Field(default=0, ge=-30, le=30)
    swell_seconds: float = Field(default=0, ge=0, le=60)

    @model_validator(mode="after")
    def _shape_fits(self) -> "AudioTrack":
        if self.end_seconds is not None and self.end_seconds <= self.offset_seconds:
            raise ValueError("The cue must end after it starts.")
        if bool(self.swell_db) != bool(self.swell_seconds):
            raise ValueError("A swell needs both a level and a duration.")
        return self


async def save_soundtracks(
    service: "MovieService",
    movie_id: str,
    owner: str,
    expected: int,
    tracks: list[AudioTrack],
) -> "Movie":
    from magic_video_editor.movies.service import MovieError

    movie = await service.current(movie_id, owner, expected)
    if len(tracks) > 8:
        raise MovieError("Use at most eight music or narration cues.")
    if service.media is None:
        raise MovieError("Audio processing is unavailable.", 503)
    allowed = set(movie.upload_keys) | {track.asset_key for track in movie.soundtracks}
    frozen = []
    for track in tracks:
        if track.asset_key not in allowed:
            raise MovieError("Audio is not granted to this movie.", 403)
        try:
            content = await service.assets.read_limited(
                track.asset_key, MAX_AUDIO_BYTES
            )
            duration = await service.media.audio_duration(content)
            if track.trim_start_seconds >= duration:
                raise ValueError("Trim starts after the audio ends.")
        except Exception as exc:
            raise MovieError(
                "Choose valid audio under 50 MB and trim within its duration."
            ) from exc
        key = f"outputs/movies/{movie_id}/audio/{sha256(content).hexdigest()}"
        # The probe above accepts only WAV or MP3. Preserve that verified media
        # type for signed playback URLs, including extensionless immutable keys.
        content_type = (
            "audio/wav"
            if content[:4] in {b"RIFF", b"RF64", b"BW64"} and content[8:12] == b"WAVE"
            else "audio/mpeg"
        )
        await service.assets.write_once(key, content, content_type)
        frozen.append(track.model_copy(update={"asset_key": key}))
    movie.soundtracks = frozen
    movie.assembly = None
    return await service.save(movie, expected)
