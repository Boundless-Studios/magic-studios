"""The stores refuse to persist a manifest that would not read back."""

import logging

import pytest

from magic_video_editor.studio.manifest import create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.write_guard import ManifestUnwritable, serialize_checked


def _broken_manifest():
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    # Bypass validation the way a buggy in-memory transformation could.
    return manifest.model_copy(update={"motions": (None,)})


def test_serialize_checked_refuses_null_motions_and_logs_the_types(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="magic_video_editor.studio.write_guard"),
        pytest.raises(ManifestUnwritable) as raised,
    ):
        serialize_checked(_broken_manifest(), project_id="project-1", revision=2)
    assert raised.value.project_id == "project-1"
    assert raised.value.revision == 2
    record = next(r for r in caplog.records if "unreadable manifest" in r.getMessage())
    assert record.motions == [{"type": "builtins.NoneType", "repr": "None"}]
    assert "motions.0" in record.validation_error


def test_serialize_checked_returns_the_payload_for_a_valid_manifest() -> None:
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    payload = serialize_checked(manifest, project_id="project-1", revision=1)
    assert '"project_id":"project-1"' in payload


async def test_memory_store_refuses_to_append_an_unreadable_manifest() -> None:
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    created = await store.create(manifest)
    with pytest.raises(ManifestUnwritable):
        await store.append("project-1", created.revision, _broken_manifest())
    current = await store.get("project-1")
    assert current is not None
    assert current.revision == created.revision
