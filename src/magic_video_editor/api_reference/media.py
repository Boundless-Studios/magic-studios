"""Reference entries for media routes: movies and scene flights."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from magic_video_editor.api_reference.model import RouteDoc
from magic_video_editor.movies import models as movie_models
from magic_video_editor.movies.models import CharacterInput, Join, ShotInput

# Doc-only shapes of the success bodies. The routes return plain dicts or the
# movie snapshot itself; these mirror them for the reference and change nothing.

TakeStatus = Literal["waiting", "submitting", "running", "succeeded", "failed", "cancelled"]


# The studio has its own, different `Release`, so the movie's gets its own name.


class MovieRelease(movie_models.Release):
    """A named, frozen copy of an assembly."""


class Movie(movie_models.Movie):
    """A movie (or scene flight) at one revision."""

    releases: list[MovieRelease] = Field(default_factory=list)


class MovieListItem(Movie):
    expires_at: datetime | None = Field(
        description="When inactivity deletion removes the movie unless it is opened; "
        "`null` unless that is close."
    )


class MovieCapabilities(BaseModel):
    account_tier: Literal["invited", "public"]
    generation_available: bool = Field(description="Shot takes can be started.")
    composition_available: bool = Field(description="Opening images can be generated.")
    planner_available: bool = Field(description="`POST …/plan` is available.")
    director_available: bool = Field(
        description="Plans include style and cast and follow `target_seconds`."
    )
    min_duration_seconds: float
    duration_step_seconds: float
    max_duration_seconds: float
    max_batch_size: int = Field(description="The largest `batch_size` a movie may use.")
    dialogue_available: bool = Field(description="Dialogue shots and voice uploads can be used.")
    max_dialogue_seconds: float
    voice_min_seconds: float = Field(description="Shortest voice reference clip.")
    voice_max_seconds: float = Field(description="Longest voice reference clip.")
    max_speakers_per_shot: int


class MoviePlan(BaseModel):
    style: str | None = Field(default=None, description="Present only when the director planned.")
    characters: list[CharacterInput] | None = Field(
        default=None, description="Present only when the director planned."
    )
    shots: list[ShotInput]


class MovieUploadGrant(BaseModel):
    asset_key: str = Field(description="Pass this to the route that uses the file.")
    upload_url: str = Field(description="Signed URL to PUT the bytes to.")
    upload_headers: dict[str, str] | None = Field(
        default=None,
        description="Public accounts only: headers the PUT must send.",
    )
    revision: int = Field(description="The movie revision after the grant.")


class SceneFlightRecipe(BaseModel):
    capability_id: str
    width: int
    height: int
    fps: int
    steps: int
    default_duration_seconds: float
    min_duration_seconds: float
    max_duration_seconds: float
    duration_step_seconds: float
    max_legs: int


class SceneFlightCapabilities(SceneFlightRecipe):
    generation_available: bool = Field(description="This server can pin a last frame.")
    image_types: list[str]


class SceneFlightTake(BaseModel):
    take_id: str
    request_id: str
    status: TakeStatus
    stale: bool = Field(description="The leg changed since this take was made.")
    error: str | None
    video_key: str | None = Field(description="Set once the take has succeeded.")
    last_frame_key: str | None
    origin_frame_key: str | None = Field(description="The origin as cropped for the model.")
    destination_frame_key: str | None = Field(
        description="The destination as cropped for the model."
    )
    prompt: str
    seed: int | None
    width: int
    height: int
    fps: int
    length_frames: int | None
    steps: int | None
    capability_id: str | None
    executor_preset: str | None
    duration_seconds: float = Field(
        description="Requested length until the clip exists, then its measured length."
    )
    first_frame_diff: float | None = Field(
        description="Mean difference (0-1) between the first frame and the origin."
    )
    last_frame_diff: float | None = Field(
        description="Mean difference (0-1) between the last frame and the destination."
    )
    job_id: str
    completed_by_worker_id: str | None
    completed_at: str | None
    output_sha256: str | None
    output_byte_length: int | None


class SceneFlightLeg(BaseModel):
    leg_id: str
    index: int
    name: str
    origin_image_key: str | None
    destination_image_key: str | None
    prompt: str = Field(description="The camera direction as composed or given.")
    negative_prompt: str
    duration_seconds: float
    selected_take_id: str | None
    takes: list[SceneFlightTake]


class SceneFlightChain(BaseModel):
    request_id: str
    status: Literal["pending", "succeeded", "failed"]
    video_key: str | None = Field(description="Set once the chain has succeeded.")
    error: str | None
    joins: list[Join] = Field(description="Measured difference at each cut.")


class SceneFlight(BaseModel):
    flight_id: str
    name: str
    revision: int
    generation_available: bool
    recipe: SceneFlightRecipe
    images: list[str] = Field(description="Image keys added to this flight.")
    legs: list[SceneFlightLeg]
    chain: SceneFlightChain | None


class SceneFlightSummary(BaseModel):
    flight_id: str
    name: str
    revision: int
    leg_count: int
    active_takes: int
    expires_at: datetime | None = Field(
        description="When inactivity deletion removes the flight unless it is opened; "
        "`null` unless that is close."
    )


class SceneFlightImage(BaseModel):
    image_key: str = Field(description="Use as a leg's origin or destination.")
    width: int
    height: int
    sha256: str = Field(description="Digest of the stored PNG.")
    revision: int

MOVIE_404 = "No such movie, or not yours."
FLIGHT_404 = "No such scene flight, or not yours."
STALE = (
    "`expected_revision` is stale. `detail.current_revision` has the current one; "
    "re-read and retry."
)
BUSY = "The movie changed under the request too many times; retry."
REFUSED_ACCOUNT = (
    "Your account may not start this work: public accounts need their own machine "
    "(`house_compute_unavailable`) and, for MiniMax H3, operator approval "
    "(`minimax_h3_unavailable`)."
)
REFUSED_OWN = (
    "Your account may not start this work: public accounts need their own machine "
    "(`house_compute_unavailable`)."
)
FLIGHT_BUSY = "The flight changed under the request too many times; retry."
LIMITS_429 = "Too many jobs are running for your account. Wait for some to finish."
LIMITS_507 = "Your storage allowance is used up. Delete something first."
STORAGE_503 = "File storage is unavailable. Retry later."
FILE_404 = "Not found, not yours, or the file is not stored."

ROUTES: dict[tuple[str, str], RouteDoc] = {
    # Movies
    ("GET", "/studio/movies/capabilities"): RouteDoc(
        tag="Movies",
        response=MovieCapabilities,
        summary="Get movie capabilities",
        description=(
            "Reports what this server and your account can start for movies: takes "
            "(`generation_available`), opening images (`composition_available`), shot "
            "planning (`planner_available`, `director_available`) and dialogue "
            "(`dialogue_available`), plus your `account_tier`.\n\n"
            "Also returns the limits a client needs: shot duration range and step, "
            "`max_batch_size`, `max_dialogue_seconds`, voice clip length and "
            "`max_speakers_per_shot`."
        ),
    ),
    ("POST", "/studio/movies"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Create a movie",
        description=(
            "Creates an empty movie that you own, with the given `name`. Add a story, "
            "cast and shots next.\n\n"
            "Returns the movie at revision 1."
        ),
    ),
    ("GET", "/studio/movies"): RouteDoc(
        tag="Movies",
        response=list[MovieListItem],
        summary="List movies",
        description=(
            "Lists your movies. Scene flights are not included.\n\n"
            "`expires_at` is set when a movie is close to being deleted for inactivity "
            "and is `null` otherwise. A movie with a release never expires."
        ),
    ),
    ("GET", "/studio/movies/{id}"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Get a movie",
        description=(
            "Returns the movie at its current revision: story, cast, shots and takes, "
            "cut, soundtracks, assembly, build, releases and pending image jobs.\n\n"
            "Reading a movie does not settle finished jobs. Call "
            "`POST /studio/movies/{id}/reconcile` to poll."
        ),
        errors={404: MOVIE_404, 410: None},
    ),
    ("DELETE", "/studio/movies/{id}"): RouteDoc(
        tag="Movies",
        summary="Delete a movie",
        description=(
            "Permanently deletes the movie with every take, cut, release and file. "
            "`confirm_name` must repeat the movie's exact name. Running jobs are "
            "cancelled.\n\n"
            "Answers `204` with no body."
        ),
        errors={
            404: MOVIE_404,
            410: None,
            422: "`confirm_name` does not match the name (`deletion_not_confirmed`).",
            503: "File storage is unavailable. Some files may be gone already; delete again.",
        },
    ),
    ("GET", "/studio/movies/{id}/history"): RouteDoc(
        tag="Movies",
        response=list[Movie],
        summary="List movie revisions",
        description=(
            "Returns every saved revision of the movie as a full snapshot, oldest "
            "first."
        ),
        errors={404: MOVIE_404, 410: None},
    ),
    ("PUT", "/studio/movies/{id}/story"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Save the movie story and shots",
        description=(
            "Replaces the brief, style, shot list, `batch_size` and `compute_target` "
            "in one write. Send the full shot list each time; shots you leave out are "
            "removed, and the cut keeps only clips of the shots that remain.\n\n"
            "A shot whose fields change has its takes marked stale, and so does every "
            "shot that continues from it; running takes are cancelled. Changing the "
            "style counts as changing every shot. Changing `opening_prompt`, "
            "`continuation_of` or the style clears the shot's opening approval.\n\n"
            "Saving does not start generation."
        ),
        errors={
            404: MOVIE_404,
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or the request "
                "removes a shot that still has an active take or a pending opening image."
            ),
            410: None,
            422: (
                "Shot ids repeat, `continuation_of` names a missing shot or forms a cycle, "
                "a dialogue line names someone not in the cast or cannot fit one take, "
                "`batch_size` is over this server's limit, or the id is a scene flight."
            ),
        },
    ),
    ("PUT", "/studio/movies/{id}/characters"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Save the movie cast",
        description=(
            "Replaces the whole cast, at most 8 characters. Server fields such as "
            "`voice_seconds` and `portrait_asset_key` are ignored if you send them "
            "back.\n\n"
            "`voice_asset_key` must be an upload granted to this movie or a voice the "
            "cast already has. A new clip must be WAV or MP3, 2 to 15 seconds and at "
            "most 50 MB; it is stored under a fixed key. A character keeps its "
            "portrait only while `appearance` is unchanged.\n\n"
            "Takes of shots where a changed character speaks are marked stale."
        ),
        errors={
            403: (
                "`voice_asset_key` is not granted to this movie, or a public account "
                "sent a new voice clip (`house_compute_unavailable`)."
            ),
            404: "No such movie, or not yours, or the voice upload was never stored.",
            409: STALE,
            410: None,
            422: (
                "Character ids repeat, a character who still has lines was removed, or "
                "the voice clip is not valid WAV or MP3 of 2 to 15 seconds under 50 MB."
            ),
            503: "Audio processing or file storage is unavailable. Retry later.",
        },
    ),
    ("PUT", "/studio/movies/{id}/characters/{cid}/voice"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Attach a voice clip to a movie character",
        description=(
            "Sets one character's voice reference from a granted upload and keeps "
            "the rest of the cast as it is. Upload the clip first with "
            "`POST /studio/movies/{id}/uploads`.\n\n"
            "The clip must be WAV or MP3, 2 to 15 seconds and at most 50 MB. It is "
            "stored under a fixed key, and takes of shots where the character speaks "
            "are marked stale. Returns the movie."
        ),
        errors={
            403: (
                "The clip is not granted to this movie, or the account is public "
                "(`house_compute_unavailable`)."
            ),
            404: "No such movie or character, or not yours, or the upload was never stored.",
            409: STALE,
            410: None,
            422: "The clip is not valid WAV or MP3 of 2 to 15 seconds under 50 MB.",
            503: "Audio processing or file storage is unavailable. Retry later.",
        },
    ),
    ("POST", "/studio/movies/{id}/characters/{cid}/portrait"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Generate a movie character portrait",
        description=(
            "Queues an image job that draws a reference sheet of the character from "
            "its `appearance` and the movie style. The character needs an "
            "appearance.\n\n"
            "Returns the movie with the job in `compositions`. When it lands on a later "
            "reconcile it becomes the character's `portrait_asset_key`, unless the "
            "appearance changed meanwhile. Storyboard frames draw speakers from their "
            "portraits.\n\n"
            "Re-sending the same `request_id` starts nothing new; it reconciles."
        ),
        errors={
            403: REFUSED_OWN,
            404: "No such movie or character, or not yours.",
            409: STALE,
            410: None,
            422: (
                "The character has no appearance, the prompt was refused "
                "(`prompt_refused`), or `compute_target` is `my_machines` with no "
                "machine attached."
            ),
            429: LIMITS_429,
            503: "GPU generation or prompt screening is unavailable. Retry later.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/characters/{cid}/audition"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Audition a movie character voice",
        description=(
            "Queues one short MiniMax H3 take of the character speaking its "
            "`audition_line` in the voice its `voice` field describes, starting from "
            "its portrait when it has one.\n\n"
            "When the take lands on a later reconcile, its audio is checked against "
            "the voice clip limits and becomes the character's voice reference, unless "
            "`voice` changed meanwhile. Takes of shots where the character speaks are "
            "then marked stale.\n\n"
            "Re-sending the same `request_id` starts nothing new; it reconciles. Not "
            "available for public accounts."
        ),
        errors={
            403: REFUSED_OWN,
            404: "No such movie or character, or not yours.",
            409: STALE,
            410: None,
            422: (
                "The character has no voice description, the prompt was refused "
                "(`prompt_refused`), or `compute_target` is `my_machines` with no "
                "machine attached."
            ),
            429: LIMITS_429,
            503: "GPU generation, audio processing or prompt screening is unavailable.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/storyboard"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Generate a movie storyboard",
        description=(
            "Queues opening images for every shot. A chain of continuation shots "
            "shares one frame, composed from the opening direction of its first shot "
            "and the portraits of up to three characters who speak in the chain.\n\n"
            "Every shot needs an `opening_prompt`, and every speaking character needs a "
            "portrait. As each frame lands on a later reconcile it becomes the shot's "
            "opening image, if the shot's opening direction is unchanged. That clears "
            "the shot's approval and marks its takes stale.\n\n"
            "Re-sending the same `request_id` starts nothing new; it reconciles."
        ),
        errors={
            403: REFUSED_OWN,
            404: MOVIE_404,
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or another "
                "image job is still pending."
            ),
            410: None,
            422: (
                "There are no shots, a shot has no `opening_prompt`, a speaking character "
                "has no portrait, a prompt was refused (`prompt_refused`), or "
                "`compute_target` is `my_machines` with no machine attached."
            ),
            429: LIMITS_429,
            503: "GPU generation or prompt screening is unavailable. Retry later.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/plan"): RouteDoc(
        tag="Movies",
        response=MoviePlan,
        summary="Plan movie shots from a brief",
        description=(
            "Proposes shots for a `brief` and optional `style`. Nothing is saved: send "
            "the result through `PUT …/story` and `PUT …/characters` to keep it. "
            "`expected_revision` must still match the movie.\n\n"
            "When the director is configured the answer has `style`, `characters` and "
            "`shots` and follows `target_seconds`; otherwise it has `shots` only. The "
            "brief and style are screened first. Not available for public accounts."
        ),
        errors={
            403: "Shot planning is not available for public accounts (`director_unavailable`).",
            404: MOVIE_404,
            409: STALE,
            410: None,
            422: "The brief or style was refused (`prompt_refused`).",
            502: "The planner failed or returned an unusable plan. Retry or write the shots.",
            503: "Shot planning or prompt screening is not available here.",
        },
    ),
    ("POST", "/studio/movies/{id}/build"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Build the whole movie",
        description=(
            "Starts making the movie from its current plan. For each shot it uses the "
            "selected take, else the newest successful one, else one already running, "
            "and generates the rest in batches after the shots they continue from. "
            "When every shot has a take it saves a story-order cut and assembles it.\n\n"
            "The build moves forward each time the movie is reconciled; follow "
            "`build.status` and `build.error`. Editing the plan during a build fails "
            "it. Re-sending the same `request_id`, or building while a build runs, "
            "reconciles instead of starting again.\n\n"
            "The style, cast appearances and all shot text are screened before "
            "anything is saved."
        ),
        errors={
            403: REFUSED_ACCOUNT,
            404: MOVIE_404,
            409: STALE,
            410: None,
            422: (
                "There are no shots, a shot has no starting image or usable take, or a "
                "prompt was refused (`prompt_refused`)."
            ),
            503: "Prompt screening is unavailable. Retry later.",
        },
    ),
    ("POST", "/studio/movies/{id}/generate"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Generate movie shot takes",
        description=(
            "Queues one new MiniMax H3 take for each shot in `shot_ids`: at most "
            "`batch_size` shots, and at most `batch_size` active takes across the "
            "movie. A shot needs a starting image unless it continues another shot; a "
            "continuation take waits until the previous shot has a selected "
            "successful take. A dialogue shot ends on its opening image and needs a "
            "voice reference for every speaker.\n\n"
            "With `select_when_ready`, a take is selected when it succeeds if it is "
            "still the shot's newest. Re-sending the same `request_id` with the same "
            "shots returns the movie and does nothing.\n\n"
            "The take prompts are screened before anything is saved. Returns the "
            "movie after a reconcile."
        ),
        errors={
            403: REFUSED_ACCOUNT,
            404: "No such movie or shot, or not yours.",
            409: (
                "`expected_revision` is stale (`detail.current_revision`), a shot "
                "already has an active take, or `request_id` was used with other shots."
            ),
            410: None,
            422: (
                "The batch is too large or repeats a shot, it would exceed the active "
                "take budget, a shot lacks a starting image or voice reference, a "
                "prompt is too long or was refused (`prompt_refused`), or "
                "`compute_target` is `my_machines` with no machine attached."
            ),
            429: LIMITS_429,
            503: "GPU generation or prompt screening is unavailable. Retry later.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/reconcile"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Reconcile a movie",
        description=(
            "Brings the movie up to date with its jobs. It submits waiting takes whose "
            "inputs are ready, records finished and failed takes, lands opening "
            "images, portraits and auditions, runs a pending assembly and advances a "
            "build.\n\n"
            "Call it to poll. Returns the movie, saved as a new revision when anything "
            "changed, so re-read `revision` before your next write."
        ),
        errors={404: MOVIE_404, 409: BUSY, 410: None},
    ),
    ("POST", "/studio/movies/{id}/uploads"): RouteDoc(
        tag="Movies",
        response=MovieUploadGrant,
        summary="Grant a movie upload",
        description=(
            "Grants a new storage key on this movie for one file. Allowed "
            "`content_type` values: `image/png`, `image/jpeg`, `image/webp`, "
            "`video/mp4`, `audio/mpeg`, `audio/wav`.\n\n"
            "Returns `asset_key`, a signed `upload_url` and the new `revision`. PUT "
            "the bytes to `upload_url` with the same `Content-Type`; that request "
            "needs no Studio authorization. Public accounts also get `upload_headers`, "
            "which the PUT must send.\n\n"
            "The grant advances the movie revision. Pass `asset_key` to the route that "
            "uses the file."
        ),
        errors={
            404: MOVIE_404,
            409: BUSY,
            410: None,
            422: "The content type is not one of the allowed types.",
            429: LIMITS_429,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/opening"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Set a movie shot opening image",
        description=(
            "Sets the shot's opening image from a granted upload. The image is "
            "converted to PNG (at most 20 MiB and 25 megapixels) and stored under a "
            "fixed key.\n\n"
            "This clears the shot's opening approval and marks its takes stale, along "
            "with the takes of shots that continue from it."
        ),
        errors={
            403: "The image is not granted to this movie.",
            404: "No such movie or shot, or not yours, or the upload was never stored.",
            409: STALE,
            410: None,
            422: "The image cannot be read, or is over 20 MiB or 25 megapixels.",
            503: STORAGE_503,
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/import"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Import a clip as a movie shot take",
        description=(
            "Adds an uploaded MP4 (at most 256 MiB) to the shot as a successful take. "
            "It does not select the take. Importing the same bytes again returns the "
            "movie unchanged.\n\n"
            "The clip is processed on the service, so public accounts cannot import "
            "yet."
        ),
        errors={
            403: (
                "The clip is not granted to this movie, or the account is public "
                "(`house_compute_unavailable`)."
            ),
            404: "No such movie or shot, or not yours, or the upload was never stored.",
            409: STALE,
            410: None,
            422: "The clip is not a readable video, or is over 256 MiB.",
            503: "Video processing or file storage is unavailable. Retry later.",
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/approve"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Approve a movie shot opening image",
        description=(
            "Records that you approved the shot's opening image. The shot needs an "
            "opening image or must continue another shot.\n\n"
            "Approval is cleared when the opening image, its direction or the style "
            "changes."
        ),
        errors={
            404: "No such movie or shot, or not yours.",
            409: STALE,
            410: None,
            422: "The shot has no opening image and does not continue another shot.",
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/compose"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Generate a movie shot opening image",
        description=(
            "Queues an image job for the shot's opening image, from the style and "
            "`opening_prompt`. It starts from the current opening image, or a blank "
            "canvas, and up to four `reference_asset_keys` from granted uploads (three "
            "when this server composes on GPU machines).\n\n"
            "When the image lands on a later reconcile it replaces the opening image, "
            "unless the opening prompt, style or opening image changed meanwhile. That "
            "clears approval and marks the shot's takes stale.\n\n"
            "Re-sending the same `request_id` with the same references starts nothing "
            "new; it reconciles."
        ),
        errors={
            403: (
                "A reference is not granted to this movie, or your account may not use "
                "this compute (`house_compute_unavailable`)."
            ),
            404: "No such movie or shot, or not yours, or a reference was never stored.",
            409: (
                "`expected_revision` is stale (`detail.current_revision`), an opening "
                "image for this shot is already pending, or `request_id` was used for "
                "another shot or other references."
            ),
            410: None,
            422: (
                "The shot has no `opening_prompt`, the prompt is too long or was refused "
                "(`prompt_refused`), there are too many references, a reference image "
                "cannot be read, or `compute_target` is `my_machines` with no machine "
                "attached."
            ),
            429: LIMITS_429,
            503: "Image generation, file storage or prompt screening is unavailable.",
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/takes/{tid}/accept-continuity"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Accept continuity for a movie take",
        description=(
            "Marks a successful take as current after you checked that it still fits, "
            "clearing its stale flag. For a shot that continues another, it records "
            "the predecessor take you reviewed against, so that take must be selected, "
            "successful and current.\n\n"
            "Clears the assembly."
        ),
        errors={
            404: "No such movie, shot or take, or not yours.",
            409: STALE,
            410: None,
            422: (
                "The take has not succeeded, or the previous shot has no selected, "
                "successful, current take."
            ),
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/select"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Select a movie shot take",
        description=(
            "Selects `take_id` as the shot's take. It must have succeeded and not be "
            "stale.\n\n"
            "Choosing a different take makes later continuation shots stale: their "
            "takes are marked stale and running ones cancelled, except waiting takes "
            "that have not been pinned to a frame yet."
        ),
        errors={
            404: "No such movie, shot or take, or not yours.",
            409: STALE,
            410: None,
            422: "The take has not succeeded or is stale.",
        },
    ),
    ("POST", "/studio/movies/{id}/shots/{sid}/takes/{tid}/cancel"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Cancel a movie shot take",
        description=(
            "Cancels a take. A waiting take is cancelled at once; a running one is "
            "asked to stop and settles on reconcile. A finished take is left as it is "
            "and the movie is returned unchanged.\n\n"
            "Otherwise returns the movie after a reconcile."
        ),
        errors={
            404: "No such movie, shot or take, or not yours.",
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or the movie "
                "changed under the reconcile too many times."
            ),
            410: None,
        },
    ),
    ("GET", "/studio/movies/{id}/selected-cut"): RouteDoc(
        tag="Movies",
        response=list[movie_models.Clip],
        summary="Get the cut from the selected takes",
        description=(
            "Returns the cut Generate movie would save: one clip per shot's selected, "
            "succeeded and current take, in story order, with the build's default "
            "dissolves. Generated shots without dialogue are `mute`; imported clips "
            "keep their sound. Shots without such a take "
            "are left out, so a movie with none returns `[]`.\n\n"
            "Read-only. Review starts from this cut until you save your own with "
            "`PUT /studio/movies/{id}/cut`."
        ),
        errors={404: MOVIE_404, 410: None},
    ),
    ("PUT", "/studio/movies/{id}/cut"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Save the movie cut",
        description=(
            "Replaces the cut with 1 to 120 clips in order. Each clip names a "
            "successful, current take, with `in_seconds` before `out_seconds` and "
            "within the take. `transition_seconds` dissolves into the next clip, and "
            "`mute` replaces the take's own audio with silence.\n\n"
            "Changing the cut clears the assembly. Saving does not assemble."
        ),
        errors={
            404: "No such movie, shot or take, or not yours.",
            409: STALE,
            410: None,
            422: "A clip's take has not succeeded or is stale, or its trim is outside the take.",
        },
    ),
    ("PUT", "/studio/movies/{id}/soundtracks"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Save the movie soundtracks",
        description=(
            "Replaces the music and narration cues, at most 8. Each `asset_key` must be "
            "a granted upload or a cue the movie already has. The audio must be WAV or "
            "MP3 under 50 MB, with the trim inside its length; it is stored under a "
            "fixed key.\n\n"
            "Saving clears the assembly. An empty list removes every cue."
        ),
        errors={
            403: "An audio file is not granted to this movie.",
            404: MOVIE_404,
            409: STALE,
            410: None,
            422: "An audio file is missing, unreadable, over 50 MB, or shorter than its trim.",
            503: "Audio processing or file storage is unavailable. Retry later.",
        },
    ),
    ("POST", "/studio/movies/{id}/assemble"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Assemble the movie cut",
        description=(
            "Queues a render of the saved cut with its soundtracks. The render runs "
            "on reconcile; follow `assembly.status` and `assembly.error`.\n\n"
            "Re-sending the `request_id` of the current assembly starts nothing new; "
            "it reconciles."
        ),
        errors={
            403: REFUSED_OWN,
            404: "No such movie, or not yours, or the cut names a missing take.",
            409: STALE,
            410: None,
            422: (
                "There is no saved cut, the cut has stale takes, or `compute_target` is "
                "`my_machines` with no machine attached."
            ),
        },
    ),
    ("POST", "/studio/movies/{id}/release"): RouteDoc(
        tag="Movies",
        response=Movie,
        summary="Release a movie",
        description=(
            "Freezes the current assembly as a release called `name`. The assembly "
            "must have succeeded for the current cut and soundtracks.\n\n"
            "A release never changes. A movie with a release is not deleted for "
            "inactivity. Download it with "
            "`GET /studio/movies/{id}/releases/{release_id}/download`."
        ),
        errors={
            404: MOVIE_404,
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or a release "
                "with this name exists."
            ),
            410: None,
            422: "The current cut has not been assembled successfully.",
        },
    ),
    ("GET", "/studio/movies/{id}/releases/{release_id}/download"): RouteDoc(
        tag="Movies",
        file="The release MP4.",
        summary="Download a movie release",
        description=(
            "Returns the release's MP4 as an attachment named "
            "`movie-<release_id>.mp4`. Supports Range requests.\n\n"
            "Only the owner can download it. `proxy` is accepted and ignored."
        ),
        errors={404: "No such movie or release, or not yours.", 410: None, 416: None, 503: "File storage is unavailable right now. Retry later."},
    ),
    ("GET", "/studio/movies/{id}/assets/{key}"): RouteDoc(
        tag="Movies",
        file="The stored file.",
        summary="Get a movie file",
        description=(
            "Returns one of the movie's files by key: an upload, opening image, take "
            "video or frame, portrait, voice clip, soundtrack, assembly or release.\n\n"
            "Images, video and audio are served inline; other types download. Supports "
            "Range requests. `proxy` is accepted and ignored."
        ),
        errors={404: FILE_404, 410: None, 416: None, 503: "File storage is unavailable right now. Retry later."},
    ),
    # Scene flights
    ("GET", "/studio/scene-flights/capabilities"): RouteDoc(
        tag="Scene flights",
        response=SceneFlightCapabilities,
        summary="Get scene flight capabilities",
        description=(
            "Reports whether this server can generate scene-flight takes "
            "(`generation_available`), the accepted `image_types`, and the fixed "
            "recipe: canvas size, frame rate, steps, duration range and step, and "
            "`max_legs`."
        ),
    ),
    ("POST", "/studio/scene-flights"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Create a scene flight",
        description=(
            "Creates an empty scene flight that you own. Add location images and legs "
            "next.\n\n"
            "Returns the flight at revision 1."
        ),
    ),
    ("GET", "/studio/scene-flights"): RouteDoc(
        tag="Scene flights",
        response=list[SceneFlightSummary],
        summary="List scene flights",
        description=(
            "Lists your scene flights with `revision`, `leg_count` and "
            "`active_takes`.\n\n"
            "`expires_at` is set when a flight is close to being deleted for "
            "inactivity and is `null` otherwise."
        ),
    ),
    ("GET", "/studio/scene-flights/{id}"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Get a scene flight",
        description=(
            "Returns the flight with its images, legs, takes and chain.\n\n"
            "Reading the flight settles finished takes and the chain render, and saves "
            "a new revision when anything changed. Poll this route while takes run."
        ),
        errors={404: FLIGHT_404, 409: FLIGHT_BUSY, 410: None},
    ),
    ("DELETE", "/studio/scene-flights/{id}"): RouteDoc(
        tag="Scene flights",
        summary="Delete a scene flight",
        description=(
            "Permanently deletes the flight with every leg, take, image and chain. "
            "`confirm_name` must repeat the flight's exact name. Running jobs are "
            "cancelled.\n\n"
            "Answers `204` with no body."
        ),
        errors={
            404: FLIGHT_404,
            410: None,
            422: "`confirm_name` does not match the name (`deletion_not_confirmed`).",
            503: "File storage is unavailable. Some files may be gone already; delete again.",
        },
    ),
    ("POST", "/studio/scene-flights/{id}/images"): RouteDoc(
        tag="Scene flights",
        response=SceneFlightImage,
        summary="Add a scene flight image",
        description=(
            "Adds an origin or destination image, sent as base64 in `content_base64` "
            "with its `content_type` (PNG, JPEG or WebP, at most 20 MiB and 25 "
            "megapixels). It is converted to PNG with its EXIF orientation applied.\n\n"
            "Returns `image_key`, `width`, `height`, `sha256` and the flight's "
            "`revision`. The same picture always gets the same key; adding it again "
            "changes nothing."
        ),
        errors={
            404: FLIGHT_404,
            409: FLIGHT_BUSY,
            410: None,
            422: (
                "The base64 is invalid, the type is not PNG, JPEG or WebP, or the image "
                "is empty, unreadable, over 20 MiB or over 25 megapixels."
            ),
            429: LIMITS_429,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("PUT", "/studio/scene-flights/{id}/legs"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Save the scene flight legs",
        description=(
            "Replaces the legs, in order, at most 24. Each leg names an origin and a "
            "destination image already added to this flight, which must differ, and "
            "either a camera `prompt` or both `origin_name` and `destination_name` to "
            "compose one.\n\n"
            "A leg whose prompt, duration, images or negative prompt changed has its "
            "takes marked stale, running ones cancelled, and its selection cleared. "
            "Changing or removing legs clears the chain. Saving does not start "
            "generation."
        ),
        errors={
            403: "A leg image was not added to this flight.",
            404: FLIGHT_404,
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or the request "
                "removes a leg that still has an active take."
            ),
            410: None,
            422: (
                "Leg ids repeat, there are more than 24 legs, a leg's origin and "
                "destination are the same image, or its prompt is too long."
            ),
        },
    ),
    ("POST", "/studio/scene-flights/{id}/legs/{leg_id}/takes"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Generate a scene flight take",
        description=(
            "Queues one take of the leg, with the origin pinned as its first frame "
            "and the destination as its last. Use a new `request_id` for each take; "
            "another one on the same leg is a re-roll. `seed` is optional.\n\n"
            "A leg can have one active take, and a flight at most three. The newest "
            "take is selected when it succeeds. Re-sending the same `request_id` "
            "returns the flight and does nothing.\n\n"
            "The prompt is screened before anything is saved."
        ),
        errors={
            403: REFUSED_ACCOUNT,
            404: "No such scene flight or leg, or not yours.",
            409: (
                "`expected_revision` is stale (`detail.current_revision`), the leg "
                "already has an active take, or `request_id` was used for another leg."
            ),
            410: None,
            422: (
                "The flight already has three active takes, or the prompt is too long "
                "or was refused (`prompt_refused`)."
            ),
            429: LIMITS_429,
            503: (
                "This server cannot pin a last frame, or GPU generation or prompt "
                "screening is unavailable."
            ),
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/scene-flights/{id}/legs/{leg_id}/select"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Select a scene flight take",
        description=(
            "Selects `take_id` as the leg's take. It must have succeeded and not be "
            "stale. The chain uses each leg's selected take."
        ),
        errors={
            404: "No such scene flight, leg or take, or not yours.",
            409: STALE,
            410: None,
            422: "The take has not succeeded or is stale.",
        },
    ),
    ("POST", "/studio/scene-flights/{id}/legs/{leg_id}/takes/{take_id}/cancel"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Cancel a scene flight take",
        description=(
            "Cancels a take. A waiting take is cancelled at once; a running one is "
            "asked to stop and settles when the flight is next read. A finished take "
            "is left as it is.\n\n"
            "Returns the flight."
        ),
        errors={
            404: "No such scene flight, leg or take, or not yours.",
            409: (
                "`expected_revision` is stale (`detail.current_revision`), or the flight "
                "changed under the request too many times."
            ),
            410: None,
        },
    ),
    ("GET", "/studio/scene-flights/{id}/legs/{leg_id}/takes/{take_id}/video"): RouteDoc(
        tag="Scene flights",
        file="The take MP4.",
        summary="Download a scene flight take video",
        description=(
            "Returns the take's MP4 once it has succeeded. With `download=true` it is "
            "an attachment named `flight-<flight_id>-<leg_id>-<take prefix>.mp4`; "
            "otherwise it plays inline.\n\n"
            "Supports Range requests."
        ),
        errors={
            404: (
                "No such scene flight, leg or take, or not yours, or the take has no "
                "video yet."
            ),
            410: None,
            416: None,
            503: "File storage is unavailable right now. Retry later.",
        },
    ),
    ("POST", "/studio/scene-flights/{id}/chain"): RouteDoc(
        tag="Scene flights",
        response=SceneFlight,
        summary="Chain the scene flight legs",
        description=(
            "Joins every leg's selected take in leg order, with hard cuts. Each leg "
            "must start on the image the previous leg lands on, and every leg needs a "
            "selected successful take.\n\n"
            "Saves the cut, queues the render and returns the flight; reading the "
            "flight settles the render. Re-sending the `request_id` of the current "
            "chain starts nothing new."
        ),
        errors={
            403: REFUSED_OWN,
            404: FLIGHT_404,
            409: STALE,
            410: None,
            422: (
                "There are no legs, a leg does not start where the previous one lands, "
                "or a leg has no selected successful take."
            ),
        },
    ),
    ("GET", "/studio/scene-flights/{id}/chain/video"): RouteDoc(
        tag="Scene flights",
        file="The chained MP4.",
        summary="Download the scene flight chain video",
        description=(
            "Returns the chained MP4 once the chain has succeeded. With "
            "`download=true` it is an attachment named `flight-<flight_id>-chain.mp4`; "
            "otherwise it plays inline.\n\n"
            "Supports Range requests."
        ),
        errors={
            404: "No such scene flight, or not yours, or the chain has not succeeded.",
            410: None,
            416: None,
            503: "File storage is unavailable right now. Retry later.",
        },
    ),
    ("GET", "/studio/scene-flights/{id}/assets/{key}"): RouteDoc(
        tag="Scene flights",
        file="The stored file.",
        summary="Get a scene flight file",
        description=(
            "Returns one of the flight's files by key: an added image, a pinned first "
            "or last frame, a take's last frame or video, or the chain.\n\n"
            "Images and video are served inline. Supports Range requests."
        ),
        errors={404: FILE_404, 410: None, 416: None, 503: "File storage is unavailable right now. Retry later."},
    ),
}
