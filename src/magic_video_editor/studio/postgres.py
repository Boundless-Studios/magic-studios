"""PostgreSQL persistence for versioned Studio manifests."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

from magic_video_editor.studio.manifest import manifest_digest
from magic_video_editor.studio.write_guard import serialize_checked
from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    ExportRecord,
    Release,
    canonical_release_digest,
    canonical_release_payload_v1,
)
from magic_video_editor.studio.schema import STUDIO_SCHEMA_ADVISORY_LOCK
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


class ManifestUnreadable(RuntimeError):
    """A stored manifest revision failed `AnimationProjectManifest.model_validate`.

    Raised instead of the bare pydantic `ValidationError` so callers (the
    routes' exception handling, `app.py`) can answer a stable 503 pointing at
    the repair route rather than a 500. Revisions are append-only, so this
    always means a *previous* revision is still readable -- it is a signal to
    call `POST /studio/projects/{project_id}/repair`, not data loss."""

    def __init__(self, project_id: str | None, revision: int | None = None) -> None:
        self.project_id = project_id
        self.revision = revision
        super().__init__(
            f"manifest unreadable for project={project_id!r} revision={revision!r}"
        )


class PostgresStudioStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._owns_pool = False

    async def start(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._database_url,
                min_size=1,
                max_size=4,
            )
            self._owns_pool = True
        try:
            async with self._pool.acquire() as connection:
                migration = files("magic_video_editor.migrations").joinpath(
                    "002_studio_manifests.sql"
                ).read_text()
                media_kind_backfill = files("magic_video_editor.migrations").joinpath(
                    "008_backfill_media_kind.sql"
                ).read_text()
                project_owner = files("magic_video_editor.migrations").joinpath(
                    "009_studio_project_owner.sql"
                ).read_text()
                release_export_timestamps = files(
                    "magic_video_editor.migrations"
                ).joinpath("010_release_export_timestamps.sql").read_text()
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1))",
                        STUDIO_SCHEMA_ADVISORY_LOCK,
                    )
                    await connection.execute(migration)
                    await _backfill_release_digests(connection)
                    await connection.execute(media_kind_backfill)
                    await connection.execute(project_owner)
                    await connection.execute(release_export_timestamps)
        except Exception as exc:
            if self._pool is not None and self._owns_pool:
                try:
                    await self._pool.close()
                except Exception as close_exc:
                    exc.add_note(
                        f"pool close failed during rollback: {close_exc}"
                    )
                finally:
                    self._pool = None
                    self._owns_pool = False
            raise

    async def stop(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
            self._owns_pool = False

    async def create(self, manifest: AnimationProjectManifest) -> StoredManifest:
        snapshot = deepcopy(manifest)
        digest = manifest_digest(snapshot)
        updated_at = datetime.now(UTC)
        try:
            async with self._require_pool().acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO studio_projects (
                            project_id, current_revision, updated_at, created_by
                        ) VALUES ($1, 1, $2, $3)
                        """,
                        snapshot.project_id,
                        updated_at,
                        snapshot.owner_id,
                    )
                    await connection.execute(
                        """
                        INSERT INTO studio_manifest_revisions (
                            project_id, revision, manifest_digest, manifest, created_at
                        ) VALUES ($1, $2, $3, $4::jsonb, $5)
                        """,
                        snapshot.project_id,
                        1,
                        digest,
                        _manifest_json(snapshot),
                        updated_at,
                    )
        except asyncpg.UniqueViolationError as exc:
            current_revision = await self._read_current_revision(snapshot.project_id)
            raise ProjectAlreadyExists(current_revision) from exc
        return StoredManifest(
            project_id=snapshot.project_id,
            revision=1,
            manifest_digest=digest,
            manifest=snapshot,
            updated_at=updated_at,
        )

    async def get(self, project_id: str) -> StoredManifest | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT p.project_id, p.current_revision AS revision,
                       r.manifest_digest, r.manifest, p.updated_at
                FROM studio_projects AS p
                JOIN studio_manifest_revisions AS r
                  ON r.project_id = p.project_id
                 AND r.revision = p.current_revision
                WHERE p.project_id = $1
                """,
                project_id,
            )
        return _stored_manifest(row) if row is not None else None

    async def list_revisions(self, project_id: str) -> tuple[StoredManifest, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT project_id, revision, manifest_digest, manifest,
                       created_at AS updated_at
                FROM studio_manifest_revisions
                WHERE project_id = $1
                ORDER BY revision ASC
                """,
                project_id,
            )
        return tuple(_stored_manifest(row) for row in rows)

    async def list_raw_revisions(
        self, project_id: str
    ) -> tuple[RawManifestRevision, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT revision, manifest, created_at
                FROM studio_manifest_revisions
                WHERE project_id = $1
                ORDER BY revision DESC
                """,
                project_id,
            )
        return tuple(
            RawManifestRevision(
                revision=row["revision"],
                manifest=_raw_revision_manifest(row["manifest"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    async def current_revision_number(self, project_id: str) -> int | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT current_revision
                FROM studio_projects
                WHERE project_id = $1
                """,
                project_id,
            )
        return int(row["current_revision"]) if row is not None else None

    async def find_by_external_subject(
        self,
        *,
        owner_id: str,
        system: str,
        entity_kind: str,
        entity_id: str,
    ) -> StoredManifest | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT p.project_id, p.current_revision AS revision,
                       r.manifest_digest, r.manifest, p.updated_at
                FROM studio_projects AS p
                JOIN studio_manifest_revisions AS r
                  ON r.project_id = p.project_id
                 AND r.revision = p.current_revision
                WHERE r.manifest ->> 'owner_id' = $1
                  AND r.manifest -> 'external_subject' ->> 'system' = $2
                  AND r.manifest -> 'external_subject' ->> 'entity_kind' = $3
                  AND r.manifest -> 'external_subject' ->> 'entity_id' = $4
                LIMIT 1
                """,
                owner_id,
                system,
                entity_kind,
                entity_id,
            )
        return _stored_manifest(row) if row is not None else None

    async def delete_project(self, project_id: str) -> None:
        # Children first: these foreign keys do not cascade. GPU telemetry does.
        async with self._require_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM studio_exports WHERE release_id IN "
                "(SELECT release_id FROM studio_releases WHERE project_id = $1)",
                project_id,
            )
            await connection.execute("DELETE FROM studio_releases WHERE project_id = $1", project_id)
            await connection.execute(
                "DELETE FROM studio_manifest_revisions WHERE project_id = $1", project_id
            )
            await connection.execute("DELETE FROM studio_projects WHERE project_id = $1", project_id)

    async def reassign_owner(self, previous_owner_id: str, new_owner_id: str) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE studio_projects SET created_by = $2 WHERE created_by = $1",
                previous_owner_id,
                new_owner_id,
            )

    async def list_project_owners(self) -> tuple[tuple[str, str], ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT p.project_id, r.manifest ->> 'owner_id' AS owner_id
                FROM studio_projects AS p
                JOIN studio_manifest_revisions AS r
                  ON r.project_id = p.project_id
                 AND r.revision = p.current_revision
                """
            )
        return tuple((row["project_id"], row["owner_id"] or "") for row in rows)

    async def list_projects(self, owner_id: str) -> tuple[ProjectRecord, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT p.project_id, p.current_revision AS revision,
                       r.manifest_digest, r.manifest, p.updated_at,
                       p.created_at, p.created_by
                FROM studio_projects AS p
                JOIN studio_manifest_revisions AS r
                  ON r.project_id = p.project_id
                 AND r.revision = p.current_revision
                WHERE r.manifest ->> 'owner_id' = $1
                ORDER BY p.updated_at DESC
                """,
                owner_id,
            )
        records = []
        for row in rows:
            try:
                stored = _stored_manifest(row)
            except ManifestUnreadable:
                raw = _raw_revision_manifest(row["manifest"])
                records.append(
                    ProjectRecord(
                        project_id=row["project_id"],
                        revision=row["revision"],
                        created_at=row["created_at"],
                        created_by=row["created_by"],
                        updated_at=row["updated_at"],
                        stored=None,
                        raw_name=raw.get("name") if isinstance(raw, dict) else None,
                        unreadable=True,
                    )
                )
                continue
            records.append(
                ProjectRecord(
                    project_id=stored.project_id,
                    revision=stored.revision,
                    created_at=row["created_at"],
                    created_by=row["created_by"],
                    updated_at=stored.updated_at,
                    stored=stored,
                )
            )
        return tuple(records)

    async def append(
        self,
        project_id: str,
        expected_revision: int,
        manifest: AnimationProjectManifest,
    ) -> StoredManifest:
        _require_manifest_project(project_id, manifest)
        snapshot = deepcopy(manifest)
        digest = manifest_digest(snapshot)
        updated_at = datetime.now(UTC)
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                current = await _lock_current_manifest(connection, project_id)
                current_revision = (
                    int(current["current_revision"])
                    if current is not None
                    else None
                )
                if current_revision is None:
                    raise RevisionConflict(0)
                if current_revision != expected_revision:
                    raise RevisionConflict(current_revision)
                next_revision = current_revision + 1
                payload = serialize_checked(
                    snapshot, project_id=project_id, revision=next_revision
                )
                await connection.execute(
                    """
                    INSERT INTO studio_manifest_revisions (
                        project_id, revision, manifest_digest, manifest, created_at
                    ) VALUES ($1, $2, $3, $4::jsonb, $5)
                    """,
                    project_id,
                    next_revision,
                    digest,
                    payload,
                    updated_at,
                )
                await connection.execute(
                    """
                    UPDATE studio_projects
                    SET current_revision = $2, updated_at = $3
                    WHERE project_id = $1
                    """,
                    project_id,
                    next_revision,
                    updated_at,
                )
        return StoredManifest(
            project_id=project_id,
            revision=next_revision,
            manifest_digest=digest,
            manifest=snapshot,
            updated_at=updated_at,
        )

    async def create_release(
        self,
        *,
        project_id: str,
        expected_revision: int,
        name: str,
        manifest: AnimationProjectManifest,
    ) -> Release:
        _require_manifest_project(project_id, manifest)
        release_id = str(uuid4())
        try:
            async with self._require_pool().acquire() as connection:
                async with connection.transaction():
                    current = await _lock_current_manifest(connection, project_id)
                    if current is None:
                        raise RevisionConflict(0)
                    current_revision = int(current["current_revision"])
                    if current_revision != expected_revision:
                        raise RevisionConflict(current_revision)
                    digest = str(current["manifest_digest"])
                    if manifest_digest(manifest) != digest:
                        raise ManifestMismatch(
                            "manifest does not match current revision digest"
                        )
                    raw_snapshot = _raw_manifest(current["manifest"])
                    snapshot = AnimationProjectManifest.model_validate(raw_snapshot)
                    release = _new_release(
                        release_id=release_id, name=name, project_id=project_id,
                        source_revision=current_revision, manifest_digest_value=digest,
                        snapshot=snapshot, raw_snapshot=raw_snapshot,
                    )
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO studio_releases (
                            project_id, name, source_revision, manifest_digest,
                            manifest_snapshot, release_id, release_digest,
                            release_digest_version
                            , release_signed_payload
                        ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::uuid, $7, $8, $9)
                        RETURNING release_id::text, created_at
                        """,
                        project_id,
                        name,
                        current_revision,
                        digest,
                        _raw_manifest_json(raw_snapshot),
                        release_id,
                        release.release_digest,
                        release.release_digest_version,
                        release.signed_payload,
                    )
        except asyncpg.UniqueViolationError as exc:
            if getattr(exc, "constraint_name", None) == (
                "studio_releases_project_id_name_key"
            ):
                raise DuplicateReleaseName(project_id, name) from exc
            raise
        created_at = inserted.get("created_at")
        return (
            release.model_copy(update={"created_at": created_at})
            if created_at is not None
            else release
        )

    async def get_release(self, release_id: str) -> Release | None:
        try:
            parsed_release_id = UUID(release_id)
        except ValueError:
            return None
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT release_id::text, project_id, source_revision, name,
                       manifest_digest, manifest_snapshot, release_digest,
                       release_digest_version, created_at
                       , release_signed_payload
                FROM studio_releases
                WHERE release_id = $1::uuid
                """,
                str(parsed_release_id),
            )
        return _release(row) if row is not None else None

    async def list_releases(self, project_id: str) -> tuple[Release, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """SELECT release_id::text, project_id, source_revision, name,
                          manifest_digest, manifest_snapshot, release_digest,
                          release_digest_version, created_at
                          , release_signed_payload
                   FROM studio_releases WHERE project_id = $1
                   ORDER BY created_at, release_id""",
                project_id,
            )
        return tuple(_release(row) for row in rows)

    async def save_export(self, record: ExportRecord) -> ExportRecord:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """INSERT INTO studio_exports (
                       export_id, release_id, profile_name, profile_version,
                       artifact_key, sha256, size_bytes, created_at, completed_at
                   ) VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT (export_id) DO UPDATE SET export_id = EXCLUDED.export_id
                   RETURNING export_id, release_id::text, profile_name,
                             profile_version, artifact_key, sha256, size_bytes,
                             created_at, completed_at""",
                record.export_id, record.release_id, record.profile_name,
                record.profile_version, record.artifact_key, record.sha256,
                record.size_bytes, record.created_at, record.completed_at,
            )
        stored = _export_record(row)
        if _export_identity(stored) != _export_identity(record):
            raise ManifestMismatch("export identity already has different metadata")
        return stored

    async def get_export(self, export_id: str) -> ExportRecord | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """SELECT export_id, release_id::text, profile_name,
                          profile_version, artifact_key, sha256, size_bytes,
                          created_at, completed_at
                   FROM studio_exports WHERE export_id = $1""",
                export_id,
            )
        return _export_record(row) if row is not None else None

    async def list_release_exports(
        self, release_id: str
    ) -> tuple[ExportRecord, ...]:
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(
                """SELECT export_id, release_id::text, profile_name,
                          profile_version, artifact_key, sha256, size_bytes,
                          created_at, completed_at
                   FROM studio_exports
                   WHERE release_id = $1::uuid
                   ORDER BY created_at DESC""",
                release_id,
            )
        return tuple(_export_record(row) for row in rows)

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL Studio store has not started")
        return self._pool

    async def _read_current_revision(self, project_id: str) -> int:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT current_revision
                FROM studio_projects
                WHERE project_id = $1
                """,
                project_id,
            )
        return int(row["current_revision"]) if row is not None else 0


