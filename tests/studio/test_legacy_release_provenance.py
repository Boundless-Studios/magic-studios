"""Persisted desktop releases must survive newly defaulted candidate fields."""

import json
from hashlib import sha256

import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import Release, canonical_release_payload_v1
from magic_video_editor.studio.service import StudioService
from magic_video_editor.studio.store import ReleaseIntegrityError
from .test_releases import releasable_manifest


async def persisted_release(provenance):
    store = MemoryStudioStore()
    created = await store.create(releasable_manifest())
    release = await StudioService(store=store, assets=MemoryAssetStore()).create_release(
        'project-1', created.revision, 'v1', 'user-1',
    )
    raw = release.model_dump(mode='json')
    for motion in raw['manifest']['motions']:
        for candidate in motion['candidates']:
            candidate.pop('execution_provenance', None)
            if provenance is not None:
                candidate['execution_provenance'] = provenance
    # Frozen signed bytes are derived from the genuine persisted primitives,
    # before the current model has had a chance to supply any missing defaults.
    raw['signed_payload'] = canonical_release_payload_v1(release, raw_manifest=raw['manifest'])
    raw['release_digest'] = sha256(raw['signed_payload'].encode()).hexdigest()
    return store, raw


@pytest.mark.parametrize('provenance', [None, {}, {'worker_id': 'worker-1', 'recipe_sha256': 'b' * 64}])
async def test_persisted_release_preserves_absent_empty_and_actual_provenance(provenance):
    store, raw = await persisted_release(provenance)
    loaded = Release.model_validate(raw)
    assert canonical_release_payload_v1(loaded) == raw['signed_payload']
    store._releases[loaded.release_id] = loaded
    assert (await store.get_release(loaded.release_id)).release_digest == raw['release_digest']
    assert await store.list_releases('project-1') == (loaded,)
    reloaded = Release.model_validate_json(loaded.model_dump_json())
    store._releases[loaded.release_id] = reloaded
    assert (await store.get_release(loaded.release_id)).signed_payload == raw['signed_payload']
    candidate = json.loads(reloaded.model_dump_json())['manifest']['motions'][0]['candidates'][0]
    assert ('execution_provenance' in candidate) == (provenance is not None)


@pytest.mark.parametrize('mutation', ['provenance', 'view_snapshot'])
async def test_legacy_compatibility_does_not_hide_provenance_or_view_tampering(mutation):
    store, raw = await persisted_release(None)
    if mutation == 'provenance':
        raw['manifest']['motions'][0]['candidates'][0]['execution_provenance'] = {'worker_id': 'forged'}
    else:
        reference = {'revision_id': 'ref-1', 'key': 'sources/studio/project-1/views/N/references/a.png',
                     'sha256': 'b' * 64, 'width': 64, 'height': 64}
        raw['manifest'].update(sprite_layout='isometric', views=[{'view_id': 'N', 'name': 'North', 'reference': reference}])
        raw['manifest']['motions'][0]['specification'].update(view_id='N', view_reference=reference)
    loaded = Release.model_validate(raw)
    store._releases[loaded.release_id] = loaded
    with pytest.raises(ReleaseIntegrityError, match='payload does not match snapshot'):
        await store.get_release(loaded.release_id)


async def test_new_view_release_reference_remains_signed_after_round_trip():
    store, raw = await persisted_release({})
    reference = {'revision_id': 'ref-1', 'key': 'sources/studio/project-1/views/N/references/a.png',
                 'sha256': 'b' * 64, 'width': 64, 'height': 64}
    raw['manifest'].update(sprite_layout='isometric', views=[{'view_id': 'N', 'name': 'North', 'reference': reference}])
    raw['manifest']['motions'][0]['specification'].update(view_id='N', view_reference=dict(reference))
    unsigned = Release.model_validate(raw)
    raw['signed_payload'] = canonical_release_payload_v1(unsigned)
    raw['release_digest'] = sha256(raw['signed_payload'].encode()).hexdigest()
    loaded = Release.model_validate(raw)
    store._releases[loaded.release_id] = loaded
    assert await store.get_release(loaded.release_id) == loaded
    raw['manifest']['views'][0]['reference']['sha256'] = 'c' * 64
    store._releases[loaded.release_id] = Release.model_validate(raw)
    with pytest.raises(ReleaseIntegrityError, match='payload does not match snapshot'):
        await store.list_releases('project-1')
