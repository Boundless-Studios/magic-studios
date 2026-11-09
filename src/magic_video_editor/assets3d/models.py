"""A 3D asset is one revisioned document: source pictures, stage candidates, their
reviews, the selection per stage, and releases.

See docs/design/3d-studio.md. Candidates form a chain (clean from mesh, rig from
clean, …); a review is bound to the candidate's file hash; a release is a frozen
pack that is never edited. This first part has no engine: candidates come from the
owner's own GLB uploads, and the engine stages report themselves unavailable.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["character", "prop"]
Role = Literal["front", "back", "left", "right"]
Stage = Literal["mesh", "clean", "rig", "clip", "export", "render"]
ImageType = Literal["image/png", "image/jpeg", "image/webp"]
GLB_TYPE = "model/gltf-binary"

STAGES: tuple[Stage, ...] = ("mesh", "clean", "rig", "clip", "export", "render")
ROLES: tuple[Role, ...] = ("front", "back", "left", "right")
# The stage each stage's candidates are made from; `mesh` starts the chain.
PARENT_STAGE: dict[Stage, Stage | None] = {
    "mesh": None,
    "clean": "mesh",
    "rig": "clean",
    "clip": "rig",
    "export": "clip",
    "render": "clip",
}
MAX_SOURCES = len(ROLES)
MAX_CANDIDATES_PER_PARENT = 8
# Provisional until qualification measures real sizes; under the pool's 256 MiB
# output verification cap, so an engine output and an upload share one limit.
MAX_MODEL_BYTES = 200 * 1024 * 1024
MAX_SOURCE_BYTES = 20 * 1024 * 1024


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Asset3DSource(Model):
    source_id: str
    role: Role
    asset_key: str
    sha256: str
    byte_length: int
    media_type: ImageType
    # Only `upload` exists yet; a sprite project's subject and turnaround picks
    # arrive later, with `origin_ref` naming the project and revision they came from.
    origin: Literal["upload"] = "upload"
    origin_ref: dict[str, Any] | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Asset3DBounds(Model):
    min: list[float] = Field(min_length=3, max_length=3)
    max: list[float] = Field(min_length=3, max_length=3)


class GlbMeasurement(Model):
    """What Studio reads from a GLB's own structure; see `media.inspect_glb`."""

    meshes: int
    primitives: int
    # Triangle-list primitives only (mode 4, the default).
    triangles: int
    # Primitives drawn as points, lines, strips or fans; their faces are not counted.
    other_primitives: int
    vertices: int
    nodes: int
    materials: int
    textures: int
    images: int
    skins: int
    joints: int
    animations: int
    # From the POSITION accessors' min/max, without node transforms applied.
    bounds: Asset3DBounds | None = None


class Asset3DGateCheck(Model):
    id: str
    passed: bool
    value: Any = None
    threshold: Any = None


class Asset3DGate(Model):
    """A stage's own measurements, as the engine attested them. Studio enforces them."""

    name: str
    passed: bool
    checks: list[Asset3DGateCheck] = Field(default_factory=list)


class Asset3DReview(Model):
    outcome: Literal["keep", "reject"]
    note: str = Field(default="", max_length=500)
    # The file this verdict was given on; a different file needs a new review.
    sha256: str
    actor_id: str
    # A signed-in person or an API key (an agent). Releases repeat it, so an
    # agent's batch verdict is never presented as a person looking at the model.
    actor_kind: Literal["session", "api_key"]
    at: str


class Asset3DCandidate(Model):
    candidate_id: str
    stage: Stage
    # The candidate of the previous stage this one was made from; none for a mesh.
    parent_id: str | None = None
    origin: Literal["upload", "engine"]
    asset_key: str
    sha256: str
    byte_length: int
    media_type: Literal["model/gltf-binary"] = GLB_TYPE
    measured: GlbMeasurement | None = None
    gate: Asset3DGate | None = None
    # An upload's file name and who added it; for engine output, what actually
    # ran, from the worker's attested diagnostics only: capability, recipe and
    # configuration, worker, engine identity, effective inputs and timings.
    provenance: dict[str, Any] = Field(default_factory=dict)
    review: Asset3DReview | None = None
    created_at: str


class Asset3DRelease(Model):
    release_id: str
    name: str
    asset_key: str
    manifest_sha256: str
    source_revision: int
    created_at: str
    file_count: int
    # The candidate whose GLB the pack ships as its model.
    model_candidate_id: str


class Asset3DOperation(Model):
    """One engine stage run, journalled before it is submitted to the pool.

    Everything the job pins is resolved and recorded here first: the worker, the
    recipe and configuration versions and the runtime-version keys they are under,
    the settings and inputs exactly as submitted. Reconciliation may resubmit it
    with the same job id and idempotency key, on any process, and folds the
    attested result into a candidate."""

    operation_id: str
    request_id: str
    stage: Stage
    parent_id: str | None = None
    settings: dict[str, Any]
    # The engine stage's input name (a view role, or `mesh`) to its stored key.
    inputs: dict[str, str]
    capability_id: str
    profile: str
    worker_id: str
    recipe_key: str
    recipe_version: str
    configuration_key: str
    configuration_version: str
    min_vram_mb: int
    deadline_seconds: int
    tenant_id: str | None = None
    job_id: str
    output_key: str
    status: Literal["submitting", "running", "succeeded", "failed", "cancelled"] = "submitting"
    error: str | None = None
    candidate_id: str | None = None
    created_at: str
    updated_at: str


class Asset3D(Model):
    asset_id: str
    owner_id: str
    name: str
    kind: Kind = "prop"
    revision: int = 1
    sources: list[Asset3DSource] = Field(default_factory=list)
    candidates: list[Asset3DCandidate] = Field(default_factory=list)
    selections: dict[Stage, str] = Field(default_factory=dict)
    releases: list[Asset3DRelease] = Field(default_factory=list)
    # Every upload grant this asset has issued (its files are deleted with it).
    upload_keys: list[str] = Field(default_factory=list)
    # Grants not yet used as a source or mesh, with when each was issued. Each
    # holds its full size limit against the owner's storage until it is used or
    # found expired.
    pending_uploads: dict[str, str] = Field(default_factory=dict)
    # Set first when the asset is deleted, before its files go. A release that
    # stored a zip checks it, and removes the zip itself if the asset is going.
    deleting: bool = False
    # Engine stage runs, newest last (see `Asset3DOperation`).
    operations: list[Asset3DOperation] = Field(default_factory=list)