async def _lock_current_manifest(connection: Any, project_id: str) -> Any | None:
    return await connection.fetchrow(
        """
        SELECT p.current_revision, r.manifest_digest, r.manifest
        FROM studio_projects AS p
        JOIN studio_manifest_revisions AS r
          ON r.project_id = p.project_id
         AND r.revision = p.current_revision
        WHERE p.project_id = $1
        FOR UPDATE OF p
        """,
        project_id,
    )


async def _backfill_release_digests(connection: Any) -> None:
    rows = await connection.fetch(
        """SELECT release_id::text, project_id, source_revision, name,
                  manifest_digest, manifest_snapshot, release_digest,
                  release_digest_version, created_at
           FROM studio_releases
           WHERE release_digest IS NULL OR release_digest_version IS NULL
              OR release_signed_payload IS NULL
           ORDER BY release_id
           FOR UPDATE"""
    )
    for row in rows:
        snapshot = _manifest(
            row["manifest_snapshot"],
            project_id=row["project_id"],
            revision=row["source_revision"],
        )
        release = _new_release(
            release_id=row["release_id"], name=row["name"],
            project_id=row["project_id"], source_revision=row["source_revision"],
            manifest_digest_value=row["manifest_digest"], snapshot=snapshot,
            digest_version=1, raw_snapshot=_raw_manifest(row["manifest_snapshot"]),
            created_at=row.get("created_at"),
        )
        await connection.execute(
            """UPDATE studio_releases
               SET release_digest = $2, release_digest_version = 1,
                   release_signed_payload = $3
               WHERE release_id = $1::uuid""",
            release.release_id, release.release_digest, release.signed_payload,
        )
    await connection.execute(
        "ALTER TABLE studio_releases ALTER COLUMN release_digest SET NOT NULL"
    )
    await connection.execute(
        "ALTER TABLE studio_releases ALTER COLUMN release_digest_version SET NOT NULL"
    )
    await connection.execute(
        "ALTER TABLE studio_releases ALTER COLUMN release_signed_payload SET NOT NULL"
    )
    await connection.execute(
        """DO $$ BEGIN
             ALTER TABLE studio_releases ADD CONSTRAINT
               studio_releases_digest_version_check
               CHECK (release_digest_version = 1);
           EXCEPTION WHEN duplicate_object THEN NULL;
           END $$"""
    )


