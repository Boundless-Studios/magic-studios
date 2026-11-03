import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from magic_video_editor.studio.manifest import create_project_manifest, manifest_digest
from magic_video_editor.studio.models import (
    ExportRecord,
    Release,
    canonical_release_digest,
    canonical_release_payload_v1,
)
from magic_video_editor.studio.postgres import PostgresStudioStore
from magic_video_editor.studio.store import (
    DuplicateReleaseName,
    ManifestMismatch,
    ProjectAlreadyExists,
    ReleaseIntegrityError,
    RevisionConflict,
)


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.in_transaction = True
        self.connection.transaction_entries += 1

    async def __aexit__(self, *_args: object) -> None:
        self.connection.in_transaction = False


class _NamedUniqueViolation(asyncpg.UniqueViolationError):
    @property
    def constraint_name(self) -> str:
        return "studio_releases_project_id_name_key"


class _ReleaseIdUniqueViolation(asyncpg.UniqueViolationError):
    @property
    def constraint_name(self) -> str:
        return "studio_releases_pkey"


class _Connection:
    def __init__(
        self,
        fetchrow_results: list[dict[str, object] | None | BaseException],
        execute_results: list[str | BaseException] | None = None,
        fetch_results: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.fetchrow_results = fetchrow_results
        self.execute_results = execute_results or []
        self.fetch_results = fetch_results or []
        self.calls: list[tuple[str, str, tuple[object, ...], bool]] = []
        self.in_transaction = False
        self.transaction_entries = 0

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def fetchrow(self, query: str, *args: object):
        self.calls.append(("fetchrow", query, args, self.in_transaction))
        result = self.fetchrow_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def execute(self, query: str, *args: object):
        self.calls.append(("execute", query, args, self.in_transaction))
        if self.execute_results:
            result = self.execute_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return "INSERT 0 1" if query.lstrip().startswith("INSERT") else "UPDATE 1"

    async def fetch(self, query: str, *args: object):
        self.calls.append(("fetch", query, args, self.in_transaction))
        return self.fetch_results.pop(0) if self.fetch_results else []


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)

    async def close(self) -> None:
        self.closed = True


class _CloseFailingPool(_Pool):
    async def close(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


async def test_append_locks_current_revision_and_only_advances_project_pointer() -> None:
    connection = _Connection([{"current_revision": 2}])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Changed")

    stored = await store.append("project-1", expected_revision=2, manifest=manifest)

    assert stored.revision == 3
    assert connection.transaction_entries == 1
    assert all(call[3] for call in connection.calls)
    lock_query = connection.calls[0][1]
    assert "current_revision" in lock_query
    assert "FOR UPDATE" in lock_query
    revision_inserts = [
        call for call in connection.calls
        if "INSERT INTO studio_manifest_revisions" in call[1]
    ]
    assert len(revision_inserts) == 1
    assert revision_inserts[0][2][1] == 3
    assert json.loads(revision_inserts[0][2][3]) == manifest.model_dump(mode="json")
    assert any("UPDATE studio_projects" in call[1] for call in connection.calls)
    assert not any(
        "UPDATE studio_manifest_revisions" in call[1]
        for call in connection.calls
    )


async def test_append_rejects_stale_revision_before_writing() -> None:
    connection = _Connection([{"current_revision": 4}])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Changed")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.append("project-1", expected_revision=3, manifest=manifest)

    assert exc_info.value.current_revision == 4
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][1]


async def test_append_rejects_missing_project_before_writing() -> None:
    connection = _Connection([None])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Changed")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.append("project-1", expected_revision=0, manifest=manifest)

    assert exc_info.value.current_revision == 0
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][1]


async def test_create_maps_duplicate_project_to_store_error() -> None:
    connection = _Connection(
        [{"current_revision": 2}],
        execute_results=[asyncpg.UniqueViolationError("duplicate project")],
    )
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Original")

    with pytest.raises(ProjectAlreadyExists) as exc_info:
        await store.create(manifest)

    assert exc_info.value.current_revision == 2
    assert any("INSERT INTO studio_projects" in call[1] for call in connection.calls)
    assert "SELECT current_revision" in connection.calls[-1][1]


async def test_append_rejects_manifest_project_id_mismatch_before_query() -> None:
    connection = _Connection([])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-2", "Wrong")

    with pytest.raises(ManifestMismatch):
        await store.append("project-1", expected_revision=1, manifest=manifest)

    assert connection.calls == []


