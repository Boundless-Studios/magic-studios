"""In-memory Studio manifest persistence."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from magic_video_editor.studio.manifest import manifest_digest
from magic_video_editor.studio.write_guard import serialize_checked
from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    ExportRecord,
    Release,
    canonical_release_digest,
    canonical_release_payload_v1,
)
from magic_video_editor.studio.store import (
    DuplicateReleaseName,
    ManifestMismatch,
    ProjectAlreadyExists,
    ProjectRecord,
    RawManifestRevision,
    ReleaseIntegrityError,
    RevisionConflict,
    StoredManifest,
    quality_policy_versions,
)


class MemoryStudioStore:
    def __init__(self) -> None:
        self._projects: dict[str, StoredManifest] = {}
        self._revisions: dict[str, list[StoredManifest]] = {}
        self._raw_revisions: dict[str, list[RawManifestRevision]] = {}
        self._current_revision: dict[str, int] = {}
        self._releases: dict[str, Release] = {}
        self._release_names: dict[tuple[str, str], str] = {}
        self._exports: dict[str, ExportRecord] = {}
        self._created_at: dict[str, datetime] = {}
        self._created_by: dict[str, str | None] = {}

    async def create(self, manifest: AnimationProjectManifest) -> StoredManifest:
        existing = self._projects.get(manifest.project_id)
        if existing is not None:
            raise ProjectAlreadyExists(existing.revision)
        now = datetime.now(UTC)
        stored = StoredManifest(
            project_id=manifest.project_id,
            revision=1,
            manifest_digest=manifest_digest(manifest),
            manifest=deepcopy(manifest),
            updated_at=now,
        )
        self._projects[manifest.project_id] = stored
        self._revisions.setdefault(manifest.project_id, []).append(deepcopy(stored))
        self._current_revision[manifest.project_id] = 1
        self._raw_revisions.setdefault(manifest.project_id, []).append(
            RawManifestRevision(
                revision=1,
                manifest=stored.manifest.model_dump(mode="json"),
                created_at=now,
            )
        )
        self._created_at[manifest.project_id] = now
        self._created_by[manifest.project_id] = manifest.owner_id
        return deepcopy(stored)

    async def append_raw(
        self, project_id: str, manifest: dict[str, Any]
    ) -> RawManifestRevision:
        """Test-only: append a raw, unvalidated manifest revision as the new
        current revision, so `repair_project` can be exercised against a
        corrupt row without a real Postgres row (T-manifest-repair)."""
        current_revision = self._current_revision.get(project_id, 0)
        next_revision = current_revision + 1
        raw_revision = RawManifestRevision(
            revision=next_revision,
            manifest=deepcopy(manifest),
            created_at=datetime.now(UTC),
        )
        self._raw_revisions.setdefault(project_id, []).append(raw_revision)
        self._current_revision[project_id] = next_revision
        return raw_revision

    async def get(self, project_id: str) -> StoredManifest | None:
        stored = self._projects.get(project_id)
        return deepcopy(stored) if stored is not None else None

    async def list_project_owners(self) -> tuple[tuple[str, str], ...]:
        return tuple((project_id, stored.manifest.owner_id) for project_id, stored in self._projects.items())

    async def list_projects(self, owner_id: str) -> tuple[ProjectRecord, ...]:
        """Every project owned by `owner_id`.

        A project's current revision can be a raw one appended by
        `append_raw` (T-manifest-repair's test seam for a corrupt revision)
        that never made it into `self._projects` because it failed
        validation. Such a project is still listed -- as unreadable, with its
        raw `name` -- rather than raising or silently falling back to a stale
        readable revision."""
        records = []
        for project_id, stored in self._projects.items():
            if stored.manifest.owner_id != owner_id:
                continue
            current_revision = self._current_revision.get(project_id, stored.revision)
            if current_revision == stored.revision:
                records.append(
                    ProjectRecord(
                        project_id=project_id,
                        revision=stored.revision,
                        created_at=self._created_at[project_id],
                        created_by=self._created_by.get(project_id),
                        updated_at=stored.updated_at,
                        stored=deepcopy(stored),
                    )
                )
                continue
            raw_current = next(
                (
                    raw
                    for raw in self._raw_revisions.get(project_id, [])
                    if raw.revision == current_revision
                ),
                None,
            )
            records.append(
                ProjectRecord(
                    project_id=project_id,
                    revision=current_revision,
                    created_at=self._created_at[project_id],
                    created_by=self._created_by.get(project_id),
                    updated_at=(
                        raw_current.created_at if raw_current else stored.updated_at
                    ),
                    stored=None,
                    raw_name=(
                        raw_current.manifest.get("name") if raw_current else None
                    ),
                    unreadable=True,
                )
            )
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return tuple(records)

    async def delete_project(self, project_id: str) -> None:
        release_ids = {
            release_id for release_id, release in self._releases.items()
            if release.project_id == project_id
        }
        for export_id in [
            export_id for export_id, record in self._exports.items()
            if record.release_id in release_ids
        ]:
            del self._exports[export_id]
        for release_id in release_ids:
            del self._releases[release_id]
        for key in [key for key in self._release_names if key[0] == project_id]:
            del self._release_names[key]
        for table in (
            self._projects, self._revisions, self._raw_revisions,
            self._current_revision, self._created_at, self._created_by,
        ):
            table.pop(project_id, None)

    async def reassign_owner(self, previous_owner_id: str, new_owner_id: str) -> None:
        for project_id, created_by in list(self._created_by.items()):
            if created_by == previous_owner_id:
                self._created_by[project_id] = new_owner_id

    async def list_revisions(self, project_id: str) -> tuple[StoredManifest, ...]:
        return tuple(deepcopy(item) for item in self._revisions.get(project_id, []))

    async def list_raw_revisions(
        self, project_id: str
    ) -> tuple[RawManifestRevision, ...]:
        return tuple(
            deepcopy(item)
            for item in reversed(self._raw_revisions.get(project_id, []))
        )

    async def current_revision_number(self, project_id: str) -> int | None:
        return self._current_revision.get(project_id)

    async def find_by_external_subject(
        self,
        *,
        owner_id: str,
        system: str,
        entity_kind: str,
        entity_id: str,
    ) -> StoredManifest | None:
        for stored in self._projects.values():
            manifest = stored.manifest
            if manifest.owner_id != owner_id:
                continue
            subject = manifest.external_subject
            if (
                subject is not None
                and subject.system == system
                and subject.entity_kind == entity_kind
                and subject.entity_id == entity_id
            ):
                return deepcopy(stored)
        return None

    async def append(
        self,
        project_id: str,
        expected_revision: int,
        manifest: AnimationProjectManifest,
    ) -> StoredManifest:
        _require_manifest_project(project_id, manifest)
        current_revision = self._current_revision.get(project_id)
        if current_revision is None:
            raise RevisionConflict(0)
        if current_revision != expected_revision:
            raise RevisionConflict(current_revision)
        now = datetime.now(UTC)
        next_revision = current_revision + 1
        serialize_checked(manifest, project_id=project_id, revision=next_revision)
        stored = StoredManifest(
            project_id=project_id,
            revision=next_revision,
            manifest_digest=manifest_digest(manifest),
            manifest=deepcopy(manifest),
            updated_at=now,
        )
        self._projects[project_id] = stored
        self._revisions.setdefault(project_id, []).append(deepcopy(stored))
        self._current_revision[project_id] = next_revision
        self._raw_revisions.setdefault(project_id, []).append(
            RawManifestRevision(
                revision=next_revision,
                manifest=stored.manifest.model_dump(mode="json"),
                created_at=now,
            )
        )
        return deepcopy(stored)

    async def create_release(
        self,
        *,
        project_id: str,
        expected_revision: int,
        name: str,
        manifest: AnimationProjectManifest,
    ) -> Release:
        _require_manifest_project(project_id, manifest)
        current = self._projects.get(project_id)
        if current is None:
            raise RevisionConflict(0)
        if current.revision != expected_revision:
            raise RevisionConflict(current.revision)
        if manifest_digest(manifest) != current.manifest_digest:
            raise ManifestMismatch("manifest does not match current revision digest")
        if (project_id, name) in self._release_names:
            raise DuplicateReleaseName(project_id, name)
        snapshot = deepcopy(current.manifest)
        release = Release(
            release_id=str(uuid4()),
            name=name,
            project_id=project_id,
            source_revision=current.revision,
            manifest_digest=current.manifest_digest,
            subject_revision_id=(
                snapshot.subject.current_revision.revision_id
                if snapshot.subject.current_revision else None
            ),
            motion_revision_pins=tuple(
                (motion.motion_id, motion.selected_candidate_id)
                for motion in snapshot.motions
                if motion.selected_candidate_id is not None
            ),
            quality_policy_versions=quality_policy_versions(snapshot),
            set_approval=snapshot.set_approval,
            manifest=snapshot,
        )
        release = release.model_copy(
            update={"signed_payload": canonical_release_payload_v1(release)}
        )
        release = release.model_copy(update={"release_digest": canonical_release_digest(release)})
        self._releases[release.release_id] = release
        self._release_names[(project_id, name)] = release.release_id
        return deepcopy(release)

    async def get_release(self, release_id: str) -> Release | None:
        release = self._releases.get(release_id)
        if release is None:
            return None
        _verify_release(release)
        return deepcopy(release)

    async def list_releases(self, project_id: str) -> tuple[Release, ...]:
        releases = tuple(
            release for release in self._releases.values()
            if release.project_id == project_id
        )
        for release in releases:
            _verify_release(release)
        return deepcopy(releases)

    async def save_export(self, record: ExportRecord) -> ExportRecord:
        existing = self._exports.get(record.export_id)
        if existing is not None:
            if _export_identity(existing) != _export_identity(record):
                raise ManifestMismatch(
                    "export identity already has different metadata"
                )
            return deepcopy(existing)
        self._exports[record.export_id] = deepcopy(record)
        return deepcopy(record)

    async def get_export(self, export_id: str) -> ExportRecord | None:
        record = self._exports.get(export_id)
        return deepcopy(record) if record is not None else None

    async def list_release_exports(
        self, release_id: str
    ) -> tuple[ExportRecord, ...]:
        records = sorted(
            (
                record for record in self._exports.values()
                if record.release_id == release_id
            ),
            key=lambda record: record.created_at,
            reverse=True,
        )
        return deepcopy(tuple(records))


def _export_identity(
    record: ExportRecord,
) -> tuple[str, str, str, str, str, str, int]:
    """The immutable identity of an export -- excludes `created_at`/`completed_at`
    so a retried save with a freshly regenerated record (same content, new
    timestamps) is treated as the same export rather than a conflict."""
    return (
        record.export_id, record.release_id, record.profile_name,
        record.profile_version, record.artifact_key, record.sha256,
        record.size_bytes,
    )


def _require_manifest_project(
    project_id: str,
    manifest: AnimationProjectManifest,
) -> None:
    if manifest.project_id != project_id:
        raise ManifestMismatch("manifest project_id does not match requested project")


def _verify_release(release: Release) -> None:
    try:
        expected_payload = canonical_release_payload_v1(release)
        expected = canonical_release_digest(release)
    except ValueError as exc:
        raise ReleaseIntegrityError(str(exc)) from exc
    if release.signed_payload != expected_payload:
        raise ReleaseIntegrityError("stored release payload does not match snapshot")
    if not release.release_digest or release.release_digest != expected:
        raise ReleaseIntegrityError("stored release digest does not match snapshot")
