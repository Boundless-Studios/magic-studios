"""The worker pool's coordinator drives Magic Studios' own asset store.

Production hands `GcsAssetStore` to `WorkerPoolService`, and the pool's lease mints
each job input's GET grant through it. Pool tests use the pool's own memory store,
so a method the Studio drops (as #233 dropped `create_read_url`) passes every test
and fails only on the first leased job with inputs. These keep the stores honest.
"""

import inspect

import pytest
from outbound_gpu_worker_pool.contracts import AssetStore as PoolAssetStore

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.plugins.gcs import GcsAssetStore


def _protocol_methods() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(PoolAssetStore).items()
        if inspect.iscoroutinefunction(member)
    }


@pytest.mark.parametrize("store", [GcsAssetStore, MemoryAssetStore])
def test_studio_asset_stores_provide_every_pool_asset_method(store) -> None:
    for name, expected in _protocol_methods().items():
        method = getattr(store, name, None)
        assert method is not None, f"{store.__name__} lacks {name}, which the pool lease calls"
        positional = [p for p in expected.parameters if p != "self"]
        assert list(inspect.signature(method).parameters)[1 : len(positional) + 1] == positional
