"""Reference entries for 3D assets: source pictures, your own mesh, review and releases."""

from typing import Literal

from pydantic import BaseModel, Field

from magic_video_editor.api_reference.model import RouteDoc
from magic_video_editor.assets3d.models import Asset3D

ASSET_404 = "No 3D asset with this id, or it is not yours."
CANDIDATE_404 = "No 3D asset or candidate with these ids, or it is not yours."
STALE = "`expected_revision` is stale (`detail.current_revision`)."
STORAGE_503 = "File storage is unavailable right now. Retry later."
LIMITS_507 = "Your storage allowance is used up."
TAG = "3D"


class StageAvailability(BaseModel):
    available: bool
    reason: str | None = Field(
        description="Why the stage cannot run, starting \"not available yet\"; null when it can."
    )
    worker_id: str | None = Field(default=None, description="The machine a run would be pinned to.")


class Assets3DCapabilities(BaseModel):
    account_tier: Literal["invited", "public"]
    stages: dict[str, StageAvailability] = Field(
        description="The engine stages (`mesh`, `clean`) and whether they can run for you."
    )
    upload_available: bool
    own_mesh_available: bool = Field(description="A GLB you already have can be a mesh candidate.")
    viewer_available: bool
    release_available: bool
    kinds: list[str]
    source_roles: list[str]
    stage_order: list[str]
    max_sources: int
    max_candidates_per_parent: int
    max_model_bytes: int
    max_source_bytes: int
    max_release_bytes: int
    upload_types: list[str]


class Assets3DUploadGrant(BaseModel):
    asset_key: str
    upload_url: str
    upload_headers: dict[str, str]
    revision: int


class Asset3DReadinessProblem(BaseModel):
    stage: str | None
    message: str


class Asset3DReleasedModel(BaseModel):
    stage: str
    candidate_id: str


class Assets3DReadiness(BaseModel):
    ready: bool
    problems: list[Asset3DReadinessProblem]
    model: Asset3DReleasedModel | None = Field(description="The candidate a release would ship as its model.")
    sources: int = Field(description="How many source pictures a release would hold.")


