"""Reference entries for motions routes."""

from typing import Any

from pydantic import BaseModel, Field

from magic_video_editor.api_reference.model import RouteDoc


class TakeFrame(BaseModel):
    index: int = Field(description="Zero-based index in the stored source.")
    timestamp_ms: float
    image: str = Field(description="A PNG data URL, at most 512 px on its long side.")
    pose: list[float] = Field(description="A coarse pose signature used for loop matching.")
    edge_contact: bool | None = Field(description="Whether the figure touches the frame edge; null when it cannot be measured.")
    foreground_bbox: list[int] | None = Field(description="`[left, top, right, bottom]` of the figure, in source pixels.")
    central_vertical_bounds: list[int] | None = Field(description="`[top, bottom]` of the figure's central band.")


class TakeFrames(BaseModel):
    analysis_version: str
    candidate_id: str
    source_sha256: str
    frame_count: int = Field(description="Frames in the whole take; `frames` holds only the requested page.")
    source_chroma_alpha: bool = Field(description="Whether the take's alpha came from removing a green backdrop.")
    width: int
    height: int
    duration_ms: float
    fps: float
    frame_rate: str | None = Field(description="The decoded stream's frame rate as a fraction, for video.")
    time_base: str | None
    index_space: str = Field(description="Always `stored_source_frames`.")
    registered_pivot: dict[str, float] | None = Field(default=None, description="`x` and `y` of the take's registered pivot, when it has one.")
    frames: list[TakeFrame]


class LoopSuggestion(BaseModel):
    analysis_version: str
    mode: str = Field(description="`idle`, `walk` or `attack`.")
    candidate_id: str
    source_sha256: str
    status: str = Field(description="`complete`, or `incomplete` when no clean cycle was found or the figure is clipped.")
    start: int | None
    end: int | None = Field(description="The matching end frame, excluded from `ordered_frames`.")
    ordered_frames: list[int] = Field(description="Source frame indices to save, in order.")
    frame_duration_ms: int | None
    loop_policy: str = Field(description="`loop`, or `once` for an attack.")
    edge_clipped_frames: list[int]
    warnings: list[str]
    visual_review_required: bool
    source_interval_ms: float | None = None
    source_frame_rate: str | None = None
    source_time_base: str | None = None
    foreground_bottom_delta_px: int | None = None


class BodyMeasurement(BaseModel):
    height: float
    hip_x: float
    feet_y: float
    top: float


class BodyCalibration(BaseModel):
    normalization: dict[str, Any] = Field(description="Pass to the frame revision route unchanged, apart from padding or edge cleanup you choose.")
    source_measurement: BodyMeasurement
    reference_measurement: BodyMeasurement
    source_candidate_id: str
    reference_candidate_id: str
    source_sha256: str
    reference_sha256: str
    source_frame: int
    reference_frame: int
    method: str
    visual_review_required: bool
    warnings: list[str]

P = "/studio/projects/{project_id}"
M = P + "/motions/{motion_id}"
T = M + "/candidates/{candidate_id}"

UNREADABLE = "The project's latest revision cannot be read (`manifest_unreadable`). Call the project repair route, then retry."
UNREADABLE_OR = "or the project's latest revision cannot be read (`manifest_unreadable`)."
DOWNLOAD_PATH = (
    "Returns `key` and a download path in `upload_url`, not the bytes. GET that path "
    "with the same key or session. The file is built on the first request and reused "
    "after that."
)
TAKE_422 = "The motion or take is not in this project, or its file cannot be read or decoded."
TA = P + "/turnarounds"
VD = P + "/view-drafts"


class TurnaroundOperation(BaseModel):
    job_id: str
    operation: str = Field(description="Always `generate_turnaround`.")
    status: str
    turnaround_id: str
    motion_id: None = Field(description="Always null: a turnaround is not an animation.")


class TurnaroundStarted(BaseModel):
    turnaround_id: str = Field(description="Stable identifier for the take; use it for every later call.")
    operation: TurnaroundOperation


