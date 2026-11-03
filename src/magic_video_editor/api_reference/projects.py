"""Reference entries for project, subject, opening-frame and setup routes."""

from pydantic import BaseModel

from magic_video_editor.api_reference.model import RouteDoc
from magic_video_editor.studio.setup import ProjectSetup


class ProjectSetupSnapshot(BaseModel):
    revision: int
    setup: ProjectSetup

_PROJECT = "No such project of yours."
_UNREADABLE = (
    "The project's latest revision cannot be read (`manifest_unreadable`). Call "
    "`POST /studio/projects/{project_id}/repair`."
)
_SETUP_VERSION = (
    "This project uses the versioned setup; send `X-Studio-Setup-Version: 1` "
    "(`unsupported_setup_version`)."
)
_SETUP_409 = (
    "A stale `expected_revision`, a missing or unsupported `X-Studio-Setup-Version` "
    "header (`unsupported_setup_version`), or a setup conflict (`setup_conflict`)."
)
_STORAGE_FULL = "Your storage allowance is used up. Delete something first."
_UPLOAD_TARGET = (
    "Returns `key`, a signed `upload_url` for a `PUT` of the bytes, and "
    "`upload_headers`, which you send with the `PUT` unchanged. The upload itself "
    "records nothing in the project."
)

ROUTES: dict[tuple[str, str], RouteDoc] = {
    # Projects
    ("POST", "/studio/projects"): RouteDoc(
        tag="Projects",
        summary="Create a project",
        description=(
            "Creates an empty animation project that you own, at revision 1.\n\n"
            "`preset_motions` adds those preset animations at once. `external_subject` "
            "binds the project to a record in another system; see `GET /studio/targets` "
            "for the installed systems. You can bind each external record to only one "
            "of your projects.\n\n"
            "Does not start any generation."
        ),
        errors={
            400: "`external_subject` names an unknown system (`unknown_target`) or entity kind (`unknown_entity_kind`).",
            409: (
                "One of your projects is already bound to that `external_subject` "
                "(`external_subject_already_bound`); `detail.project_id` names it."
            ),
        },
    ),
    ("GET", "/studio/projects"): RouteDoc(
        tag="Projects",
        summary="List your projects",
        description=(
            "Lists every project you own, with its revision, subject state, motion and "
            "release counts, and a count of running, failed and completed work.\n\n"
            "A project whose latest revision cannot be read is still listed, with "
            "`unreadable: true`; repair it with `POST /studio/projects/{project_id}/repair`. "
            "`expires_at` is set when the project is due for deletion for inactivity. "
            "Opening the project resets it. Projects with a release never expire."
        ),
    ),
    ("GET", "/studio/projects/for-external-subject"): RouteDoc(
        tag="Projects",
        summary="Find the project bound to an external subject",
        description=(
            "Returns your project bound to the record named by `system`, `entity_kind` "
            "and `entity_id`.\n\n"
            "When none exists, it answers `404`; create one with `POST /studio/projects` "
            "and `external_subject` set."
        ),
        errors={
            404: "None of your projects is bound to that record.",
            410: None,
            503: _UNREADABLE,
        },
    ),
    ("GET", "/studio/projects/{project_id}"): RouteDoc(
        tag="Projects",
        summary="Get a project",
        description=(
            "Returns the project at its current revision: the subject and its "
            "revisions, the opening frame, views, motions and their takes, and the "
            "active operations.\n\n"
            "`primary_action` names the next step, such as `add_subject`, "
            "`approve_subject`, `add_motion` or `export`.\n\n"
            "Send `X-Studio-Setup-Version: 1`. A project that uses the versioned setup, "
            "such as a `single_view` project or one with views declared through "
            "`POST /studio/projects/{project_id}/setup/apply`, requires it."
        ),
        errors={404: _PROJECT, 409: _SETUP_VERSION, 410: None, 503: _UNREADABLE},
    ),
    ("DELETE", "/studio/projects/{project_id}"): RouteDoc(
        tag="Projects",
        summary="Delete a project",
        description=(
            "Permanently deletes the project and every file, job, release and export it "
            "owns. Running work is cancelled first. Packages already delivered to "
            "another system are not recalled.\n\n"
            "Send the project's exact name in `confirm_name`. Returns `204`."
        ),
        errors={
            404: _PROJECT,
            410: None,
            422: "`confirm_name` is not the project's exact name (`deletion_not_confirmed`).",
            503: _UNREADABLE,
        },
    ),
    ("PATCH", "/studio/projects/{project_id}"): RouteDoc(
        tag="Projects",
        summary="Set a project's external subject",
        description=(
            "Binds the project to a record in another system, or clears the binding "
            "when `external_subject` is `null`. The field is required.\n\n"
            "Leaves the subject, takes and approvals as they are. Preset motion prompts "
            "that use the subject's name are rewritten with the new `display_name`, "
            "unless you gave that motion its own prompt."
        ),
        errors={
            400: "`external_subject` names an unknown system (`unknown_target`) or entity kind (`unknown_entity_kind`).",
            404: _PROJECT,
            409: None,
            410: None,
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("GET", "/studio/projects/{project_id}/gpu-telemetry"): RouteDoc(
        tag="Projects",
        summary="Get GPU telemetry for a project",
        description=(
            "Returns the latest GPU sample recorded for this project's work: device, "
            "VRAM use, utilization, active operation count and when it was sampled. "
            "`stale` is true when the sample is old.\n\n"
            "Returns `null` when there is no sample."
        ),
        errors={404: _PROJECT, 410: None, 503: _UNREADABLE},
    ),
    ("GET", "/studio/projects/{project_id}/files"): RouteDoc(
        tag="Projects",
        summary="Download a project file",
        description=(
            "Streams one of the project's files, named by `key`. The links returned by "
            "the preview and download routes point here.\n\n"
            "Every request is checked against the caller, so a copied link does not work "
            "for anyone else. `download` sets an attachment filename. Single byte ranges "
            "are supported (`206`)."
        ),
        errors={
            404: "No such project of yours, or the file is not one of its files or does not exist.",
            410: None,
            416: None,
            503: _UNREADABLE,
        },
        file="The file, with its stored content type.",
    ),
    ("POST", "/studio/projects/{project_id}/assets/preview"): RouteDoc(
        tag="Projects",
        summary="Get a project asset preview link",
        description=(
            "Returns a link for showing a project asset inline, such as in an image or "
            "video element. `asset_key` must be a subject revision, an opening frame, a "
            "motion take or a bound view reference of this project.\n\n"
            "The link is `upload_url` in the response, despite the name. It points at "
            "`GET /studio/projects/{project_id}/files` and works only for you."
        ),
        errors={
            404: _PROJECT,
            410: None,
            422: (
                "The asset is not part of this project (`asset_preview_not_allowed`), is "
                "missing (`asset_preview_missing`), or is a view reference that failed "
                "its integrity check (`asset_preview_reference_invalid`)."
            ),
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/assets/download"): RouteDoc(
        tag="Projects",
        summary="Get a project asset download link",
        description=(
            "Like the preview link, but the file downloads as an attachment named "
            "after the project and subject take, for example `my-hero-subject-take-2.png`.\n\n"
            "The link is `upload_url` in the response and works only for you."
        ),
        errors={
            404: _PROJECT,
            410: None,
            422: (
                "The asset is not part of this project (`asset_preview_not_allowed`), is "
                "missing (`asset_preview_missing`), or is a view reference that failed "
                "its integrity check (`asset_preview_reference_invalid`)."
            ),
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/operations/cancel-all"): RouteDoc(
        tag="Projects",
        summary="Cancel all project operations",
        description=(
            "Cancels every queued or running operation in the project.\n\n"
            "Partial success: read both `cancelled` (job ids) and `skipped`. A job that "
            "finished before it could be cancelled is listed in `skipped` with its "
            "current `state`."
        ),
        errors={404: _PROJECT, 410: None, 503: _UNREADABLE},
    ),
    ("GET", "/studio/projects/{project_id}/operations"): RouteDoc(
        tag="Projects",
        summary="List a project's current operations",
        description=(
            "Lists operations that are queued, running, or finished but not yet "
            "settled into the project, followed by up to 10 recent failed or "
            "cancelled ones. A finished operation's `stage` is `awaiting_settlement` "
            "until it is settled.\n\n"
            "Poll this while work runs, calling "
            "`POST /studio/projects/{project_id}/activity/reconcile` before each read: "
            "for work on an attached machine or Higgsfield, reconciling is what moves a "
            "finished job forward. Stop when no `stage` is `queued`, `generating` or "
            "`awaiting_settlement`."
        ),
        errors={404: _PROJECT, 410: None, 503: _UNREADABLE},
    ),
    ("GET", "/studio/projects/{project_id}/activity"): RouteDoc(
        tag="Projects",
        summary="List a project's activity",
        description=(
            "Pages through every subject, opening-frame and motion operation, newest "
            "first, finished ones included. A failed operation carries `failure_code`, "
            "`failure_explanation` and `retryable`.\n\n"
            "Pass `next_cursor` back as `cursor` for the next page. `limit` is 1 to 100. "
            "`motion_id` narrows the list to one motion; `motion_id=subject` narrows it to "
            "subject and opening-frame work."
        ),
        errors={
            404: _PROJECT,
            410: None,
            422: "`cursor` is not one this route returned (`invalid_cursor`).",
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/activity/reconcile"): RouteDoc(
        tag="Projects",
        summary="Settle finished operations into a project",
        description=(
            "Settles every finished subject, opening-frame and motion operation that is "
            "not yet in the project, each as a new revision. Then returns the first page "
            "of activity.\n\n"
            "An operation that cannot be settled is skipped and keeps a "
            "`settlement_blocked` note explaining why. Safe to call repeatedly."
        ),
        errors={404: _PROJECT, 410: None, 500: None, 503: _UNREADABLE},
    ),
    ("GET", "/studio/projects/{project_id}/history"): RouteDoc(
        tag="Projects",
        summary="List a project's history",
        description=(
            "Lists every saved revision of the project and every failed or cancelled "
            "operation, oldest first. Each entry has a `summary` and, for a revision, "
            "its number."
        ),
        errors={
            404: _PROJECT,
            410: None,
            503: "The project, or one of its earlier revisions, cannot be read.",
        },
    ),
    ("POST", "/studio/projects/{project_id}/repair"): RouteDoc(
        tag="Projects",
        summary="Repair an unreadable project",
        description=(
            "Restores a project whose latest revision cannot be read. It finds the "
            "newest earlier revision that still reads and saves it as a new revision. "
            "Nothing is deleted; the bad revisions stay in the history.\n\n"
            "Returns `repaired`, the revision restored from, the new revision, and the "
            "revisions that failed to read. `repaired` is false when the latest "
            "revision was already fine or no readable revision exists."
        ),
        errors={
            404: _PROJECT,
            409: "Another change landed during the repair; `detail.current_revision` has it. Re-read and retry.",
            410: None,
        },
    ),
    # Subject
    ("POST", "/studio/projects/{project_id}/subject/uploads"): RouteDoc(
        tag="Subject",
        summary="Create a subject image upload",
        description=(
            "Starts uploading a finished subject image, PNG only. " + _UPLOAD_TARGET + "\n\n"
            "After the `PUT`, call `POST /studio/projects/{project_id}/subject/settle` "
            "with the `key`. To generate a subject from reference images instead, use "
            "`POST /studio/projects/{project_id}/subject/references/uploads`."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: "`content_type` is not `image/png` (`subject_content_type_invalid`).",
            503: _UNREADABLE,
            507: _STORAGE_FULL,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/references/uploads"): RouteDoc(
        tag="Subject",
        summary="Create a subject reference upload",
        description=(
            "Starts uploading a reference image to generate the subject from: PNG, JPEG "
            "or WebP. " + _UPLOAD_TARGET + "\n\n"
            "Pass the `key` in `reference_keys` to "
            "`POST /studio/projects/{project_id}/subject/generate`."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: "`content_type` is not PNG, JPEG or WebP (`subject_reference_content_type_invalid`).",
            503: _UNREADABLE,
            507: _STORAGE_FULL,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/generate"): RouteDoc(
        tag="Subject",
        summary="Generate the subject",
        description=(
            "Queues a subject image generated from 1 to 6 uploaded reference images, "
            "with an optional `direction`. Returns the operation.\n\n"
            "`request_id` is your idempotency key: re-sending the same request with the "
            "same `request_id` returns the original operation instead of starting another.\n\n"
            "The project does not change until the result is settled, with "
            "`POST /studio/projects/{project_id}/subject/operations/{job_id}/settle` or "
            "`POST /studio/projects/{project_id}/activity/reconcile`."
        ),
        errors={
            403: (
                "Your account may not use the shared machines and has no machine of its "
                "own attached (`house_compute_unavailable`)."
            ),
            404: _PROJECT,
            409: (
                "A stale `expected_revision`, or this `request_id` was already used with "
                "another body or at an older revision (`request_id_reused`). Re-read and "
                "retry for the first; send a new `request_id` for the second."
            ),
            410: None,
            422: (
                "A reference key is not one of this project's references, is repeated, "
                "missing or too large (`subject_reference_key_invalid`, "
                "`subject_reference_invalid`), or `direction` was refused (`prompt_refused`)."
            ),
            429: None,
            503: (
                "Subject generation is not enabled on this server, prompt screening is "
                "unavailable, or the project cannot be read (`manifest_unreadable`)."
            ),
            507: None,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/operations/{job_id}/settle"): RouteDoc(
        tag="Subject",
        summary="Settle a subject generation",
        description=(
            "Adds the finished image from a subject generation to the project as a new "
            "subject revision and makes it current. The new revision is ready for "
            "approval, not approved.\n\n"
            "A new subject clears the opening frame and the set approval. Existing motion "
            "takes stay and are marked as made from an earlier subject.\n\n"
            "Settling an operation that is already settled returns the project unchanged."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: (
                "The operation is not a finished subject generation of this project "
                "(`subject_operation_not_ready`), a reference changed since it ran "
                "(`subject_reference_lineage_invalid`), or the image failed the subject "
                "checks."
            ),
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/settle"): RouteDoc(
        tag="Subject",
        summary="Settle an uploaded subject image",
        description=(
            "Adds an uploaded PNG to the project as a new subject revision and makes it "
            "current. `asset_key` is the `key` from "
            "`POST /studio/projects/{project_id}/subject/uploads`.\n\n"
            "The image is checked and assessed. Findings are warnings and never block "
            "approval. The new revision is ready for approval, not approved.\n\n"
            "A new subject clears the opening frame and the set approval. Existing motion "
            "takes stay and are marked as made from an earlier subject."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: (
                "`asset_key` is not a subject upload of this project "
                "(`subject_asset_key_invalid`), the upload is missing or too large, or "
                "the image is not a valid subject PNG. `detail.code` says which."
            ),
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/approve"): RouteDoc(
        tag="Subject",
        summary="Approve the subject",
        description=(
            "Approves the current subject revision. Motions are generated from the "
            "approved subject.\n\n"
            "Every finding in the assessment is recorded as acknowledged. To acknowledge "
            "only some, send their codes in `waived_finding_ids` with a `waiver_reason`.\n\n"
            "Clears the set approval."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: (
                "There is no settled subject to approve (`subject_assessment_missing`), a "
                "waived id is not a finding of this assessment (`subject_waiver_unknown`, "
                "with `detail.finding_ids`), or `waived_finding_ids` was sent without "
                "`waiver_reason`."
            ),
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/revisions/{revision_id}/select"): RouteDoc(
        tag="Subject",
        summary="Select a subject revision",
        description=(
            "Makes an earlier subject revision current again. The opening frame and "
            "motion takes stay.\n\n"
            "The approval carries over only if this exact image was already approved. "
            "Otherwise the revision is ready for approval again. Clears the set approval."
        ),
        errors={
            404: "No such project of yours, or no such subject revision in it.",
            409: None,
            410: None,
            422: "The revision is rejected, or its image is missing or no longer passes the subject checks.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/revisions/{revision_id}/reframe"): RouteDoc(
        tag="Subject",
        summary="Reframe or clean up a subject revision",
        description=(
            "Runs a fix on a subject revision and saves the result as a new revision, "
            "which becomes current. `edit` is `reframe` (fit the subject to the frame, "
            "the default) or `cleanup` (keep only the main subject).\n\n"
            "The new revision needs approval. Like any new subject, it clears the "
            "opening frame and the set approval."
        ),
        errors={
            404: "No such project of yours, or no such subject revision in it.",
            409: None,
            410: None,
            422: "The revision is rejected, or its image is missing, too large, or fails the subject checks after the fix.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/revisions/{revision_id}/reject"): RouteDoc(
        tag="Subject",
        summary="Reject a subject revision",
        description=(
            "Marks a subject revision as rejected so it cannot be selected. It is kept, "
            "not deleted.\n\n"
            "Rejecting the current revision makes the newest remaining revision current "
            "and clears the subject approval. Clears the set approval."
        ),
        errors={
            404: "No such project of yours, or no such subject revision in it.",
            409: None,
            410: None,
            422: "The revision is already rejected.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/subject/revisions/{revision_id}/restore"): RouteDoc(
        tag="Subject",
        summary="Restore a rejected subject revision",
        description=(
            "Makes a rejected subject revision selectable again. It does not select it."
        ),
        errors={
            404: "No such project of yours, or no such subject revision in it.",
            409: None,
            410: None,
            422: "The revision is not rejected.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/composition/compose"): RouteDoc(
        tag="Subject",
        summary="Compose the opening frame",
        description=(
            "Queues an opening-frame still made from the approved current subject and "
            "`prompt`, with the chosen `composition_model`. Returns the operation. Needs "
            "an approved subject.\n\n"
            "`request_id` is your idempotency key: re-sending the same request returns the "
            "original operation. Settle the result with "
            "`POST /studio/projects/{project_id}/composition/operations/{job_id}/settle` "
            "or `POST /studio/projects/{project_id}/activity/reconcile`.\n\n"
            "Runs on shared machines only, so public accounts cannot use it."
        ),
        errors={
            403: "Your account may not use the shared machines (`house_compute_unavailable`).",
            404: _PROJECT,
            409: (
                "A stale `expected_revision`, or this `request_id` was already used with "
                "another body or at an older revision (`request_id_reused`). Re-read and "
                "retry for the first; send a new `request_id` for the second."
            ),
            410: None,
            422: "The current subject is not approved, or `prompt` was refused (`prompt_refused`).",
            429: None,
            503: (
                "Opening-frame composition is not enabled on this server, prompt "
                "screening is unavailable, or the project cannot be read "
                "(`manifest_unreadable`)."
            ),
            507: None,
        },
    ),
    ("POST", "/studio/projects/{project_id}/composition/regenerate"): RouteDoc(
        tag="Subject",
        summary="Regenerate the opening frame",
        description=(
            "Queues a new opening-frame still. It behaves exactly like "
            "`POST /studio/projects/{project_id}/composition/compose`; once settled, the "
            "new still replaces the current one."
        ),
        errors={
            403: "Your account may not use the shared machines (`house_compute_unavailable`).",
            404: _PROJECT,
            409: (
                "A stale `expected_revision`, or this `request_id` was already used with "
                "another body or at an older revision (`request_id_reused`). Re-read and "
                "retry for the first; send a new `request_id` for the second."
            ),
            410: None,
            422: "The current subject is not approved, or `prompt` was refused (`prompt_refused`).",
            429: None,
            503: (
                "Opening-frame composition is not enabled on this server, prompt "
                "screening is unavailable, or the project cannot be read "
                "(`manifest_unreadable`)."
            ),
            507: None,
        },
    ),
    ("POST", "/studio/projects/{project_id}/composition/operations/{job_id}/settle"): RouteDoc(
        tag="Subject",
        summary="Settle an opening frame composition",
        description=(
            "Adds the finished still from a composition operation to the project and "
            "makes it the current opening frame. The previous one is marked superseded. "
            "The new still is not approved.\n\n"
            "Settling an operation that is already settled returns the project unchanged."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: (
                "The operation is not a finished composition of this project, the "
                "approved subject changed while it ran, or the still is missing or not "
                "a valid RGBA PNG (`detail.code` when given)."
            ),
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/composition/approve"): RouteDoc(
        tag="Subject",
        summary="Approve the opening frame",
        description=(
            "Approves the current opening-frame still. It must have been composed from "
            "the current subject revision."
        ),
        errors={
            404: _PROJECT,
            409: (
                "A stale `expected_revision`, or there is no current still composed from "
                "the current subject."
            ),
            410: None,
            500: None,
            503: _UNREADABLE,
        },
    ),
    # Setup
    ("GET", "/studio/projects/{project_id}/setup"): RouteDoc(
        tag="Setup",
        summary="Get a project's setup",
        description=(
            "Returns the project's `revision` and its `setup`: the sprite layout, each "
            "view with its heading, name and reference, and each view's motions.\n\n"
            "Send `X-Studio-Setup-Version: 1`. Edit the setup with "
            "`POST /studio/projects/{project_id}/setup/preview` and then `.../setup/apply`."
        ),
        errors={
            404: _PROJECT,
            409: "Send `X-Studio-Setup-Version: 1` (`unsupported_setup_version`).",
            410: None,
            503: _UNREADABLE,
        },
        response=ProjectSetupSnapshot,
    ),
    ("POST", "/studio/projects/{project_id}/setup/references"): RouteDoc(
        tag="Setup",
        summary="Stage a setup reference image",
        description=(
            "Stores a reference image for use in a setup: a single PNG, JPEG or WebP "
            "image of at most 10 MB and 8192 pixels a side, sent base64-encoded in "
            "`content_base64`.\n\n"
            "Returns the staged reference with its `revision_id`, `key`, `sha256`, "
            "`width` and `height`. Use the `revision_id` as a view's or the identity's "
            "`reference` in `POST /studio/projects/{project_id}/setup/preview`. Does not "
            "change the project.\n\n"
            "Send `X-Studio-Setup-Version: 1`."
        ),
        errors={
            404: _PROJECT,
            409: (
                "A missing or unsupported `X-Studio-Setup-Version` header "
                "(`unsupported_setup_version`), or the image cannot be decoded or is "
                "outside the limits (`setup_conflict`)."
            ),
            410: None,
            422: "`content_base64` is not valid base64, or the decoded image is empty or over 10 MB.",
            503: _UNREADABLE,
            507: _STORAGE_FULL,
        },
    ),
    ("POST", "/studio/projects/{project_id}/setup/preview"): RouteDoc(
        tag="Setup",
        summary="Preview a setup change",
        description=(
            "Compares `desired_setup` with the project's current setup and returns the "
            "plan: a `plan_token`, the `impacts` the change would have, and any "
            "`blocked_operations` that must finish or be settled first. Does not change "
            "the project.\n\n"
            "Apply the plan with `POST /studio/projects/{project_id}/setup/apply` at the "
            "same `expected_revision`. Send `X-Studio-Setup-Version: 1`."
        ),
        errors={
            404: _PROJECT,
            409: _SETUP_409,
            410: None,
            422: "`desired_setup` is not a valid setup for its sprite layout.",
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/setup/apply"): RouteDoc(
        tag="Setup",
        summary="Apply a setup change",
        description=(
            "Applies a plan from `POST /studio/projects/{project_id}/setup/preview` in one "
            "revision. Send its `plan_token` and the same `expected_revision` it was "
            "previewed at.\n\n"
            "`confirmed_impacts` must list the `impact_id` of every impact that "
            "requires confirmation. A plan that changes nothing returns the project at "
            "its current revision. Send `X-Studio-Setup-Version: 1`."
        ),
        errors={
            404: _PROJECT,
            409: (
                "A stale `expected_revision`; a missing or unsupported "
                "`X-Studio-Setup-Version` (`unsupported_setup_version`); a plan token that "
                "is invalid or was made for another revision (`setup_conflict`); "
                "operations that must finish or be settled first (`setup_blocked`); or "
                "unconfirmed impacts (`setup_confirmation_required`)."
            ),
            410: None,
            422: "The planned setup is no longer valid.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/setup/dispatches/{operation_id}/abandon"): RouteDoc(
        tag="Setup",
        summary="Abandon a stuck motion dispatch",
        description=(
            "Releases a motion dispatch reservation left behind when starting a "
            "generation did not finish. A held reservation blocks setup changes to that "
            "motion.\n\n"
            "Send `confirm_dispatch_stopped: true` once you know the process that held "
            "it has stopped. If the operation was registered, it must have failed or "
            "been cancelled. Send `X-Studio-Setup-Version: 1`."
        ),
        errors={
            404: _PROJECT,
            409: (
                "A stale `expected_revision`; a missing or unsupported "
                "`X-Studio-Setup-Version` (`unsupported_setup_version`); "
                "`confirm_dispatch_stopped` is not true (`setup_confirmation_required`); "
                "or the operation is still live or finished but not settled (`setup_blocked`)."
            ),
            410: None,
            422: "The project holds no reservation for `operation_id`.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("PUT", "/studio/projects/{project_id}/sprite-layout"): RouteDoc(
        tag="Setup",
        summary="Set a project's sprite layout",
        description=(
            "Sets `sprite_layout`: `unassigned`, `single_view`, `side_view` (views "
            "`left` and `right`) or `isometric` (eight compass views). Every existing view "
            "must fit the new layout.\n\n"
            "Clears the set approval."
        ),
        errors={
            404: _PROJECT,
            409: "A stale `expected_revision`, or " + _SETUP_VERSION[0].lower() + _SETUP_VERSION[1:],
            410: None,
            422: "An existing view does not fit the new layout; use a separate project.",
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("PUT", "/studio/projects/{project_id}/views/{view_id}"): RouteDoc(
        tag="Setup",
        summary="Create or update a character view",
        description=(
            "Creates the view, or updates its `name` and `reference`. The view must fit "
            "the project's sprite layout.\n\n"
            "`reference` is `{key, sha256}` for an image uploaded with "
            "`POST /studio/projects/{project_id}/views/{view_id}/reference/uploads`, or "
            "the approved current subject. Send `reference: null` to remove it; leave it "
            "out to keep the current one. The view's motions use the new reference.\n\n"
            "Clears the set approval."
        ),
        errors={
            404: _PROJECT,
            409: "A stale `expected_revision`, or " + _SETUP_VERSION[0].lower() + _SETUP_VERSION[1:],
            410: None,
            422: (
                "The view does not fit the sprite layout, or the reference does not "
                "belong to this view, does not match `sha256`, is not a single decodable "
                "image within limits, or is a subject image that is not the approved "
                "current subject."
            ),
            500: None,
            503: _UNREADABLE,
        },
    ),
    ("POST", "/studio/projects/{project_id}/views/{view_id}/reference/uploads"): RouteDoc(
        tag="Setup",
        summary="Create a view reference upload",
        description=(
            "Starts uploading a reference image for one view: PNG, JPEG or WebP. "
            + _UPLOAD_TARGET + "\n\n"
            "Bind it with `PUT /studio/projects/{project_id}/views/{view_id}`, passing the "
            "`key` and the image's `sha256`."
        ),
        errors={
            404: _PROJECT,
            409: None,
            410: None,
            422: (
                "The view does not fit the sprite layout, or `content_type` is not PNG, "
                "JPEG or WebP (`subject_reference_content_type_invalid`)."
            ),
            503: _UNREADABLE,
            507: _STORAGE_FULL,
        },
    ),
}