ROUTES: dict[tuple[str, str], RouteDoc] = {
    ("GET", "/studio/3d/capabilities"): RouteDoc(
        tag=TAG,
        response=Assets3DCapabilities,
        summary="Get what 3D assets can do",
        description=(
            "Which engine stages can run for this account and why not, what every "
            "account can do (upload pictures and its own GLB, view, release), and "
            "the limits on files and candidates. `mesh` and `clean` stay unavailable "
            "while 3D generation is switched off on this server or the 3D engine "
            "profile is not qualified, which today it is not; public accounts also "
            "need a machine of their own on that profile."
        ),
    ),
    ("POST", "/studio/3d"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Create a 3D asset",
        description="Creates an empty 3D asset called `name`, a `character` or a `prop` (`kind`).",
    ),
    ("GET", "/studio/3d"): RouteDoc(
        tag=TAG,
        response=list[Asset3D],
        summary="List your 3D assets",
        description="Every 3D asset you own, newest first, each at its current revision.",
    ),
    ("GET", "/studio/3d/{id}"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Get a 3D asset",
        description=(
            "The asset at its current revision: source pictures, candidates with "
            "their measurements and reviews, the selection per stage, and releases."
        ),
        errors={404: ASSET_404},
    ),
    ("DELETE", "/studio/3d/{id}"): RouteDoc(
        tag=TAG,
        summary="Delete a 3D asset",
        description=(
            "Permanently deletes the asset with every picture, candidate, release and "
            "file. `confirm_name` must repeat the asset's exact name."
        ),
        errors={404: ASSET_404, 422: "`confirm_name` does not match (`deletion_not_confirmed`).", 503: STORAGE_503},
    ),
    ("POST", "/studio/3d/{id}/uploads"): RouteDoc(
        tag=TAG,
        response=Assets3DUploadGrant,
        summary="Grant a 3D asset upload",
        description=(
            "Grants a storage key on this asset for one file of `content_type` "
            "`image/png`, `image/jpeg` or `image/webp` (a source picture, at most "
            "20 MB) or `model/gltf-binary` (a GLB, at most 200 MB). PUT the bytes to "
            "`upload_url` with the same `Content-Type` and the `upload_headers`; that "
            "request needs no Studio authorization. Then pass `asset_key` to "
            "`POST …/sources` or `POST …/meshes`.\n\n"
            "Until it is used, a grant counts against your storage: its full limit, "
            "or `byte_length` when you give the file's size, which also becomes the "
            "signed ceiling. A grant that would not fit in what you have left is "
            "refused (507). "
            "An asset holds at most 8 unused grants; one unused for over an hour and "
            "a quarter is released. An asset takes at most 64 uploads in all."
        ),
        errors={
            404: ASSET_404,
            409: (
                "The asset changed during the grant (retry), it has 8 unused grants "
                "(`too_many_pending_uploads`), or it has taken 64 uploads (`too_many_uploads`)."
            ),
            422: "The content type is not a PNG, JPEG, WebP or GLB, or `byte_length` is over its limit.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/3d/{id}/sources"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Set a source picture",
        description=(
            "Makes a granted picture upload the asset's `front`, `back`, `left` or "
            "`right` view. The bytes are checked to be a PNG, JPEG or WebP, and must "
            "match the type they were uploaded as. A new picture for a role replaces "
            "the old one in the asset; the old file stays stored until the asset is "
            "deleted."
        ),
        errors={
            403: "The upload was not granted to this asset.",
            404: "No such asset, or nothing has been uploaded to `asset_key` yet.",
            409: f"{STALE} Or this picture already is that view.",
            422: "The file is not a PNG, JPEG or WebP, does not match its upload type, or is larger than 20 MB.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/3d/{id}/meshes"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Add your own mesh",
        description=(
            "Adds a granted GLB upload as a `mesh` candidate (origin `upload`). The "
            "file must be a self-contained binary glTF 2.0: header, JSON chunk and "
            "optional BIN chunk, with no external buffers or images, and a JSON "
            "chunk of at most 4 MB. `measured` "
            "reports its meshes, primitives, triangles, vertices, nodes, materials, "
            "textures, images, skins, joints, animations and the POSITION bounds "
            "(node transforms not applied). A stage holds at most 8 candidates from "
            "one parent."
        ),
        errors={
            403: "The upload was not granted to this asset.",
            404: "No such asset, or nothing has been uploaded to `asset_key` yet.",
            409: f"{STALE} Or the file is already a mesh candidate, or the stage is full.",
            422: "The file is not a valid GLB, does not match its upload type, or is larger than 200 MB.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/3d/{id}/candidates/{candidate_id}/review"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Keep or reject a 3D candidate",
        description=(
            "Records `outcome` (`keep` or `reject`) with an optional `note`, bound to "
            "the candidate's file hash. The review records whether a signed-in person "
            "or an API key gave it, and releases repeat that. Rejecting a selected "
            "candidate clears its stage's selection.\n\n"
            "Send `sha256`, the candidate's file hash as you read it, so that a verdict "
            "is never applied to a file you did not look at."
        ),
        errors={404: CANDIDATE_404, 409: f"{STALE} Or `sha256` does not match (`candidate_file_changed`)."},
    ),
    ("PUT", "/studio/3d/{id}/selections"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Select candidates per stage",
        description=(
            "`selections` maps a stage to the candidate to select, or to `null` to "
            "clear it, applied together. Each must be a kept candidate of that stage. "
            "A candidate made from a parent needs that parent selected in its own "
            "stage, in this request or already. Selecting a different parent never "
            "changes later stages; readiness then reports the mixed chain."
        ),
        errors={
            404: CANDIDATE_404,
            409: f"{STALE} Or a candidate is not kept, or its parent is not selected (`parent_not_selected`).",
            422: "A candidate belongs to another stage.",
        },
    ),
    ("POST", "/studio/3d/{id}/stages/{stage}"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Run an engine stage",
        description=(
            "Runs `mesh` (from the source pictures; a `front` picture is required) or "
            "`clean` (from `parent_id`, a mesh candidate you kept or selected) on the "
            "3D engine through the worker pool. `settings` for mesh: `resolution` 512 "
            "or 1024 (default 1024) and `seed` (default random). For clean: exactly one "
            "of `quality_tier` (`draft`, `standard`, `high`, `ultra`; default "
            "`standard`) or `target_triangles` (1,000 to 500,000).\n\n"
            "The machine is resolved and pinned with its recipe before submission, "
            "never another profile's. The run is recorded in `operations` with the "
            "settings exactly as submitted. `request_id` makes it idempotent: the same "
            "id returns the same run. Call `POST …/reconcile` to fold a finished run "
            "into a candidate, whose provenance and gate come only from what the "
            "worker attested."
        ),
        errors={
            404: CANDIDATE_404,
            409: (
                "The stage is not available yet (`stage_unavailable`, with the reason), "
                "the request_id was used for another request (`request_id_reused`), the "
                "parent is not kept or failed its gate, or the stage is full."
            ),
            422: "A setting is out of range or unknown, or the parent is missing or not a mesh.",
            429: None,
            507: LIMITS_507,
        },
    ),
    ("POST", "/studio/3d/{id}/reconcile"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Fold finished stage runs into the asset",
        description=(
            "Resubmits any run whose submission was interrupted (the same job), and "
            "turns finished runs into candidates or records why they failed. Safe to "
            "call any number of times."
        ),
        errors={404: ASSET_404, 409: "The asset is busy; reconcile again."},
    ),
    ("GET", "/studio/3d/{id}/readiness"): RouteDoc(
        tag=TAG,
        response=Assets3DReadiness,
        summary="Check whether the asset can be released",
        description=(
            "Lists what blocks a release: no selected mesh, a selected candidate no "
            "longer kept (or reviewed on a different file), a failed gate, or a chain "
            "whose parent is not the one selected. Also names the model a release "
            "would ship."
        ),
        errors={404: ASSET_404},
    ),
    ("POST", "/studio/3d/{id}/release"): RouteDoc(
        tag=TAG,
        response=Asset3D,
        summary="Release a 3D asset",
        description=(
            "Freezes the selection into a zip called `name`: `model/<asset>.glb` (the "
            "selected `clean` candidate, else the selected `mesh`), `sources/<role>.<ext>`, "
            "a `manifest.json` with the chain's hashes, provenance, gates and reviews "
            "and every file's SHA-256, and a `CREDITS.md`.\n\n"
            "A release never changes. Download it with "
            "`GET /studio/3d/{id}/releases/{release_id}/download`."
        ),
        errors={
            404: ASSET_404,
            409: f"{STALE} Or the asset is not ready, or a release with this name exists.",
            413: "The model, pictures and manifest are larger than 300 MB.",
            429: None,
            503: STORAGE_503,
            507: LIMITS_507,
        },
    ),
    ("GET", "/studio/3d/{id}/releases/{release_id}/download"): RouteDoc(
        tag=TAG,
        file="The release zip.",
        summary="Download a 3D asset release",
        description="Streams one of the asset's releases as `3d-<release_id>.zip`. Supports Range requests.",
        errors={404: "No asset or release with these ids, or it is not yours.", 416: None, 503: STORAGE_503},
    ),
    ("GET", "/studio/3d/{id}/assets/{key}"): RouteDoc(
        tag=TAG,
        file="The stored file.",
        summary="Get a 3D asset file",
        description=(
            "Returns one of the asset's files by key: a source picture, a candidate's "
            "GLB or a release. Pictures are served inline; GLBs and zips as downloads. "
            "Supports Range requests."
        ),
        errors={404: "No such file in this asset, or it is not yours.", 416: None, 503: STORAGE_503},
    ),
}