class TurnaroundPickSet(BaseModel):
    pick_revision: int
    picks: dict[str, dict[str, Any]] = Field(description="Per heading: `frame_index` and any confirmed override.")
    findings: list[dict[str, Any]] = Field(description="Overrides this revision used, with their distance in frames (null for an out-of-order pick whose neighbours leave no frame between them).")
    saved_by: str
    saved_at: str


class Turnaround(BaseModel):
    turnaround_id: str
    source_view_id: str
    source_heading: str = Field(description="Always `S` for now.")
    source_reference: dict[str, Any] = Field(description="The reference the turn started from.")
    source_superseded: bool = Field(description="Whether that view's reference has since been replaced or cleared.")
    source_backdrop: str = Field(description="`transparent` or `chroma`.")
    equipment: list[dict[str, str]]
    prompt_version: str
    prompt: str
    prompt_sha256: str
    prepared_frame: dict[str, Any] = Field(description="Key, hash, size and preparation of the frame sent to both template slots.")
    requested_revision: int
    created_at: str | None
    job_id: str | None
    status: str | None
    settled: bool
    frame_count: int | None
    video: dict[str, Any] | None = Field(description="The settled video's key, hash and execution provenance.")
    cards_version: str
    placement_cards: dict[str, list[str]] = Field(description=(
        "Per heading, what a correct picture shows: what the camera sees, then where each equipment row "
        "lands. Computed from the direction geometry, never hand-written."))
    synthesized_parts: dict[str, list[str]] = Field(description=(
        "Per heading, what that picture shows that the source never did. Those parts are synthesized."))
    pick_set: TurnaroundPickSet | None = Field(description="The saved picks, or null before the first save.")


class TurnaroundDetail(Turnaround):
    suggested_picks: dict[str, dict[str, Any]] | None = Field(description=(
        "Once settled: Studio's suggested `frame` per heading, with its `reason`. The source heading is the "
        "first frame. Proposals only; the person owns every pick."))
    suggestions_error: str | None = Field(default=None, description="Why suggestions could not be measured, if so.")


class ViewDraft(BaseModel):
    draft_id: str
    turnaround_id: str
    heading: str
    pick_revision: int = Field(description="The saved pick set the draft was made from.")
    frame_index: int
    suggested_frame: int | None = Field(description="Studio's suggestion the pick was checked against, or null when "
                                                    "there were no suggestions.")
    picks_version: str = Field(description="The frame picker that made `suggested_frame`.")
    overrides: list[dict[str, Any]] = Field(description="Pick overrides confirmed for this heading, if any.")
    source_reference: dict[str, Any]
    video_sha256: str
    raw_frame_sha256: str
    processing: str = Field(description="The one keying and framing path every draft takes.")
    image: dict[str, Any] = Field(description="The processed 1024 PNG: key, hash and size.")
    image_path: str = Field(description="Where to fetch the image.")
    placement_card: list[str] = Field(description="The card the check is made against, as stored with the draft.")
    synthesized_parts: list[str] = Field(description="What this direction shows that the source never did.")
    check: dict[str, Any] | None = Field(description="The latest check receipt, or null.")
    checked: bool = Field(description="Whether the latest check ticked facing, equipment and identity.")
    discarded: bool
    bound_view_ids: list[str] = Field(description="Directions this draft is bound to.")
    stale_reason: str | None = Field(description="Why the draft no longer matches its turnaround, saved picks "
                                                 "or source direction, or null.")
    bindable: bool = Field(description="Checked, not discarded, not yet bound and not stale.")


class ViewDraftCreated(BaseModel):
    draft_id: str
    draft: ViewDraft


class ViewDraftList(BaseModel):
    drafts: list[ViewDraft]


class TurnaroundList(BaseModel):
    turnarounds: list[Turnaround]