async def test_list_projects_flags_an_unreadable_row_and_keeps_readable_ones() -> None:
    now = datetime.now(UTC)
    manifest = create_project_manifest("project-1", "Readable", owner_id="owner-1")
    readable_row = {
        "project_id": "project-1",
        "revision": 1,
        "manifest_digest": manifest_digest(manifest),
        "manifest": manifest.model_dump(mode="json"),
        "updated_at": now,
        "created_at": now,
        "created_by": "owner-1",
    }
    broken_row = {
        "project_id": "project-2",
        "revision": 2,
        "manifest_digest": "irrelevant",
        "manifest": {
            "project_id": "project-2",
            "name": "Broken raw name",
            "motions": None,
        },
        "updated_at": now,
        "created_at": now,
        "created_by": "owner-1",
    }
    connection = _Connection([], fetch_results=[[readable_row, broken_row]])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    records = await store.list_projects("owner-1")

    assert len(records) == 2
    readable_record, broken_record = records
    assert readable_record.unreadable is False
    assert readable_record.stored is not None
    assert readable_record.project_id == "project-1"

    assert broken_record.unreadable is True
    assert broken_record.stored is None
    assert broken_record.raw_name == "Broken raw name"
    assert broken_record.project_id == "project-2"
    assert broken_record.revision == 2
    assert broken_record.created_by == "owner-1"


async def test_create_release_locks_source_revision_and_inserts_snapshot(monkeypatch) -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    durable = create_project_manifest("project-1", "Release snapshot")
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
        {"release_id": release_id},
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    monkeypatch.setattr(
        "magic_video_editor.studio.postgres.uuid4", lambda: UUID(release_id)
    )

    release = await store.create_release(
        project_id="project-1",
        expected_revision=2,
        name="v1",
        manifest=durable,
    )

    assert release.release_id == release_id
    assert connection.transaction_entries == 1
    assert "FOR UPDATE" in connection.calls[0][1]
    insert = next(
        call for call in connection.calls if "INSERT INTO studio_releases" in call[1]
    )
    assert insert[2][2] == 2
    assert "manifest_snapshot" in insert[1]
    assert json.loads(insert[2][4]) == durable.model_dump(mode="json")
    assert insert[2][6] == release.release_digest


async def test_create_release_returns_the_inserted_created_at(monkeypatch) -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    durable = create_project_manifest("project-1", "Release snapshot")
    inserted_at = datetime(2026, 1, 1, tzinfo=UTC)
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
        {"release_id": release_id, "created_at": inserted_at},
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    monkeypatch.setattr(
        "magic_video_editor.studio.postgres.uuid4", lambda: UUID(release_id)
    )

    release = await store.create_release(
        project_id="project-1", expected_revision=2, name="v1", manifest=durable,
    )

    assert release.created_at == inserted_at
    insert = next(
        call for call in connection.calls if "INSERT INTO studio_releases" in call[1]
    )
    assert "RETURNING release_id::text, created_at" in insert[1]


async def test_create_release_rejects_missing_project_before_writing() -> None:
    connection = _Connection([None])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Release snapshot")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.create_release(
            project_id="project-1",
            expected_revision=0,
            name="v1",
            manifest=manifest,
        )

    assert exc_info.value.current_revision == 0
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][1]


