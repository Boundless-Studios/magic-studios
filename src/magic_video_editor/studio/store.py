"""Persistence contract for versioned Studio manifests."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    ExportRecord,
    FrozenModel,
    Release,
)


@dataclass(frozen=True, slots=True)
class RawManifestRevision:
    """A durable manifest revision's raw JSON, unvalidated.

    Repair (T-manifest-repair) must be able to read a revision whose manifest
    fails `AnimationProjectManifest.model_validate` -- that is exactly the
    revision it exists to recover from -- so this carries the raw `dict` a
    normal `StoredManifest` would otherwise refuse to hold.
    """

    revision: int
    manifest: dict[str, Any]
    created_at: datetime


class StoredManifest(FrozenModel):
    project_id: str
    revision: int
    manifest_digest: str
    manifest: AnimationProjectManifest
    updated_at: datetime


class ProjectRecord(FrozenModel):
    """A project row for the home list: the current manifest plus the
    project-table fields (`created_at`, `created_by`) that live outside the
    manifest JSON and so are not on `StoredManifest`.

    `list_projects` must never raise just because one project's current
    revision fails `AnimationProjectManifest.model_validate` -- that would
    take down the whole home list for an unrelated failure the user cannot
    see or fix (the list is what tells them a project needs repair in the
    first place). So a row whose manifest is unreadable carries `stored=None`,
    `unreadable=True`, and `raw_name` recovered from the raw JSON instead of
    the validated manifest -- `project_id`, `revision`, `created_at`,
    `created_by`, and `updated_at` live directly on the record so they are
    available either way."""

    project_id: str
    revision: int
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime
    stored: StoredManifest | None = None
    raw_name: str | None = None
    unreadable: bool = False


class RevisionConflict(Exception):
    """Raised when a write is based on a stale project revision."""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"expected revision is stale; current revision is {current_revision}")


class ProjectAlreadyExists(Exception):
    """Raised when creating a project that already has durable revisions."""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"project already exists at revision {current_revision}")


class ManifestMismatch(Exception):
    """Raised when a caller manifest does not match the requested project state."""


class ReleaseIntegrityError(Exception):
    """Stored immutable release fields do not match their persisted digest."""


class DuplicateReleaseName(Exception):
    """Raised when a release name is already used within a project."""

    def __init__(self, project_id: str, name: str) -> None:
        self.project_id = project_id
        self.name = name
        super().__init__(
            f"release name {name!r} already exists for project {project_id!r}"
        )


def quality_policy_versions(
    manifest: AnimationProjectManifest,
) -> tuple[str, ...]:
    """Return the ordered, de-duplicated policies pinned by a release."""
    versions: list[str] = []
    candidate_versions = (
        motion.selected_candidate.assessment.policy_version
        if motion.selected_candidate and motion.selected_candidate.assessment
        else None
        for motion in manifest.motions
    )
    for version in (
        manifest.subject.approval.policy_version,
        *candidate_versions,
        manifest.set_approval.policy_version,
    ):
        if version is not None and version not in versions:
            versions.append(version)
    return tuple(versions)


class StudioStore(Protocol):
    async def create(self, manifest: AnimationProjectManifest) -> StoredManifest: ...

    async def get(self, project_id: str) -> StoredManifest | None: ...

    async def append(
        self,
        project_id: str,
        expected_revision: int,
        manifest: AnimationProjectManifest,
    ) -> StoredManifest: ...

    async def list_revisions(self, project_id: str) -> tuple[StoredManifest, ...]:
        """Every durable manifest revision for a project, oldest first."""
        ...

    async def list_raw_revisions(
        self, project_id: str
    ) -> tuple[RawManifestRevision, ...]:
        """Every durable manifest revision for a project, newest first, as raw
        JSON that has not been passed through `AnimationProjectManifest.model_validate`
        (T-manifest-repair). Used only to recover a project whose current
        revision fails validation -- everything else reads through
        `list_revisions`/`get`."""
        ...

    async def current_revision_number(self, project_id: str) -> int | None:
        """The project's current revision number, or `None` if the project does
        not exist. Reads `studio_projects.current_revision` directly, so it
        works even when that revision's manifest cannot be validated."""
        ...

    async def find_by_external_subject(
        self,
        *,
        owner_id: str,
        system: str,
        entity_kind: str,
        entity_id: str,
    ) -> StoredManifest | None:
        """The owner's project currently bound to this external subject, if any
        (deep-link idempotency, Part A). At most one project per owner should
        ever carry a given `(system, entity_kind, entity_id)` binding -- the
        service enforces that by checking here before creating a new one."""
        ...

    async def list_projects(self, owner_id: str) -> tuple[ProjectRecord, ...]:
        """Every project owned by `owner_id`, newest `updated_at` first (the
        project home list, T-project-home)."""
        ...

    async def list_project_owners(self) -> tuple[tuple[str, str], ...]:
        """Every project id with its current owner, for the retention sweep."""
        ...

    async def delete_project(self, project_id: str) -> None:
        """Permanently remove the project with every revision, release and
        export record. Stored files are deleted separately by the service."""
        ...

    async def reassign_owner(self, previous_owner_id: str, new_owner_id: str) -> None:
        """Move the durable creator attribution (`studio_projects.created_by`) to a
        new actor id. Manifest ownership is moved by the service, which appends a
        revision so each manifest keeps a valid digest."""
        ...

    async def create_release(
        self,
        *,
        project_id: str,
        expected_revision: int,
        name: str,
        manifest: AnimationProjectManifest,
    ) -> Release: ...

    async def get_release(self, release_id: str) -> Release | None: ...

    async def list_releases(self, project_id: str) -> tuple[Release, ...]: ...

    async def save_export(self, record: ExportRecord) -> ExportRecord: ...

    async def get_export(self, export_id: str) -> ExportRecord | None: ...

    async def list_release_exports(
        self, release_id: str
    ) -> tuple[ExportRecord, ...]:
        """Every export recorded for a release, newest first."""
        ...