ROUTES: dict[tuple[str, str], RouteDoc] = {
    # Motions
    ("POST", P + "/motions"): RouteDoc(
        tag="Motions",
        summary="Add an animation",
        description=(
            "Adds one animation to the project. A preset animation needs `preset`; a "
            "custom one needs `prompt`. This does not start generation.\n\n"
            "Sending an existing `motion_id` replaces that animation and drops its takes. "
            "Use the batch route to have duplicates refused, or PATCH to edit one.\n\n"
            "Clears the set's quality assessment and approval. A project declared through "
            "versioned setup also needs the header `X-Studio-Setup-Version: 1`."
        ),
        errors={
            404: None,
            409: "A stale `expected_revision`, or `unsupported_setup_version` when the header is missing.",
            410: None,
            422: "The definition is invalid: an undeclared view, a removed `motion_id` reused, or conflicting references.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", P + "/motions/batch"): RouteDoc(
        tag="Motions",
        summary="Add several animations",
        description=(
            "Adds up to 32 animations in one revision. This does not start generation.\n\n"
            "All or nothing: if any `motion_id` already exists or repeats in the batch, "
            "nothing is added and `detail.motion_ids` names them."
        ),
        errors={
            404: None,
            409: "A stale `expected_revision`, or `motion_id`s already used (`detail.motion_ids`).",
            410: None,
            422: "A definition is invalid: an undeclared view, a removed `motion_id` reused, or conflicting references.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("PATCH", M): RouteDoc(
        tag="Motions",
        summary="Update an animation",
        description=(
            "Replaces the animation's definition: name, action or prompt, references, "
            "style and script. Its takes and selected take are kept.\n\n"
            "Send the whole definition, with `motion_id` equal to the one in the path. "
            "Bind a reference uploaded through the reference upload route by listing its "
            "`key` and `sha256` in `references`."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: "`motion_id` in the body differs from the path, the animation is not in the project, or the definition is invalid.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", M + "/references/uploads"): RouteDoc(
        tag="Motions",
        summary="Start an animation reference upload",
        description=(
            "Returns a signed `upload_url` and a `key` for one reference image (PNG, JPEG "
            "or WebP). PUT the file there, sending `upload_headers` unchanged.\n\n"
            "Nothing is bound yet. Add the `key` and the file's `sha256` to the "
            "animation's `references` with PATCH. The upload space belongs to the "
            "project, so `motion_id` in the path is not checked."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: "`content_type` is not PNG, JPEG or WebP (`subject_reference_content_type_invalid`).",
            503: UNREADABLE,
            507: None,
        },
    ),
    ("POST", P + "/motions/generate-batch"): RouteDoc(
        tag="Motions",
        summary="Generate takes for several animations",
        description=(
            "Queues one generation job per listed animation. Needs the approved current "
            "subject. Each finished job adds a take; see the project's operations and "
            "activity routes to follow it.\n\n"
            "Partial success: read both `queued` and `failed`. An animation in `failed` "
            "(unknown id, over the job cap, refused prompt, dispatch error) starts nothing; "
            "fix it and send it again.\n\n"
            "Re-sending the same `request_id` for unchanged animations returns the "
            "original jobs instead of starting new ones. `actions` saves edited "
            "\"what happens\" lines first, in one revision. `shared_starting_pose` starts "
            "every animation from the approved opening pose."
        ),
        errors={
            403: "This account may not start this work, for example `house_compute_unavailable` or `minimax_h3_unavailable`.",
            404: None,
            409: None,
            410: None,
            422: (
                "The subject is not approved, `shared_starting_pose` has no approved opening "
                "pose or includes view animations, or a line in `actions` was refused "
                "(`prompt_refused`)."
            ),
            429: None,
            500: None,
            503: "Animation generation is not available, prompt screening is unavailable, " + UNREADABLE_OR,
            507: None,
        },
    ),
    ("POST", M + "/generate"): RouteDoc(
        tag="Motions",
        summary="Generate a take for an animation",
        description=(
            "Queues one generation job for the animation and returns it. Needs the "
            "approved current subject. The finished job adds a take; it is not selected.\n\n"
            "Re-sending the same `request_id` for an unchanged animation returns the "
            "original job instead of starting another. `action` saves an edited "
            "\"what happens\" line first. Optional `settings` tunes the generation."
        ),
        errors={
            403: "This account may not start this work, for example `house_compute_unavailable` or `minimax_h3_unavailable`.",
            404: None,
            409: None,
            410: None,
            422: (
                "The subject is not approved, the animation is not in the project, the "
                "prompt was refused (`prompt_refused`), a view reference is missing or "
                "changed, no Higgsfield key is saved, or the `request_id` was abandoned."
            ),
            429: None,
            500: None,
            503: (
                "Animation generation or Higgsfield is not available here, the chosen "
                "machine could not accept the recipe, prompt screening is unavailable, "
                + UNREADABLE_OR
            ),
            507: None,
        },
    ),
    ("POST", M + "/operations/{job_id}/cancel"): RouteDoc(
        tag="Motions",
        summary="Cancel an animation job",
        description=(
            "Cancels a queued or running generation job and returns it. A cancelled job "
            "never adds a take, even if its render finishes."
        ),
        errors={
            404: "No such job for this animation, or the project is not found or not yours.",
            409: "The job already finished, failed or was cancelled. `detail` names its state.",
            410: None,
            503: UNREADABLE,
        },
    ),
    ("POST", M + "/operations/{job_id}/settle"): RouteDoc(
        tag="Motions",
        summary="Settle a finished animation job",
        description=(
            "Adds a completed job's output to the animation as a new take and returns the "
            "project. The take is not selected. A green-screen take also gets an alpha "
            "copy when that succeeds.\n\n"
            "Settling a job whose take is already there returns the project unchanged. "
            "Adding a take clears the set's quality assessment and approval."
        ),
        errors={
            404: "No such job for this animation, or the project is not found or not yours.",
            409: (
                "A stale `expected_revision`, a job that has not completed, an animation "
                "that no longer exists, or output that does not match its request."
            ),
            410: None,
            422: "The job's output is missing, too large or cannot be decoded. `detail.code` says which.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", T + "/select"): RouteDoc(
        tag="Motions",
        summary="Choose the take for an animation",
        description=(
            "Makes this take the animation's selected take. Every required animation "
            "needs one before the set can be approved.\n\n"
            "Clears the set's quality assessment and approval."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: "The animation or take is not in this project.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", M + "/candidates/uploads"): RouteDoc(
        tag="Motions",
        summary="Start a take upload",
        description=(
            "Returns a signed `upload_url` and a `key` for a clip or sprite strip made "
            "elsewhere (MP4, WebM or PNG). PUT the file there, sending `upload_headers` "
            "unchanged, then add it with the settle-upload route."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: (
                "The animation is not in the project, or `content_type` is not MP4, WebM "
                "or PNG (`motion_upload_content_type_invalid`)."
            ),
            503: UNREADABLE,
            507: None,
        },
    ),
    ("POST", M + "/candidates/settle-upload"): RouteDoc(
        tag="Motions",
        summary="Add an uploaded take",
        description=(
            "Measures an uploaded file and adds it to the animation as a take with "
            "`source` `upload`. The take is not selected. A PNG strip needs `frame_count`.\n\n"
            "`asset_key` must be a key from this project's take upload route. Settling the "
            "same bytes again returns the project unchanged. A refused settle keeps the "
            "upload, so you can retry with a corrected `frame_count`."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: (
                "The animation is not in the project, the key is not from this project's "
                "uploads, a PNG has no `frame_count`, or the file is missing or cannot be "
                "measured. `detail.code` says which."
            ),
            500: None,
            503: UNREADABLE,
        },
    ),
    # Frames
    ("GET", T + "/frames"): RouteDoc(
        tag="Frames",
        summary="List a take's frames",
        description=(
            "Decodes the take's stored frames and returns one page of them. Each frame "
            "has its source `index`, `timestamp_ms`, a PNG preview up to 512 pixels as "
            "a data URL in `image`, `foreground_bbox` and `edge_contact`.\n\n"
            "Page with `start`, `limit` (at most 32) and `stride`. The top level also "
            "gives `frame_count`, `width`, `height`, `duration_ms` and `fps`. Takes of "
            "more than 512 frames cannot be read this way. Nothing is saved to the project."
        ),
        errors={
            404: None,
            410: None,
            422: TAKE_422 + " Also more than 512 frames.",
            503: UNREADABLE,
        },
        response=TakeFrames,
    ),
    ("POST", T + "/loop-suggestion"): RouteDoc(
        tag="Frames",
        summary="Suggest a loop for a take",
        description=(
            "Looks for a clean idle, walk or attack cycle in the take and suggests "
            "`target_frame_count` frames (8, 12 or 16) from it. Returns `ordered_frames`, "
            "`frame_duration_ms`, `loop_policy`, `status` and `warnings`.\n\n"
            "A suggestion is not a visual check: `status` is `incomplete` when no clean "
            "cycle was found or the figure touches the frame edge. Nothing is saved; send "
            "the frames to the frame revision route to keep them."
        ),
        errors={
            404: None,
            410: None,
            422: TAKE_422,
            503: UNREADABLE,
        },
        response=LoopSuggestion,
    ),
    ("POST", T + "/body-calibration"): RouteDoc(
        tag="Frames",
        summary="Measure body scale against a reference take",
        description=(
            "Measures the body in one frame of this take and one frame of a reference "
            "take, and returns a `normalization` that scales and anchors this take to "
            "match. Pass it to the frame revision route to apply it to every frame.\n\n"
            "Both animations must be in the same view, and both takes need transparent "
            "frames, so derive alpha first. `reference_motion_id` defaults to `walk`. "
            "Nothing is saved."
        ),
        errors={
            404: None,
            410: None,
            422: (
                "A motion or take is missing, the views differ, a frame number is out of "
                "range, the frames have no transparency, or the body could not be measured."
            ),
            503: UNREADABLE,
        },
        response=BodyCalibration,
    ),
    ("POST", T + "/derive-alpha"): RouteDoc(
        tag="Frames",
        summary="Derive alpha for a take",
        description=(
            "Keys out the green backdrop and adds the result as a new take with "
            "transparency. The new take is selected only if the source take was the "
            "selected one.\n\n"
            "Asking again for the same source take returns the project unchanged. Adding "
            "a take clears the set's quality assessment and approval."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: (
                "The animation or take is not in this project, the take already has alpha "
                "(`motion_already_has_alpha`), or keying failed. `detail.code` says which."
            ),
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", M + "/revisions"): RouteDoc(
        tag="Frames",
        summary="Save a frame revision of a take",
        description=(
            "Saves an edited version of `source_candidate_id` as a new take and selects "
            "it. The source take is not changed.\n\n"
            "`ordered_frames` lists source frame indices in playback order, each a number "
            "or `{index, hold}`. `frame_duration_ms`, `loop_policy` and `flip_horizontal` "
            "set timing and facing. With `target_frame_count` and `cell_size` together, a "
            "new resampled sheet is rendered, and `normalization` (from body calibration) "
            "applies one fixed scale and anchor to every frame.\n\n"
            "Clears the set's quality assessment and approval."
        ),
        errors={
            404: None,
            409: None,
            410: None,
            422: (
                "The animation or source take is missing or unmeasured, only one of "
                "`target_frame_count` and `cell_size` is sent, the normalization reference "
                "is in another view, or rendering failed."
            ),
            500: None,
            503: UNREADABLE,
        },
    ),
    ("GET", T + "/sheet.png"): RouteDoc(
        tag="Frames",
        summary="Get a take's sprite sheet",
        description=(
            "A PNG strip of the take's frames in its saved order, holds and facing, "
            "aligned to the registered pivot when there is one.\n\n" + DOWNLOAD_PATH
        ),
        errors={404: None, 410: None, 422: TAKE_422, 503: UNREADABLE},
    ),
    ("GET", T + "/preview.webm"): RouteDoc(
        tag="Frames",
        summary="Get a take's WebM preview",
        description=(
            "A looping WebM clip of the take at its own frame timing.\n\n" + DOWNLOAD_PATH
        ),
        errors={404: None, 410: None, 422: TAKE_422, 503: UNREADABLE},
    ),
    ("GET", T + "/preview.gif"): RouteDoc(
        tag="Frames",
        summary="Get a take's GIF preview",
        description=(
            "A looping GIF of the take at its own frame timing.\n\n" + DOWNLOAD_PATH
        ),
        errors={404: None, 410: None, 422: TAKE_422, 503: UNREADABLE},
    ),
    # Releases and exports
    ("POST", P + "/quality/set"): RouteDoc(
        tag="Releases and exports",
        summary="Assess set quality",
        description=(
            "Checks the selected takes as a set and saves the findings on the project as "
            "`set_assessment`. Findings are warnings, not blockers.\n\n"
            "Approval needs a current assessment, and any later change to the set "
            "clears it. Running this also clears an existing set approval."
        ),
        errors={404: None, 409: None, 410: None, 500: None, 503: UNREADABLE},
    ),
    ("POST", P + "/set/approve"): RouteDoc(
        tag="Releases and exports",
        summary="Approve the set",
        description=(
            "Approves the current set of selected takes. Needs a current quality "
            "assessment and a selected take for every required animation.\n\n"
            "Without `waived_finding_ids`, every finding present is recorded as "
            "acknowledged. Send your own list, with `waiver_reason`, to record exactly "
            "what you waived; each id must match a current finding."
        ),
        errors={
            404: None,
            409: (
                "A stale `expected_revision`, no current assessment, a required animation "
                "with no selected take, or waivers that do not match the findings."
            ),
            410: None,
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", P + "/releases"): RouteDoc(
        tag="Releases and exports",
        summary="Create a release",
        description=(
            "Freezes the approved set: the subject revision, the selected take of each "
            "animation, the quality policy versions and the approval. A release never "
            "changes afterwards, and a project with a release is not deleted for "
            "inactivity.\n\n"
            "Needs a set approval that matches the current set. Release names are unique "
            "within a project. When the project is bound to an external target that is "
            "configured here, the release is also delivered there; the outcome is in "
            "`deliveries` and a failed delivery does not fail the release."
        ),
        errors={
            404: None,
            409: "A stale `expected_revision`, no current set approval, or a name already used.",
            410: None,
            500: None,
            503: UNREADABLE,
        },
    ),
    ("GET", P + "/releases"): RouteDoc(
        tag="Releases and exports",
        summary="List a project's releases",
        description="Every release of the project, each with its delivery attempts in `deliveries`.",
        errors={404: None, 410: None, 503: UNREADABLE},
    ),
    ("POST", "/studio/releases/{release_id}/deliveries/{target}"): RouteDoc(
        tag="Releases and exports",
        summary="Retry a release delivery",
        description=(
            "Exports the release for `target` and delivers it again, then returns the "
            "latest delivery record. A failed delivery is still a success response: "
            "read `status`, `error` and `retryable`."
        ),
        errors={
            404: None,
            409: "Delivery to this target is not configured here.",
            500: None,
            503: UNREADABLE,
        },
    ),
    ("POST", "/studio/releases/{release_id}/exports"): RouteDoc(
        tag="Releases and exports",
        summary="Create an export",
        description=(
            "Packages the release as a file for `profile` and returns the export record. "
            "`png-json` (the default) is sprite sheets plus a `manifest.json`; an "
            "installed target's name is also accepted (see `GET /studio/targets`).\n\n"
            "`expected_revision` must equal the release's `source_revision`. Exports are "
            "immutable: asking again for the same profile returns the same record."
        ),
        errors={
            404: None,
            409: (
                "`expected_revision` is not the release's `source_revision`, or the stored "
                "release or export failed integrity checks (`release_integrity_error`, "
                "`immutable_export_collision`)."
            ),
            422: "An unknown `profile`, or a release asset the profile cannot package. `detail.code` says which.",
            503: "Export storage is unavailable (`export_storage_unavailable`), " + UNREADABLE_OR,
        },
    ),
    ("GET", "/studio/releases/{release_id}/exports"): RouteDoc(
        tag="Releases and exports",
        summary="List a release's exports",
        description="Every export made from the release, newest first.",
        errors={404: None, 503: UNREADABLE},
    ),
    ("GET", "/studio/exports/{export_id}"): RouteDoc(
        tag="Releases and exports",
        summary="Get an export",
        description=(
            "The export record: profile, `artifact_key`, `sha256` and `size_bytes`. The "
            "stored file is checked against `sha256` on every read."
        ),
        errors={
            404: None,
            409: "The stored file failed its integrity check (`release_integrity_error`, `immutable_export_collision`).",
            503: "Export storage is unavailable (`export_storage_unavailable`), " + UNREADABLE_OR,
        },
    ),
    ("GET", "/studio/exports/{export_id}/download"): RouteDoc(
        tag="Releases and exports",
        summary="Get an export download path",
        description=(
            "Returns `key` and a download path in `upload_url`, not the bytes. The path is "
            "the export's file route; GET it with the same key or session and check the "
            "bytes against the export's `sha256`."
        ),
        errors={
            404: "Not found, not yours, or the file is not stored.",
            409: "The stored file failed its integrity check (`release_integrity_error`, `immutable_export_collision`).",
            503: "Export storage is unavailable (`export_storage_unavailable`), " + UNREADABLE_OR,
        },
    ),
    ("GET", "/studio/exports/{export_id}/file"): RouteDoc(
        tag="Releases and exports",
        summary="Download an export file",
        description=(
            "Streams the export file, usually a zip. Only its owner can fetch it. `Range` "
            "requests are supported."
        ),
        errors={
            404: "Not found, not yours, or the file is not stored.",
            409: "The stored file failed its integrity check (`release_integrity_error`, `immutable_export_collision`).",
            416: None,
            503: "Export storage is unavailable (`export_storage_unavailable`), " + UNREADABLE_OR,
        },
        file="The export package, usually a zip.",
    ),
    # Turnarounds
    ("POST", TA): RouteDoc(
        tag="Turnarounds",
        summary="Start a turnaround",
        description=(
            "Turns the front (`S`) or front three-quarter (`SE`, `SW`) reference of `source_view_id` in place through one "
            "full circle, as one closed-loop video on a machine that reports last-frame "
            "support. `source_revision_id` must be that view's current bound reference, with "
            "a transparent or green-screen background.\n\n"
            "`equipment` lists up to four items to keep attached, each with `item`, "
            "`attachment` (hand, shoulder, hip, back, other) and `source_picture_side` (left, "
            "right, or `unsided` for back and other only). Repeating a `request_id` returns "
            "the same turnaround. The take is not an animation: it never appears among "
            "motions, exports or completeness.\n\n"
            "Check eligibility first: `GET /studio/generation-config` must list an available "
            "machine whose `features.last_frame` is true. Then follow the turnaround with "
            "`POST /studio/projects/{project_id}/activity/reconcile` and a read, as for any "
            "machine work."
        ),
        errors={
            404: None,
            409: "A stale `expected_revision`.",
            422: "The source revision is not the view's current reference, or an equipment row is invalid.",
            503: "No machine reports last-frame support, the source is not front or front three-quarter (S, SE, SW) or has scenery behind it, or the selected machine refused the job.",
        },
        response=TurnaroundStarted,
    ),
    ("GET", TA): RouteDoc(
        tag="Turnarounds",
        summary="List turnarounds",
        description=(
            "Every turnaround in the project, oldest first, with its state and provenance.\n\n"
            "Reading does not settle work. While a turnaround runs, call "
            "`POST /studio/projects/{project_id}/activity/reconcile` before each read: "
            "reconciling is what moves a finished job forward and attaches its video."
        ),
        errors={404: None},
        response=TurnaroundList,
    ),
    ("GET", TA + "/{turnaround_id}"): RouteDoc(
        tag="Turnarounds",
        summary="Read a turnaround",
        description=(
            "One turnaround: its source, prompt, the prepared frame sent to both template "
            "slots, its job state and, once settled, the video and its frame count.\n\n"
            "Reading does not settle work: poll with "
            "`POST /studio/projects/{project_id}/activity/reconcile` before each read until "
            "`settled` is true or the job has failed."
        ),
        errors={404: "The project, or the turnaround in it, does not exist."},
        response=TurnaroundDetail,
    ),
    ("POST", VD): RouteDoc(
        tag="Turnarounds",
        summary="Make a direction draft",
        description=(
            "Makes a draft for one `heading` from the frame the saved pick set (`pick_revision`) chose. The "
            "frame is keyed and framed to 1024 through one fixed path, and stored with the placement card "
            "it will be checked against. A draft is not a reference: it never counts toward completeness and "
            "nothing animates from it until it is checked and bound. Repeating a `request_id` returns the "
            "same draft. `standing: true` (the standing pass) is not available yet. If the picks are "
            "saved again while the draft is made, the draft is still recorded, with a `stale_reason`, "
            "and cannot be bound."
        ),
        errors={
            404: "The project or turnaround does not exist.",
            409: "A stale `expected_revision`.",
            422: "The take has not settled, the pick revision is not current, the heading is the source or "
                 "has no saved pick, the frame cannot be keyed, or the standing pass was requested.",
            503: "Draft storage is temporarily unavailable; try again.",
            507: "The account's storage is full. Every draft asset counts toward it.",
        },
        response=ViewDraftCreated,
    ),
    ("GET", VD): RouteDoc(
        tag="Turnarounds",
        summary="List direction drafts",
        description="Every draft in the project with its latest check, whether it is discarded or bound, and whether it can be bound.",
        errors={404: None},
        response=ViewDraftList,
    ),
    ("POST", VD + "/{draft_id}/check"): RouteDoc(
        tag="Turnarounds",
        summary="Check a direction draft",
        description=(
            "Records the person's check as a new, append-only receipt against the draft's stored placement "
            "card: `facing` correct, `equipment` where the card says, and `identity` (same face and outfit "
            "where the source shows them, synthesized parts plausible). Only a check that ticks all three "
            "makes the draft bindable. Agents record a check only with the person's explicit go-ahead."
        ),
        errors={404: "The project or draft does not exist.", 409: "A stale `expected_revision`.",
                422: "The draft was discarded.", 507: "The account's storage is full; each check receipt counts toward it."},
        response=ViewDraft,
    ),
    ("POST", VD + "/{draft_id}/discard"): RouteDoc(
        tag="Turnarounds",
        summary="Discard a direction draft",
        description=(
            "Marks the draft discarded (a project change, so it moves the revision) and writes a tombstone "
            "receipt. It can never be bound afterwards, even through a setup plan previewed before the "
            "discard. A draft that is a direction's reference cannot be discarded until that reference is "
            "replaced. Repeating a discard that was already committed returns the discarded draft, even "
            "with the original `expected_revision`. Discarding cannot be undone; make a new draft from "
            "the frame instead."
        ),
        errors={404: "The project or draft does not exist.", 409: "A stale `expected_revision`.",
                422: "The draft is a direction's reference."},
        response=ViewDraft,
    ),
    ("PUT", TA + "/{turnaround_id}/picks"): RouteDoc(
        tag="Turnarounds",
        summary="Save a turnaround's picks",
        description=(
            "Replaces the whole pick set: one `frame_index` for each of the seven headings other than the "
            "source, which is fixed at the loop's start. Send the `expected_pick_revision` you read (0 before "
            "the first save).\n\n"
            "Each pick must lie in its heading's region (between the neighbouring headings' suggestions, "
            "widened by a quarter of each gap) and the picks must run in turn order. A pick that breaks a "
            "rule needs `confirm_out_of_region` or `confirm_out_of_order` on that heading. An override counts "
            "only in the revision that carries it: replacing the set drops any you do not repeat."
        ),
        errors={
            404: "The project, or the turnaround in it, does not exist.",
            409: "The pick set changed since `expected_pick_revision`; `pick_revision` names the current one.",
            422: "The take has not settled, or a pick breaks a rule without its confirmation (`problems`).",
        },
        response=TurnaroundPickSet,
    ),
    ("GET", TA + "/{turnaround_id}/frames"): RouteDoc(
        tag="Turnarounds",
        summary="List a turnaround's frames",
        description=(
            "Decodes the settled turnaround video and returns one page of frames, paged "
            "like a take's frames with `start`, `limit` (at most 32) and `stride`."
        ),
        errors={404: None, 422: "The turnaround has not settled, or its video cannot be read or decoded."},
        response=TakeFrames,
    ),
    ("POST", TA + "/{turnaround_id}/cancel"): RouteDoc(
        tag="Turnarounds",
        summary="Cancel a turnaround",
        description="Cancels a queued or running turnaround job, including its pool job. A finished one is returned unchanged.",
        errors={404: None, 422: "The turnaround is not in this project."},
    ),
}