async def test_create_release_rejects_stale_revision_before_release_insert() -> None:
    durable = create_project_manifest("project-1", "Release snapshot")
    connection = _Connection([
        {
            "current_revision": 3,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-1", "Release snapshot")

    with pytest.raises(RevisionConflict) as exc_info:
        await store.create_release(
            project_id="project-1",
            expected_revision=2,
            name="v1",
            manifest=manifest,
        )

    assert exc_info.value.current_revision == 3
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][1]
    assert not any(
        "INSERT INTO studio_releases" in call[1]
        for call in connection.calls
    )


async def test_create_release_rejects_manifest_project_id_mismatch_before_query() -> None:
    connection = _Connection([])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    manifest = create_project_manifest("project-2", "Wrong")

    with pytest.raises(ManifestMismatch):
        await store.create_release(
            project_id="project-1",
            expected_revision=1,
            name="v1",
            manifest=manifest,
        )

    assert connection.calls == []


async def test_create_release_rejects_manifest_digest_mismatch_before_insert() -> None:
    durable = create_project_manifest("project-1", "Durable")
    caller_snapshot = durable.model_copy(update={"name": "Caller bytes"})
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(ManifestMismatch):
        await store.create_release(
            project_id="project-1",
            expected_revision=2,
            name="v1",
            manifest=caller_snapshot,
        )

    assert len(connection.calls) == 1
    assert not any(
        "INSERT INTO studio_releases" in call[1]
        for call in connection.calls
    )


async def test_create_release_maps_duplicate_name_to_store_error() -> None:
    durable = create_project_manifest("project-1", "Durable")
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
        _NamedUniqueViolation("duplicate release name"),
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(DuplicateReleaseName):
        await store.create_release(
            project_id="project-1",
            expected_revision=2,
            name="v1",
            manifest=durable,
        )


async def test_create_release_does_not_mislabel_release_id_collision() -> None:
    durable = create_project_manifest("project-1", "Durable")
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(durable),
            "manifest": durable.model_dump(mode="json"),
        },
        _ReleaseIdUniqueViolation("duplicate release id"),
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(_ReleaseIdUniqueViolation):
        await store.create_release(
            project_id="project-1", expected_revision=2, name="v1",
            manifest=durable,
        )

    assert any("INSERT INTO studio_releases" in call[1] for call in connection.calls)


async def test_get_release_returns_none_for_malformed_release_id_without_query() -> None:
    connection = _Connection([])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    assert await store.get_release("not-a-uuid") is None
    assert connection.calls == []


async def test_get_release_reads_the_immutable_manifest_snapshot() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Release snapshot")
    unsigned_release = Release(
        release_id=release_id, name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    stored_payload = canonical_release_payload_v1(unsigned_release)
    signed_release = unsigned_release.model_copy(
        update={"signed_payload": stored_payload}
    )
    stored_digest = canonical_release_digest(signed_release)
    connection = _Connection([
        {
            "release_id": release_id,
            "name": "v1",
            "manifest_digest": "a" * 64,
            "manifest_snapshot": manifest.model_dump(mode="json"),
            "project_id": "project-1",
            "source_revision": 1,
            "release_digest": stored_digest,
            "release_signed_payload": stored_payload,
        },
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    release = await store.get_release(release_id)

    assert release is not None
    assert release.manifest.name == "Release snapshot"
    assert "manifest_snapshot" in connection.calls[0][1]


async def test_get_release_preserves_a_stored_created_at() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Release snapshot")
    unsigned_release = Release(
        release_id=release_id, name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    stored_payload = canonical_release_payload_v1(unsigned_release)
    signed_release = unsigned_release.model_copy(
        update={"signed_payload": stored_payload}
    )
    stored_digest = canonical_release_digest(signed_release)
    stored_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    connection = _Connection([
        {
            "release_id": release_id,
            "name": "v1",
            "manifest_digest": "a" * 64,
            "manifest_snapshot": manifest.model_dump(mode="json"),
            "project_id": "project-1",
            "source_revision": 1,
            "release_digest": stored_digest,
            "release_signed_payload": stored_payload,
            "created_at": stored_created_at,
        },
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    release = await store.get_release(release_id)

    assert release is not None
    assert release.created_at == stored_created_at


async def test_get_release_loads_an_old_row_without_created_at() -> None:
    """A release row from before this column was populated still loads --
    the model defaults `created_at` rather than requiring the column."""
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Release snapshot")
    unsigned_release = Release(
        release_id=release_id, name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    stored_payload = canonical_release_payload_v1(unsigned_release)
    signed_release = unsigned_release.model_copy(
        update={"signed_payload": stored_payload}
    )
    stored_digest = canonical_release_digest(signed_release)
    connection = _Connection([
        {
            "release_id": release_id,
            "name": "v1",
            "manifest_digest": "a" * 64,
            "manifest_snapshot": manifest.model_dump(mode="json"),
            "project_id": "project-1",
            "source_revision": 1,
            "release_digest": stored_digest,
            "release_signed_payload": stored_payload,
        },
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    release = await store.get_release(release_id)

    assert release is not None
    assert isinstance(release.created_at, datetime)


async def test_get_release_rejects_a_digest_that_does_not_match_stored_snapshot() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Release snapshot")
    unsigned = Release(
        release_id=release_id, name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    payload = canonical_release_payload_v1(unsigned)
    connection = _Connection([{
        "release_id": release_id, "name": "v1", "project_id": "project-1",
        "source_revision": 1, "manifest_digest": "a" * 64,
        "manifest_snapshot": manifest.model_dump(mode="json"),
        "release_digest": "f" * 64,
        "release_signed_payload": payload,
    }])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(ReleaseIntegrityError, match="does not match snapshot"):
        await store.get_release(release_id)


async def test_get_release_rejects_unknown_digest_version() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Release snapshot")
    connection = _Connection([{
        "release_id": release_id, "name": "v1", "project_id": "project-1",
        "source_revision": 1, "manifest_digest": "a" * 64,
        "manifest_snapshot": manifest.model_dump(mode="json"),
        "release_digest": "f" * 64, "release_digest_version": 99,
    }])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    with pytest.raises(ReleaseIntegrityError, match="unsupported release digest version"):
        await store.get_release(release_id)


async def test_v1_payload_ignores_later_model_defaults_but_detects_raw_tampering() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Historical")
    raw_snapshot = manifest.model_dump(mode="json")
    raw_snapshot.pop("releases")  # Simulates a field/default added after v1 signing.
    unsigned = Release(
        release_id=release_id, name="v1", project_id="project-1",
        source_revision=1, manifest_digest="a" * 64, manifest=manifest,
    )
    payload = canonical_release_payload_v1(unsigned, raw_manifest=raw_snapshot)
    digest = canonical_release_digest(
        unsigned.model_copy(update={"signed_payload": payload})
    )
    base_row = {
        "release_id": release_id, "name": "v1", "project_id": "project-1",
        "source_revision": 1, "manifest_digest": "a" * 64,
        "manifest_snapshot": raw_snapshot, "release_digest": digest,
        "release_digest_version": 1, "release_signed_payload": payload,
    }
    store = PostgresStudioStore(
        "postgresql://unused", pool=_Pool(_Connection([base_row]))
    )
    assert (await store.get_release(release_id)).release_digest == digest

    tampered_snapshot = {**raw_snapshot, "name": "Tampered"}
    tampered_store = PostgresStudioStore(
        "postgresql://unused",
        pool=_Pool(_Connection([{**base_row, "manifest_snapshot": tampered_snapshot}])),
    )
    with pytest.raises(ReleaseIntegrityError, match="payload does not match"):
        await tampered_store.get_release(release_id)


async def test_create_persists_the_same_omitted_default_snapshot_it_signs(
    monkeypatch,
) -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Historical")
    raw_snapshot = manifest.model_dump(mode="json")
    raw_snapshot.pop("releases")
    connection = _Connection([
        {
            "current_revision": 2,
            "manifest_digest": manifest_digest(manifest),
            "manifest": raw_snapshot,
        },
        {"release_id": release_id},
    ])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))
    monkeypatch.setattr(
        "magic_video_editor.studio.postgres.uuid4", lambda: UUID(release_id)
    )

    created = await store.create_release(
        project_id="project-1", expected_revision=2, name="v1", manifest=manifest,
    )
    insert = next(
        call for call in connection.calls if "INSERT INTO studio_releases" in call[1]
    )
    persisted_snapshot = json.loads(insert[2][4])
    assert "releases" not in persisted_snapshot
    assert persisted_snapshot == raw_snapshot

    connection.fetchrow_results.append({
        "release_id": release_id, "name": "v1", "project_id": "project-1",
        "source_revision": 2, "manifest_digest": manifest_digest(manifest),
        "manifest_snapshot": persisted_snapshot,
        "release_digest": created.release_digest,
        "release_digest_version": created.release_digest_version,
        "release_signed_payload": created.signed_payload,
    })
    loaded = await store.get_release(release_id)

    assert loaded is not None
    assert loaded.release_digest == created.release_digest
    assert loaded.signed_payload == created.signed_payload


async def test_start_transactionally_backfills_old_release_digest_and_is_idempotent() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    manifest = create_project_manifest("project-1", "Legacy release")
    old_row = {
        "release_id": release_id, "name": "v1", "project_id": "project-1",
        "source_revision": 2, "manifest_digest": manifest_digest(manifest),
        "manifest_snapshot": manifest.model_dump(mode="json"),
        "release_digest": None, "release_digest_version": None,
    }
    connection = _Connection([], fetch_results=[[old_row], []])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    await store.start()
    await store.start()

    update = next(
        call for call in connection.calls
        if call[0] == "execute" and "SET release_digest = $2" in call[1]
    )
    assert update[3] is True
    assert update[2][0] == release_id
    assert len(update[2][1]) == 64
    assert sum("ALTER COLUMN release_digest SET NOT NULL" in call[1]
               for call in connection.calls) == 2
    assert all(
        call[3] is True for call in connection.calls
        if "release_digest" in call[1] or "pg_advisory_xact_lock" in call[1]
    )


def test_migration_preserves_revision_history_and_unique_release_names() -> None:
    migration = Path(
        "src/magic_video_editor/migrations/002_studio_manifests.sql"
    ).read_text()
    revision_table = migration.split(
        "CREATE TABLE IF NOT EXISTS studio_manifest_revisions", maxsplit=1
    )[1].split("CREATE TABLE IF NOT EXISTS studio_releases", maxsplit=1)[0]
    release_table = migration.split(
        "CREATE TABLE IF NOT EXISTS studio_releases", maxsplit=1
    )[1]

    assert "CREATE TABLE IF NOT EXISTS studio_projects" in migration
    assert "CREATE TABLE IF NOT EXISTS studio_manifest_revisions" in migration
    assert "PRIMARY KEY (project_id, revision)" in migration
    assert "manifest JSONB NOT NULL" in revision_table
    assert "CREATE TABLE IF NOT EXISTS studio_releases" in migration
    assert "release_digest TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS release_digest" in migration
    assert "ADD COLUMN IF NOT EXISTS release_signed_payload" in migration
    assert "source_revision INTEGER NOT NULL" in release_table
    assert "manifest_digest TEXT NOT NULL" in release_table
    assert "manifest_snapshot JSONB NOT NULL" in release_table
    assert "UNIQUE (project_id, name)" in release_table
    assert "UPDATE studio_manifest_revisions" not in migration


def test_project_owner_migration_adds_created_by_idempotently() -> None:
    migration = Path(
        "src/magic_video_editor/migrations/009_studio_project_owner.sql"
    ).read_text()

    assert "ALTER TABLE studio_projects" in migration
    assert "ADD COLUMN IF NOT EXISTS created_by TEXT" in migration


def test_postgres_store_runs_the_project_owner_migration_after_the_studio_schema() -> None:
    source = Path("src/magic_video_editor/studio/postgres.py").read_text()
    studio = source.index('"002_studio_manifests.sql"')
    project_owner = source.index('"009_studio_project_owner.sql"')
    assert studio < project_owner
    assert "await connection.execute(project_owner)" in source


def test_release_export_timestamps_migration_adds_completed_at_and_index() -> None:
    migration = Path(
        "src/magic_video_editor/migrations/010_release_export_timestamps.sql"
    ).read_text()

    assert "ALTER TABLE studio_exports" in migration
    assert "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ" in migration
    assert "CREATE INDEX IF NOT EXISTS studio_exports_release_id_created_at_idx" in migration
    assert "ON studio_exports (release_id, created_at DESC)" in migration


def test_postgres_store_runs_the_release_export_timestamps_migration_last() -> None:
    source = Path("src/magic_video_editor/studio/postgres.py").read_text()
    project_owner = source.index('"009_studio_project_owner.sql"')
    release_export_timestamps = source.index('"010_release_export_timestamps.sql"')
    assert project_owner < release_export_timestamps
    assert "await connection.execute(release_export_timestamps)" in source


async def test_start_closes_an_owned_pool_when_migration_fails(monkeypatch) -> None:
    connection = _Connection([], execute_results=[RuntimeError("migration failed")])
    pool = _Pool(connection)

    async def create_pool(*_args: object, **_kwargs: object) -> _Pool:
        return pool

    monkeypatch.setattr(
        "magic_video_editor.studio.postgres.asyncpg.create_pool",
        create_pool,
    )
    store = PostgresStudioStore("postgresql://unused")

    with pytest.raises(RuntimeError, match="migration failed"):
        await store.start()

    assert pool.closed is True
    assert store._pool is None
    assert store._owns_pool is False


async def test_start_preserves_migration_failure_when_owned_pool_close_also_fails(
    monkeypatch,
) -> None:
    connection = _Connection([], execute_results=[RuntimeError("migration failed")])
    pool = _CloseFailingPool(connection)

    async def create_pool(*_args: object, **_kwargs: object) -> _CloseFailingPool:
        return pool

    monkeypatch.setattr(
        "magic_video_editor.studio.postgres.asyncpg.create_pool",
        create_pool,
    )
    store = PostgresStudioStore("postgresql://unused")

    with pytest.raises(RuntimeError, match="migration failed") as exc_info:
        await store.start()

    assert pool.closed is True
    assert store._pool is None
    assert store._owns_pool is False
    assert exc_info.value.__notes__ == ["pool close failed during rollback: close failed"]


async def test_save_export_persists_and_returns_both_timestamps() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    completed_at = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
    record = ExportRecord(
        export_id="e1", release_id="00000000-0000-0000-0000-000000000007",
        profile_name="png-json", profile_version="2", artifact_key="k",
        sha256="a" * 64, size_bytes=10, created_at=created_at,
        completed_at=completed_at,
    )
    row = {
        "export_id": record.export_id, "release_id": record.release_id,
        "profile_name": record.profile_name, "profile_version": record.profile_version,
        "artifact_key": record.artifact_key, "sha256": record.sha256,
        "size_bytes": record.size_bytes, "created_at": created_at,
        "completed_at": completed_at,
    }
    connection = _Connection([row])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    stored = await store.save_export(record)

    assert stored.created_at == created_at
    assert stored.completed_at == completed_at
    insert = next(
        call for call in connection.calls if "INSERT INTO studio_exports" in call[1]
    )
    assert insert[2][7] == created_at
    assert insert[2][8] == completed_at


async def test_save_export_retry_with_regenerated_timestamps_is_still_idempotent() -> None:
    """A retried export re-derives the same content-addressed `export_id` but
    a fresh `created_at`/`completed_at` -- that must not look like a conflict
    against the immutable row Postgres already returned via `ON CONFLICT`."""
    original_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    original = ExportRecord(
        export_id="e1", release_id="00000000-0000-0000-0000-000000000007",
        profile_name="png-json", profile_version="2", artifact_key="k",
        sha256="a" * 64, size_bytes=10, created_at=original_created_at,
        completed_at=original_created_at,
    )
    retried = original.model_copy(
        update={
            "created_at": datetime(2026, 2, 1, tzinfo=UTC),
            "completed_at": datetime(2026, 2, 1, tzinfo=UTC),
        }
    )
    row = {
        "export_id": original.export_id, "release_id": original.release_id,
        "profile_name": original.profile_name,
        "profile_version": original.profile_version,
        "artifact_key": original.artifact_key, "sha256": original.sha256,
        "size_bytes": original.size_bytes, "created_at": original_created_at,
        "completed_at": original_created_at,
    }
    connection = _Connection([row])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    stored = await store.save_export(retried)

    assert stored.created_at == original_created_at


async def test_get_export_loads_an_old_row_without_completed_at() -> None:
    row = {
        "export_id": "e1", "release_id": "00000000-0000-0000-0000-000000000007",
        "profile_name": "png-json", "profile_version": "2", "artifact_key": "k",
        "sha256": "a" * 64, "size_bytes": 10,
    }
    connection = _Connection([row])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    record = await store.get_export("e1")

    assert record is not None
    assert record.completed_at is None
    assert isinstance(record.created_at, datetime)


async def test_list_release_exports_orders_newest_first_via_sql() -> None:
    release_id = "00000000-0000-0000-0000-000000000007"
    rows = [
        {
            "export_id": "e2", "release_id": release_id, "profile_name": "gaia",
            "profile_version": "1", "artifact_key": "k2", "sha256": "b" * 64,
            "size_bytes": 1, "created_at": datetime(2026, 1, 2, tzinfo=UTC),
            "completed_at": datetime(2026, 1, 2, tzinfo=UTC),
        },
        {
            "export_id": "e1", "release_id": release_id, "profile_name": "png-json",
            "profile_version": "2", "artifact_key": "k1", "sha256": "a" * 64,
            "size_bytes": 1, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "completed_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    connection = _Connection([], fetch_results=[rows])
    store = PostgresStudioStore("postgresql://unused", pool=_Pool(connection))

    records = await store.list_release_exports(release_id)

    assert [record.export_id for record in records] == ["e2", "e1"]
    fetch = next(
        call for call in connection.calls if "FROM studio_exports" in call[1]
    )
    assert "ORDER BY created_at DESC" in fetch[1]
    assert fetch[2] == (release_id,)
