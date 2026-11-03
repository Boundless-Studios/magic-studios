"""Part B: deliver a release to the target's importer automatically.

`create_release` exports the target's own profile and delivers it through
the installed adapter when the project carries a matching `external_subject`,
recording an append-only `ReleaseDelivery` on the manifest either way --
delivery never blocks or fails the release itself. `POST
/studio/releases/{release_id}/deliveries/{target}` retries.
"""

import pytest
from studio.test_releases import PNG_SHEET, releasable_manifest
from studio.test_routes import _studio_client

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.manifest import set_external_subject
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import DeliveryStatus, ExternalSubject
from magic_video_editor.studio.service import DeliveryNotConfigured, StudioService
from magic_video_editor.targets import DeliveryRequest, DeliveryResult, TargetRegistry
from magic_video_editor.targets.example.adapter import ExampleTargetAdapter

EMBER_KNIGHT = ExternalSubject(
    system="example",
    entity_kind="character",
    entity_id="char-42",
    display_name="Ember Knight",
)


class _FailableExampleAdapter(ExampleTargetAdapter):
    """Wraps the reference adapter's real (in-memory) `deliver` with a
    switch to force a terminal failure, so the failure/retry paths can be
    exercised without a second adapter implementation."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.calls: list[bytes] = []

    async def deliver(self, delivery: DeliveryRequest) -> DeliveryResult:
        self.calls.append(delivery.archive)
        if self.fail:
            return DeliveryResult.failed(
                "importer rejected the package", retryable=False
            )
        return await super().deliver(delivery)


def _registry(adapter: ExampleTargetAdapter) -> TargetRegistry:
    registry = TargetRegistry()
    registry.register(adapter)
    return registry


async def _bound_store_and_release(
    adapter: ExampleTargetAdapter,
) -> tuple[StudioService, str]:
    manifest = set_external_subject(
        releasable_manifest(), external_subject=EMBER_KNIGHT
    )
    store = MemoryStudioStore()
    created = await store.create(manifest)
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    service = StudioService(
        store=store, assets=assets, target_registry=_registry(adapter)
    )
    release = await service.create_release(
        "project-1", created.revision, "example-release", "user-1"
    )
    return service, release.release_id


@pytest.fixture(autouse=True)
def _example_delivery_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_URL", "https://example.test/import"
    )
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_TOKEN", "test-token")


async def test_create_release_delivers_to_the_target_and_records_success() -> None:
    adapter = _FailableExampleAdapter()
    service, release_id = await _bound_store_and_release(adapter)

    assert len(adapter.calls) == 1
    delivery = await service.get_release_delivery(release_id, "user-1")
    assert delivery is not None
    assert delivery.target == "example"
    assert delivery.status is DeliveryStatus.DELIVERED
    assert delivery.summary == "1 motions, 1 bindings"
    assert delivery.error is None


async def test_create_release_records_failure_without_failing_the_release() -> None:
    adapter = _FailableExampleAdapter(fail=True)
    service, release_id = await _bound_store_and_release(adapter)

    delivery = await service.get_release_delivery(release_id, "user-1")
    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    assert "importer rejected" in (delivery.error or "")


async def test_create_release_without_external_subject_never_calls_the_importer() -> (
    None
):
    manifest = releasable_manifest()
    store = MemoryStudioStore()
    created = await store.create(manifest)
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    adapter = _FailableExampleAdapter()
    service = StudioService(
        store=store, assets=assets, target_registry=_registry(adapter)
    )

    release = await service.create_release(
        "project-1", created.revision, "no-subject-release", "user-1"
    )

    assert adapter.calls == []
    assert await service.get_release_delivery(release.release_id, "user-1") is None


async def test_retry_delivery_appends_a_new_delivered_record() -> None:
    adapter = _FailableExampleAdapter(fail=True)
    service, release_id = await _bound_store_and_release(adapter)
    first = await service.get_release_delivery(release_id, "user-1")
    assert first is not None and first.status is DeliveryStatus.FAILED

    adapter.fail = False
    delivery = await service.retry_delivery(release_id, "example", "user-1")

    assert delivery.status is DeliveryStatus.DELIVERED
    matching = await service.list_release_deliveries(release_id, "user-1")
    assert [item.status for item in matching] == [
        DeliveryStatus.FAILED,
        DeliveryStatus.DELIVERED,
    ]


async def test_retry_delivery_without_configured_target_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_URL", raising=False)
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_TARGET_EXAMPLE_TOKEN", raising=False)
    manifest = set_external_subject(
        releasable_manifest(), external_subject=EMBER_KNIGHT
    )
    store = MemoryStudioStore()
    created = await store.create(manifest)
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    adapter = _FailableExampleAdapter()
    service = StudioService(
        store=store, assets=assets, target_registry=_registry(adapter)
    )
    release = await service.create_release(
        "project-1", created.revision, "example-release", "user-1"
    )

    with pytest.raises(DeliveryNotConfigured):
        await service.retry_delivery(release.release_id, "example", "user-1")


def test_create_release_route_returns_deliveries_and_retry_route_works() -> None:
    from magic_video_editor.memory import InMemoryRuntime, MemoryJobStore

    manifest = set_external_subject(
        releasable_manifest(), external_subject=EMBER_KNIGHT
    )
    store = MemoryStudioStore()
    import asyncio

    asyncio.run(store.create(manifest))
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    example_adapter = _FailableExampleAdapter(fail=True)
    service = StudioService(
        store=store, assets=assets, target_registry=_registry(example_adapter)
    )
    runtime = InMemoryRuntime(MemoryJobStore(), assets, studio_service=service)
    client = _studio_client(runtime, actor_id="user-1")

    created = client.post(
        "/studio/projects/project-1/releases",
        json={"expected_revision": 1, "name": "example-release"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["deliveries"][0]["status"] == "failed"
    assert body["deliveries"][0]["target"] == "example"

    example_adapter.fail = False
    retried = client.post(f"/studio/releases/{body['release_id']}/deliveries/example")
    assert retried.status_code == 200
    assert retried.json()["status"] == "delivered"

    listed = client.get("/studio/projects/project-1/releases")
    assert listed.status_code == 200
    assert listed.json()[0]["deliveries"][-1]["status"] == "delivered"