def _require_manifest_project(
    project_id: str,
    manifest: AnimationProjectManifest,
) -> None:
    if manifest.project_id != project_id:
        raise ManifestMismatch("manifest project_id does not match requested project")


def _manifest_json(manifest: AnimationProjectManifest) -> str:
    return manifest.model_dump_json()


def _manifest(
    raw: Any,
    *,
    project_id: str | None = None,
    revision: int | None = None,
) -> AnimationProjectManifest:
    if isinstance(raw, str):
        raw = json.loads(raw)
    try:
        return AnimationProjectManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestUnreadable(project_id, revision) from exc


def _stored_manifest(row: Any) -> StoredManifest:
    return StoredManifest(
        project_id=row["project_id"],
        revision=row["revision"],
        manifest_digest=row["manifest_digest"],
        manifest=_manifest(
            row["manifest"], project_id=row["project_id"], revision=row["revision"]
        ),
        updated_at=row["updated_at"],
    )


def _raw_revision_manifest(raw: Any) -> dict[str, Any]:
    """Decode a `studio_manifest_revisions.manifest` jsonb value without
    validating it -- asyncpg returns `str` or `dict` depending on the
    installed codec (T-manifest-repair: `list_raw_revisions` must succeed
    even when the JSON fails `AnimationProjectManifest.model_validate`)."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _release(row: Any) -> Release:
    raw_snapshot = _raw_manifest(row["manifest_snapshot"])
    snapshot = AnimationProjectManifest.model_validate(raw_snapshot)
    try:
        release = _new_release(
            release_id=row["release_id"], name=row["name"],
            project_id=row.get("project_id", snapshot.project_id),
            source_revision=row.get("source_revision", 1),
            manifest_digest_value=row["manifest_digest"], snapshot=snapshot,
            digest_version=row.get("release_digest_version", 1),
            raw_snapshot=raw_snapshot,
            created_at=row.get("created_at"),
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseIntegrityError(str(exc)) from exc
    stored_digest = row.get("release_digest")
    stored_payload = row.get("release_signed_payload")
    if not stored_payload or stored_payload != release.signed_payload:
        raise ReleaseIntegrityError("stored release payload does not match snapshot")
    if not stored_digest or stored_digest != canonical_release_digest(
        release.model_copy(update={"signed_payload": stored_payload})
    ):
        raise ReleaseIntegrityError("stored release digest does not match snapshot")
    return release.model_copy(
        update={"release_digest": stored_digest, "signed_payload": stored_payload}
    )


def _new_release(
    *, release_id: str, name: str, project_id: str, source_revision: int,
    manifest_digest_value: str, snapshot: AnimationProjectManifest,
    digest_version: int = 1,
    raw_snapshot: object | None = None,
    created_at: datetime | None = None,
) -> Release:
    extra_fields: dict[str, Any] = (
        {"created_at": created_at} if created_at is not None else {}
    )
    release = Release(
        release_id=release_id, name=name, project_id=project_id,
        source_revision=source_revision, manifest_digest=manifest_digest_value,
        release_digest_version=digest_version,
        subject_revision_id=(snapshot.subject.current_revision.revision_id
                             if snapshot.subject.current_revision else None),
        motion_revision_pins=tuple(
            (motion.motion_id, motion.selected_candidate_id)
            for motion in snapshot.motions if motion.selected_candidate_id is not None
        ),
        quality_policy_versions=quality_policy_versions(snapshot),
        set_approval=snapshot.set_approval, manifest=snapshot,
        **extra_fields,
    )
    payload = canonical_release_payload_v1(release, raw_manifest=raw_snapshot)
    release = release.model_copy(update={"signed_payload": payload})
    return release.model_copy(update={"release_digest": canonical_release_digest(release)})


def _raw_manifest(raw: Any) -> dict[str, object]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ReleaseIntegrityError("release manifest snapshot must be a JSON object")
    return raw


def _raw_manifest_json(raw: dict[str, object]) -> str:
    """Serialize the exact raw primitive shape without adding model defaults."""
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def _export_record(row: Any) -> ExportRecord:
    extra_fields: dict[str, Any] = {}
    created_at = row.get("created_at")
    if created_at is not None:
        extra_fields["created_at"] = created_at
    completed_at = row.get("completed_at")
    if completed_at is not None:
        extra_fields["completed_at"] = completed_at
    return ExportRecord(
        export_id=row["export_id"], release_id=row["release_id"],
        profile_name=row["profile_name"], profile_version=row["profile_version"],
        artifact_key=row["artifact_key"], sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        **extra_fields,
    )


def _export_identity(
    record: ExportRecord,
) -> tuple[str, str, str, str, str, str, int]:
    """The immutable identity of an export -- excludes `created_at`/`completed_at`,
    which are DB-owned and must not defeat the idempotent-retry check on
    `save_export`'s `ON CONFLICT`."""
    return (
        record.export_id, record.release_id, record.profile_name,
        record.profile_version, record.artifact_key, record.sha256,
        record.size_bytes,
    )
