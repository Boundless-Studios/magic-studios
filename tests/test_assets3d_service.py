"""3D assets through their routes: sources, own meshes, review, selection, release."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.assets3d import release as packs
from magic_video_editor.assets3d import service as assets3d_service
from magic_video_editor.assets3d.models import Asset3DCandidate, Asset3DGate
from magic_video_editor.assets3d.routes import create_assets3d_router
from magic_video_editor.assets3d.service import Asset3DError, Assets3DService
from magic_video_editor.assets3d.stages import DISABLED
from magic_video_editor.assets3d.store import MemoryAssets3DStore
from magic_video_editor.contracts import require_deletable_prefix
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.accounts import AccountPolicy, AccountTier
from magic_video_editor.studio.routes import studio_actor_id

sys.path.insert(0, str(Path(__file__).parent))
from test_assets3d_media import glb, triangle  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"front picture"
JPEG = b"\xff\xd8\xff\xe0" + b"back picture"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"left"
API_KEY = {"Authorization": "Bearer msk_test"}


def build(*, public=False):
    assets = MemoryAssetStore()

    async def tier(owner):
        return AccountTier.PUBLIC if public else AccountTier.INVITED

    service = Assets3DService(MemoryAssets3DStore(), assets, account_policy=AccountPolicy(tier_for=tier))
    app = FastAPI()
    app.include_router(create_assets3d_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    return TestClient(app), service, assets


def make_asset(client, name="Crate", kind="prop") -> dict:
    created = client.post("/studio/3d", json={"name": name, "kind": kind})
    assert created.status_code == 200
    return created.json()


def granted(client, assets, id, content, content_type) -> dict:
    grant = client.post(f"/studio/3d/{id}/uploads", json={"content_type": content_type}).json()
    asyncio.run(assets.write_once(grant["asset_key"], content, content_type))
    return grant


def add_mesh(client, assets, id, content=None, **kwargs) -> dict:
    grant = granted(client, assets, id, content if content is not None else glb(), "model/gltf-binary")
    return client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": grant["revision"], "asset_key": grant["asset_key"], "file_name": "crate.glb",
    }, **kwargs)


def keep(client, id, asset, candidate_id, **kwargs) -> dict:
    reviewed = client.post(f"/studio/3d/{id}/candidates/{candidate_id}/review",
                           json={"expected_revision": asset["revision"], "outcome": "keep"}, **kwargs)
    assert reviewed.status_code == 200, reviewed.text
    return reviewed.json()


def select(client, id, asset, **selections):
    return client.put(f"/studio/3d/{id}/selections",
                      json={"expected_revision": asset["revision"], "selections": selections})


def add_clean(service, id, parent_id, *, gate=None) -> Asset3DCandidate:
    """An engine `clean` candidate, as a later part's stage completion would add it."""
    async def run():
        asset = await service.store.get(id)
        content = glb(triangle(), binary=b"\1" * 8)
        key = f"outputs/assets3d/{id}/candidates/{sha256(content).hexdigest()}.glb"
        await service.assets.write_once(key, content, "model/gltf-binary")
        candidate = Asset3DCandidate(
            candidate_id=f"clean-{len(asset.candidates)}", stage="clean", parent_id=parent_id, origin="engine",
            asset_key=key, sha256=sha256(content).hexdigest(), byte_length=len(content),
            gate=gate, created_at="2026-11-02T00:00:00+00:00",
            provenance={"capability_id": "mesh.blender.clean.v1",
                        "models": [{"id": "blender-4.2", "licence": "GPL-3.0"}]},
        )
        asset.candidates.append(candidate)
        await service.save(asset, asset.revision)
        return candidate
    return asyncio.run(run())


def test_capabilities_say_the_engine_stages_are_unavailable_for_every_tier():
    for public in (False, True):
        client, *_ = build(public=public)
        caps = client.get("/studio/3d/capabilities").json()
        assert caps["account_tier"] == ("public" if public else "invited")
        assert caps["stages"] == {
            "mesh": {"available": False, "reason": DISABLED},
            "clean": {"available": False, "reason": DISABLED},
        }
        assert caps["upload_available"] and caps["own_mesh_available"]
        assert caps["viewer_available"] and caps["release_available"]
        assert caps["max_model_bytes"] == 200 * 1024 * 1024
        assert caps["max_candidates_per_parent"] == 8 and caps["max_sources"] == 4
        assert "model/gltf-binary" in caps["upload_types"]


def test_public_accounts_bring_a_mesh_and_release_it():
    client, service, assets = build(public=True)
    asset = make_asset(client)
    id = asset["asset_id"]
    added = add_mesh(client, assets, id)
    assert added.status_code == 200
    asset = added.json()
    cid = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, cid)
    asset = select(client, id, asset, mesh=cid).json()
    released = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert released.status_code == 200


def test_source_uploads_are_sniffed_and_a_role_is_replaced_by_a_new_revision():
    client, service, assets = build()
    asset = make_asset(client)
    id = asset["asset_id"]
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/gif"}).status_code == 422
    grant = granted(client, assets, id, PNG, "image/png")
    assert assets.upload_limits[grant["asset_key"]] == 20 * 1024 * 1024
    assert grant["upload_headers"] == {"x-goog-content-length-range": f"0,{20 * 1024 * 1024}"}
    asset = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": grant["revision"], "role": "front", "asset_key": grant["asset_key"], "file_name": "f.png",
    }).json()
    front = asset["sources"][0]
    assert front["media_type"] == "image/png" and front["sha256"] == sha256(PNG).hexdigest()
    assert front["provenance"] == {"file_name": "f.png", "uploaded_by": "session"}
    # The same picture again for the same role is not a change.
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": asset["revision"], "role": "front", "asset_key": grant["asset_key"],
    }).status_code == 409
    # The bytes decide the type: a GLB is not a picture, a mislabelled upload is refused.
    model = granted(client, assets, id, glb(), "model/gltf-binary")
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": model["revision"], "role": "back", "asset_key": model["asset_key"],
    }).status_code == 422
    lying = granted(client, assets, id, JPEG, "image/png")
    refused = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": lying["revision"], "role": "back", "asset_key": lying["asset_key"],
    })
    assert refused.status_code == 422 and "image/jpeg" in refused.json()["detail"]
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": lying["revision"], "role": "top", "asset_key": lying["asset_key"],
    }).status_code == 422
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": lying["revision"], "role": "back", "asset_key": "uploads/assets3d/x/y",
    }).status_code == 403
    back = granted(client, assets, id, JPEG, "image/jpeg")
    asset = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": back["revision"], "role": "back", "asset_key": back["asset_key"],
    }, headers=API_KEY).json()
    assert [s["role"] for s in asset["sources"]] == ["front", "back"]
    assert asset["sources"][1]["provenance"]["uploaded_by"] == "api_key"
    replacement = granted(client, assets, id, WEBP, "image/webp")
    before = asset["revision"]
    asset = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": replacement["revision"], "role": "front", "asset_key": replacement["asset_key"],
    }).json()
    assert asset["revision"] == replacement["revision"] + 1 > before
    assert [(s["role"], s["media_type"]) for s in asset["sources"]] == [("front", "image/webp"), ("back", "image/jpeg")]
    # The replaced picture stays stored (and counted) until the asset is deleted.
    assert asyncio.run(assets.describe(grant["asset_key"])) is not None
    assert grant["asset_key"] in asyncio.run(service.store.accounted_keys(id))
    served = client.get(f"/studio/3d/{id}/assets/{replacement['asset_key']}")
    assert served.status_code == 200 and served.content == WEBP
    assert served.headers["content-type"] == "image/webp"
    # Only the current pictures are served.
    assert client.get(f"/studio/3d/{id}/assets/{grant['asset_key']}").status_code == 404


def test_own_mesh_upload_is_measured_and_recorded_as_an_upload():
    client, service, assets = build()
    id = make_asset(client, kind="character")["asset_id"]
    added = add_mesh(client, assets, id, headers=API_KEY)
    assert added.status_code == 200
    candidate = added.json()["candidates"][0]
    assert candidate["stage"] == "mesh" and candidate["origin"] == "upload" and candidate["parent_id"] is None
    assert candidate["media_type"] == "model/gltf-binary" and candidate["gate"] is None
    assert candidate["measured"]["triangles"] == 6 and candidate["measured"]["joints"] == 2
    assert candidate["provenance"] == {"file_name": "crate.glb", "uploaded_by": "api_key"}
    assert candidate["sha256"] == sha256(glb()).hexdigest()
    served = client.get(f"/studio/3d/{id}/assets/{candidate['asset_key']}")
    assert served.status_code == 200 and served.content == glb()
    # The same file twice is one candidate; a broken GLB says why.
    assert add_mesh(client, assets, id).status_code == 409
    truncated = add_mesh(client, assets, id, glb(binary=b"\2" * 8)[:-4])
    assert truncated.status_code == 422 and "truncated" in truncated.json()["detail"]
    assert add_mesh(client, assets, id, b"PK\x03\x04 not a model").status_code == 422
    # A GLB granted as a picture is refused, so it is never served as one.
    grant = granted(client, assets, id, glb(binary=b"\3" * 8), "image/png")
    assert client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": grant["revision"], "asset_key": grant["asset_key"],
    }).status_code == 422


def test_a_stage_holds_at_most_eight_candidates_per_parent():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    for index in range(8):
        assert add_mesh(client, assets, id, glb(binary=bytes([index]) * 8)).status_code == 200
    full = add_mesh(client, assets, id, glb(binary=b"\x09" * 8))
    assert full.status_code == 409 and "at most 8" in full.json()["detail"]


def test_size_limits_and_the_storage_cap():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    grant = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "model/gltf-binary"}).json()
    assert assets.upload_limits[grant["asset_key"]] == 200 * 1024 * 1024
    big = granted(client, assets, id, PNG + b"\0" * (20 * 1024 * 1024), "image/png")
    too_big = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": big["revision"], "role": "front", "asset_key": big["asset_key"],
    })
    assert too_big.status_code == 422 and "20 MB" in too_big.json()["detail"]

    async def full(owner, requested):
        raise Asset3DError("Storage limit reached. Free storage before starting new work.", 507)

    # At the cap no new grant or release starts, but a file already uploaded
    # (and reserved) can still be added: that only shrinks what it counts.
    model = granted(client, assets, id, glb(), "model/gltf-binary")
    service.work_guard = full
    before = set(assets.assets)
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 507
    added = client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": model["revision"], "asset_key": model["asset_key"],
    })
    assert added.status_code == 200 and len(added.json()["candidates"]) == 1
    assert set(assets.assets) == before


def test_reviews_are_bound_to_the_file_hash_and_record_who_gave_them():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    candidate = asset["candidates"][0]
    cid = candidate["candidate_id"]
    stale = client.post(f"/studio/3d/{id}/candidates/{cid}/review",
                        json={"expected_revision": asset["revision"], "outcome": "keep", "sha256": "0" * 64})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "candidate_file_changed"
    reviewed = client.post(f"/studio/3d/{id}/candidates/{cid}/review", headers=API_KEY, json={
        "expected_revision": asset["revision"], "outcome": "keep", "note": "clean", "sha256": candidate["sha256"],
    }).json()
    review = reviewed["candidates"][0]["review"]
    assert review["sha256"] == candidate["sha256"] and review["actor_kind"] == "api_key"
    assert review["actor_id"] == "owner" and review["note"] == "clean"
    asset = select(client, id, reviewed, mesh=cid).json()
    assert asset["selections"] == {"mesh": cid}
    # Rejecting the selected candidate clears its selection.
    rejected = client.post(f"/studio/3d/{id}/candidates/{cid}/review",
                           json={"expected_revision": asset["revision"], "outcome": "reject"}).json()
    assert rejected["selections"] == {}
    assert rejected["candidates"][0]["review"]["actor_kind"] == "session"
    assert client.post(f"/studio/3d/{id}/candidates/nope/review",
                       json={"expected_revision": rejected["revision"], "outcome": "keep"}).status_code == 404


def test_selection_needs_kept_candidates_and_the_current_revision():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    cid = asset["candidates"][0]["candidate_id"]
    assert select(client, id, asset, mesh=cid).status_code == 409
    asset = keep(client, id, asset, cid)
    wrong_stage = select(client, id, asset, clean=cid)
    assert wrong_stage.status_code == 422
    stale = client.put(f"/studio/3d/{id}/selections",
                       json={"expected_revision": asset["revision"] - 1, "selections": {"mesh": cid}})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == asset["revision"]
    assert client.put(f"/studio/3d/{id}/selections",
                      json={"expected_revision": asset["revision"], "selections": {}}).status_code == 422
    asset = select(client, id, asset, mesh=cid).json()
    assert asset["selections"] == {"mesh": cid}
    cleared = client.put(f"/studio/3d/{id}/selections",
                         json={"expected_revision": asset["revision"], "selections": {"mesh": None}})
    assert cleared.status_code == 200 and cleared.json()["selections"] == {}


def test_the_chain_rule_for_selection_and_readiness():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    asset = add_mesh(client, assets, id, glb(binary=b"\5" * 8)).json()
    first, second = (c["candidate_id"] for c in asset["candidates"])
    asset = keep(client, id, asset, first)
    asset = keep(client, id, asset, second)
    clean = add_clean(service, id, first)
    asset = client.get(f"/studio/3d/{id}").json()
    asset = keep(client, id, asset, clean.candidate_id)
    # A clean made from the first mesh needs that mesh selected.
    refused = select(client, id, asset, clean=clean.candidate_id)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "parent_not_selected"
    refused = select(client, id, asset, mesh=second, clean=clean.candidate_id)
    assert refused.status_code == 409
    asset = select(client, id, asset, mesh=first, clean=clean.candidate_id).json()
    readiness = client.get(f"/studio/3d/{id}/readiness").json()
    assert readiness["ready"] is True
    assert readiness["model"] == {"stage": "clean", "candidate_id": clean.candidate_id}
    # Selecting a different parent leaves the clean selected; the chain is now mixed.
    asset = select(client, id, asset, mesh=second).json()
    assert asset["selections"] == {"mesh": second, "clean": clean.candidate_id}
    readiness = client.get(f"/studio/3d/{id}/readiness").json()
    assert readiness["ready"] is False
    assert [p["stage"] for p in readiness["problems"]] == ["clean"]
    assert "mixed" in readiness["problems"][0]["message"]
    assert client.post(f"/studio/3d/{id}/release",
                       json={"expected_revision": asset["revision"], "name": "v1"}).status_code == 409


def test_readiness_failures():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    readiness = client.get(f"/studio/3d/{id}/readiness").json()
    assert readiness == {
        "ready": False, "problems": [{"stage": None, "message": "Select a mesh to release."}],
        "model": None, "sources": 0,
    }
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    asset = select(client, id, asset, mesh=mesh).json()
    failed = add_clean(service, id, mesh, gate=Asset3DGate(name="clean", passed=False))
    asset = keep(client, id, client.get(f"/studio/3d/{id}").json(), failed.candidate_id)
    asset = select(client, id, asset, clean=failed.candidate_id).json()
    problems = client.get(f"/studio/3d/{id}/readiness").json()["problems"]
    assert problems == [{"stage": "clean", "message": "The selected clean candidate failed its clean gate."}]

    # A review given on a different file than the candidate holds no longer counts.
    async def tamper():
        stored = await service.store.get(id)
        stored.candidates[0].review.sha256 = "0" * 64
        await service.store.append(stored, stored.revision)

    asyncio.run(tamper())
    problems = client.get(f"/studio/3d/{id}/readiness").json()["problems"]
    assert {"stage": "mesh", "message": "The selected mesh candidate is no longer kept; review and select again."} in problems


def test_release_pack_contents_manifest_hashes_and_immutability():
    client, service, assets = build()
    asset = make_asset(client, name="Oak Crate!", kind="prop")
    id = asset["asset_id"]
    front = granted(client, assets, id, PNG, "image/png")
    asset = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": front["revision"], "role": "front", "asset_key": front["asset_key"], "file_name": "f.png",
    }).json()
    left = granted(client, assets, id, WEBP, "image/webp")
    asset = client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": left["revision"], "role": "left", "asset_key": left["asset_key"],
    }).json()
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]
    asset = keep(client, id, asset, mesh["candidate_id"])
    asset = select(client, id, asset, mesh=mesh["candidate_id"]).json()
    released = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert released.status_code == 200
    asset = released.json()
    release = asset["releases"][0]
    assert release["model_candidate_id"] == mesh["candidate_id"] and release["file_count"] == 3
    assert release["source_revision"] == asset["revision"] - 1
    assert client.post(f"/studio/3d/{id}/release",
                       json={"expected_revision": asset["revision"], "name": "v1"}).status_code == 409
    download = client.get(f"/studio/3d/{id}/releases/{release['release_id']}/download")
    assert download.status_code == 200
    assert f"3d-{release['release_id']}.zip" in download.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert sorted(archive.namelist()) == [
        "CREDITS.md", "manifest.json", "model/oak-crate.glb", "sources/front.png", "sources/left.webp",
    ]
    assert archive.read("model/oak-crate.glb") == glb()
    assert archive.getinfo("model/oak-crate.glb").compress_type == zipfile.ZIP_STORED
    manifest_bytes = archive.read("manifest.json")
    assert sha256(manifest_bytes).hexdigest() == release["manifest_sha256"]
    manifest = json.loads(manifest_bytes)
    assert manifest["schema_version"] == 1
    assert (manifest["asset_id"], manifest["name"], manifest["release_id"]) == (id, "Oak Crate!", release["release_id"])
    assert manifest["source_revision"] == release["source_revision"]
    for entry in manifest["files"]:
        content = archive.read(entry["path"])
        assert entry["sha256"] == sha256(content).hexdigest() and entry["bytes"] == len(content)
    [link] = manifest["chain"]
    assert link["stage"] == "mesh" and link["sha256"] == mesh["sha256"] == sha256(glb()).hexdigest()
    assert link["provenance"] == {"file_name": "crate.glb", "uploaded_by": "session"}
    assert link["review"]["reviewer"] == "person" and link["review"]["sha256"] == mesh["sha256"]
    assert link["gate"] is None and link["measured"]["triangles"] == 6
    # Who reviewed is said as a kind, never as an account id.
    assert "owner" not in manifest_bytes.decode().replace("owner's", "")
    credits = archive.read("CREDITS.md").decode()
    assert "the owner's own file" in credits and "`sources/front.png`" in credits
    # Create-once: a second write to its key changes nothing, and later edits leave it alone.
    assert asyncio.run(assets.write_once(release["asset_key"], b"tampered", "application/zip")) is False
    replacement = granted(client, assets, id, JPEG, "image/jpeg")
    client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": replacement["revision"], "role": "front", "asset_key": replacement["asset_key"],
    })
    again = client.get(f"/studio/3d/{id}/releases/{release['release_id']}/download")
    assert again.content == download.content
    assert client.get(f"/studio/3d/{id}/releases/nope/download").status_code == 404


def test_a_clean_release_ships_the_clean_model_and_credits_its_engine():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    clean = add_clean(service, id, mesh, gate=Asset3DGate(name="clean", passed=True))
    asset = keep(client, id, client.get(f"/studio/3d/{id}").json(), clean.candidate_id)
    asset = select(client, id, asset, mesh=mesh, clean=clean.candidate_id).json()
    asset = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"}).json()
    rid = asset["releases"][0]["release_id"]
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/studio/3d/{id}/releases/{rid}/download").content))
    assert archive.read("model/crate.glb") == asyncio.run(assets.read(clean.asset_key))
    manifest = json.loads(archive.read("manifest.json"))
    assert [c["stage"] for c in manifest["chain"]] == ["mesh", "clean"]
    assert manifest["model"]["candidate_id"] == clean.candidate_id
    assert manifest["chain"][1]["gate"]["passed"] is True
    assert "blender-4.2 (GPL-3.0)" in archive.read("CREDITS.md").decode()


def test_the_release_limit_counts_every_copy_and_the_manifest(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    picture = granted(client, assets, id, PNG, "image/png")
    asset = client.get(f"/studio/3d/{id}").json()
    for role in ("front", "back"):
        asset = client.post(f"/studio/3d/{id}/sources", json={
            "expected_revision": asset["revision"], "role": role, "asset_key": picture["asset_key"],
        }).json()
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    asset = select(client, id, asset, mesh=mesh).json()
    files = len(glb()) + 2 * len(PNG)
    # One stored picture, shipped twice: counting it once would have passed.
    monkeypatch.setattr(assets3d_service, "MAX_RELEASE_BYTES", files - 1)
    refused = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert refused.status_code == 413
    # Room for every file but not for the manifest and credits.
    monkeypatch.setattr(assets3d_service, "MAX_RELEASE_BYTES", files + 10)
    assert client.post(f"/studio/3d/{id}/release",
                       json={"expected_revision": asset["revision"], "name": "v1"}).status_code == 413
    monkeypatch.setattr(assets3d_service, "MAX_RELEASE_BYTES", files + 100_000)
    released = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert released.status_code == 200 and released.json()["releases"][0]["file_count"] == 3


def test_a_waiting_release_reads_no_files_until_the_slot_is_free(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    asset = select(client, id, asset, mesh=mesh).json()
    reads = []
    original = assets.read_limited

    async def recording(key, limit):
        reads.append(key)
        return await original(key, limit)

    monkeypatch.setattr(assets, "read_limited", recording)
    on_loop = []
    build_pack = packs.build_pack

    def off_loop(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return build_pack(*args, **kwargs)

    monkeypatch.setattr(packs, "build_pack", off_loop)

    async def scenario():
        await service.pack_slots.acquire()
        waiting = asyncio.create_task(service.release(id, "owner", asset["revision"], "v1"))
        for _ in range(20):
            await asyncio.sleep(0)
        # Another release holds the slot: this one has not loaded the model yet.
        assert reads == [] and not waiting.done()
        service.pack_slots.release()
        return await waiting

    released = asyncio.run(scenario())
    assert reads and released.releases[0].name == "v1"
    assert on_loop == [False]


def test_owner_isolation():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    key = asset["candidates"][0]["asset_key"]
    client.app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.get(f"/studio/3d/{id}").status_code == 404
    assert client.get(f"/studio/3d/{id}/assets/{key}").status_code == 404
    assert client.get(f"/studio/3d/{id}/readiness").status_code == 404
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 404
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Crate"}).status_code == 404
    assert client.get("/studio/3d").json() == []
    client.app.dependency_overrides[studio_actor_id] = lambda: "owner"
    assert [a["asset_id"] for a in client.get("/studio/3d").json()] == [id]
    # Only the asset's own files are served, not any key under its prefix.
    assert client.get(f"/studio/3d/{id}/assets/outputs/assets3d/{id}/nope").status_code == 404


def test_delete_and_erase_owner_remove_every_file_and_the_ledger():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]
    asset = keep(client, id, asset, mesh["candidate_id"])
    asset = select(client, id, asset, mesh=mesh["candidate_id"]).json()
    asset = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"}).json()
    release_key = asset["releases"][0]["asset_key"]
    stored = asyncio.run(service.store.stored_bytes("owner"))
    assert stored == len(glb()) + asyncio.run(assets.describe(release_key)).size
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "nope"}).status_code == 422
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Crate"}).status_code == 204
    assert client.get(f"/studio/3d/{id}").status_code == 404
    assert asyncio.run(assets.describe(mesh["asset_key"])) is None
    assert asyncio.run(assets.describe(release_key)) is None
    assert asyncio.run(service.store.stored_bytes("owner")) == 0

    kept = make_asset(client, name="Mine")["asset_id"]
    add_mesh(client, assets, kept)
    client.app.dependency_overrides[studio_actor_id] = lambda: "someone"
    theirs = make_asset(client, name="Theirs")["asset_id"]
    add_mesh(client, assets, theirs, glb(binary=b"\7" * 8))
    asyncio.run(service.erase_owner("owner"))
    assert asyncio.run(service.store.get(kept)) is None
    assert not any(key.startswith(f"uploads/assets3d/{kept}/") for key in assets.assets)
    assert asyncio.run(service.store.get(theirs)) is not None
    assert any(key.startswith(f"uploads/assets3d/{theirs}/") for key in assets.assets)


def test_the_asset_prefixes_are_deletable_and_nothing_wider():
    assert require_deletable_prefix("uploads/assets3d/abc-123/")
    assert require_deletable_prefix("outputs/assets3d/abc-123/")
    for prefix in ("uploads/assets3d/", "outputs/assets3d/abc/releases/", "outputs/assets3d/../x/"):
        with pytest.raises(ValueError):
            require_deletable_prefix(prefix)


def test_the_local_studio_and_account_deletion_include_3d_assets():
    from magic_video_editor.local_studio import create_local_studio_app, create_local_studio_runtime

    runtime = create_local_studio_runtime()
    assert isinstance(runtime.assets3d_service, Assets3DService)
    app = create_local_studio_app(runtime=runtime)
    paths = {route.path for route in app.routes}
    assert "/studio/3d/{id}/meshes" in paths and "/studio/3d/capabilities" in paths


def record_reads(monkeypatch, assets) -> list[str]:
    reads: list[str] = []
    for name in ("read", "read_limited", "read_range"):
        original = getattr(assets, name)

        def recording(key, *args, _original=original):
            reads.append(key)
            return _original(key, *args)

        monkeypatch.setattr(assets, name, recording)
    return reads


def test_waiting_mesh_registrations_read_nothing_until_a_media_slot_is_free(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    grant = granted(client, assets, id, glb(), "model/gltf-binary")
    reads = record_reads(monkeypatch, assets)

    async def scenario():
        for _ in range(2):
            await service.media.slots.acquire()
        # Twenty requests with the same key and revision: none holds file bytes
        # while both slots are taken.
        waiting = [
            asyncio.create_task(service.add_mesh(id, "owner", grant["revision"], grant["asset_key"], "", "session"))
            for _ in range(20)
        ]
        for _ in range(50):
            await asyncio.sleep(0)
        assert reads == [] and not any(task.done() for task in waiting)
        for _ in range(2):
            service.media.slots.release()
        return await asyncio.gather(*waiting, return_exceptions=True)

    results = asyncio.run(scenario())
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, Asset3DError) and r.status == 409 for r in results if isinstance(r, Exception))
    # Read by range in pieces, never whole.
    assert reads and set(reads) == {grant["asset_key"]}


def test_hashing_and_parsing_run_off_the_event_loop(monkeypatch):
    from magic_video_editor.assets3d import media

    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    on_loop: list[bool] = []
    for name in ("inspect_stored", "digest"):
        original = getattr(media, name)

        def recording(*args, _original=original, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            return _original(*args, **kwargs)

        monkeypatch.setattr(media, name, recording)
    assert add_mesh(client, assets, id).status_code == 200
    picture = granted(client, assets, id, PNG, "image/png")
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": picture["revision"], "role": "front", "asset_key": picture["asset_key"],
    }).status_code == 200
    assert on_loop == [False, False, False]


def test_an_oversized_or_mistyped_upload_is_refused_before_any_read(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    model = granted(client, assets, id, glb(), "model/gltf-binary")
    picture = granted(client, assets, id, PNG, "image/png")
    monkeypatch.setattr(assets3d_service, "MAX_MODEL_BYTES", len(glb()) - 1)
    reads = record_reads(monkeypatch, assets)
    too_big = client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": picture["revision"], "asset_key": model["asset_key"],
    })
    assert too_big.status_code == 422 and "larger than" in too_big.json()["detail"]
    wrong_type = client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": picture["revision"], "asset_key": picture["asset_key"],
    })
    assert wrong_type.status_code == 422 and "image/png" in wrong_type.json()["detail"]
    not_uploaded = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).json()
    assert client.post(f"/studio/3d/{id}/sources", json={
        "expected_revision": not_uploaded["revision"], "role": "front", "asset_key": not_uploaded["asset_key"],
    }).status_code == 404
    assert reads == []


def test_a_release_zip_is_counted_even_when_its_save_conflicts(monkeypatch):
    from magic_video_editor.assets3d.store import Conflict

    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    asset = select(client, id, asset, mesh=mesh).json()
    before = asyncio.run(service.store.stored_bytes("owner"))
    original = service.store.append

    async def edited_meanwhile(document, expected):
        raise Conflict(expected + 1)

    monkeypatch.setattr(service.store, "append", edited_meanwhile)
    refused = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert refused.status_code == 409
    monkeypatch.setattr(service.store, "append", original)
    [zip_key] = [key for key in assets.assets if key.startswith(f"outputs/assets3d/{id}/releases/")]
    assert zip_key in asyncio.run(service.store.accounted_keys(id))
    after = asyncio.run(service.store.stored_bytes("owner"))
    assert after == before + len(assets.assets[zip_key])
    # Deleting the asset removes the unrecorded zip with everything else.
    assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Crate"}).status_code == 204
    assert zip_key not in assets.assets and asyncio.run(service.store.stored_bytes("owner")) == 0


def test_unused_grants_are_capped_and_reserved_at_their_limit():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    limit = 200 * 1024 * 1024
    grants = [client.post(f"/studio/3d/{id}/uploads", json={"content_type": "model/gltf-binary"}).json()
              for _ in range(8)]
    # Nothing is stored yet, but each grant holds its full limit against the cap.
    assert asyncio.run(service.store.stored_bytes("owner")) == 8 * limit
    refused = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "too_many_pending_uploads"
    # Using one turns its reservation into the file's real size and frees a slot.
    asyncio.run(assets.write_once(grants[0]["asset_key"], glb(), "model/gltf-binary"))
    asset = client.get(f"/studio/3d/{id}").json()
    asset = client.post(f"/studio/3d/{id}/meshes", json={
        "expected_revision": asset["revision"], "asset_key": grants[0]["asset_key"],
    }).json()
    assert grants[0]["asset_key"] not in asset["pending_uploads"] and len(asset["pending_uploads"]) == 7
    assert asyncio.run(service.store.stored_bytes("owner")) == 7 * limit + len(glb())
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 200

    # With a storage guard on the ledger, reservations alone reach the cap.
    async def capped(owner, requested):
        if await service.store.stored_bytes(owner) >= 1024 * 1024 * 1024:
            raise Asset3DError("Storage limit reached. Free storage before starting new work.", 507)

    service.work_guard = capped
    other = make_asset(client, name="Other")["asset_id"]
    assert client.post(f"/studio/3d/{other}/uploads", json={"content_type": "image/png"}).status_code == 507


def test_expired_grants_release_their_reservation(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    grants = [client.post(f"/studio/3d/{id}/uploads", json={"content_type": "model/gltf-binary"}).json()
              for _ in range(8)]
    uploaded, never = grants[0]["asset_key"], grants[1]["asset_key"]
    asyncio.run(assets.write_once(uploaded, glb(), "model/gltf-binary"))
    # Two grants are older than the signed URL's lifetime plus the margin.

    async def age():
        asset = await service.store.get(id)
        for key in (uploaded, never):
            asset.pending_uploads[key] = "2026-01-01T00:00:00+00:00"
        await service.store.append(asset, asset.revision)

    asyncio.run(age())
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 200
    asset = client.get(f"/studio/3d/{id}").json()
    # The never-used grant is gone; the uploaded file stays, counted at its real size.
    assert never not in asset["upload_keys"] and never not in asset["pending_uploads"]
    assert uploaded in asset["upload_keys"] and uploaded not in asset["pending_uploads"]
    assert len(asset["pending_uploads"]) == 7
    accounted = asyncio.run(service.store.accounted_keys(id))
    assert never not in accounted and uploaded in accounted
    limit = 200 * 1024 * 1024
    assert asyncio.run(service.store.stored_bytes("owner")) == 6 * limit + 20 * 1024 * 1024 + len(glb())
    # The uploaded file can still be added.
    added = client.post(f"/studio/3d/{id}/meshes", json={"expected_revision": asset["revision"], "asset_key": uploaded})
    assert added.status_code == 200


def test_an_asset_takes_a_bounded_number_of_uploads(monkeypatch):
    monkeypatch.setattr(assets3d_service, "MAX_UPLOADS", 3)
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    for index in range(3):
        add_mesh(client, assets, id, glb(binary=bytes([index]) * 8))
    refused = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "too_many_uploads"


GIB = 1024 * 1024 * 1024


def ledger_guard(service, cap):
    """The app's storage guard, reading the 3D ledger like a database would (it yields)."""
    async def guard(owner, requested):
        await asyncio.sleep(0)
        if await service.store.stored_bytes(owner) >= cap:
            raise Asset3DError("Storage limit reached. Free storage before starting new work.", 507)
    return guard


def test_expired_grants_on_any_asset_are_released_before_the_cap_refuses():
    client, service, assets = build()
    stale = make_asset(client, name="Stale")["asset_id"]
    for _ in range(6):
        assert client.post(f"/studio/3d/{stale}/uploads", json={"content_type": "model/gltf-binary"}).status_code == 200

    async def age():
        asset = await service.store.get(stale)
        asset.pending_uploads = {key: "2026-01-01T00:00:00+00:00" for key in asset.pending_uploads}
        await service.store.append(asset, asset.revision)

    asyncio.run(age())
    # Six unusable 200 MB reservations put the owner over 1 GiB.
    assert asyncio.run(service.store.stored_bytes("owner")) > GIB
    service.work_guard = ledger_guard(service, GIB)
    fresh = make_asset(client, name="Fresh")["asset_id"]
    granted_now = client.post(f"/studio/3d/{fresh}/uploads", json={"content_type": "image/png"})
    assert granted_now.status_code == 200
    assert client.get(f"/studio/3d/{stale}").json()["pending_uploads"] == {}
    assert asyncio.run(service.store.stored_bytes("owner")) == 20 * 1024 * 1024


def test_concurrent_grants_never_reserve_past_the_allowance():
    client, service, assets = build()
    ids = [make_asset(client, name=f"A{i}")["asset_id"] for i in range(2)]
    service.work_guard = ledger_guard(service, GIB)

    async def allowance(owner):
        await asyncio.sleep(0)
        return GIB

    service.storage_allowance = allowance

    async def burst():
        return await asyncio.gather(*(
            service.upload(ids[i % 2], "owner", "model/gltf-binary") for i in range(16)
        ), return_exceptions=True)

    results = asyncio.run(burst())
    granted_ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, Asset3DError)]
    # Five 200 MiB reservations fit in 1 GiB; the other eleven are refused.
    assert len(granted_ok) == 5 and len(refused) == 11
    assert all(r.status in (507, 409) for r in refused)
    assert asyncio.run(service.store.stored_bytes("owner")) == 5 * 200 * 1024 * 1024
    # Every refused grant left nothing behind on its asset.
    pending = sum(len(client.get(f"/studio/3d/{id}").json()["pending_uploads"]) for id in ids)
    assert pending == 5


def test_a_grant_can_reserve_only_the_files_size():
    client, service, assets = build()
    id = make_asset(client)["asset_id"]

    async def allowance(owner):
        return 1000

    service.storage_allowance = allowance
    assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 507
    grant = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png", "byte_length": 600})
    assert grant.status_code == 200
    assert grant.json()["upload_headers"] == {"x-goog-content-length-range": "0,600"}
    assert assets.upload_limits[grant.json()["asset_key"]] == 600
    assert asyncio.run(service.store.stored_bytes("owner")) == 600
    assert client.post(f"/studio/3d/{id}/uploads", json={
        "content_type": "image/png", "byte_length": 21 * 1024 * 1024,
    }).status_code == 422


def ready_to_release(client, assets, id) -> dict:
    asset = add_mesh(client, assets, id).json()
    mesh = asset["candidates"][0]["candidate_id"]
    asset = keep(client, id, asset, mesh)
    return select(client, id, asset, mesh=mesh).json()


def test_the_pack_slot_is_held_until_the_zip_is_stored(monkeypatch):
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = ready_to_release(client, assets, id)
    held = []
    original = assets.write_once

    async def recording(key, content, content_type):
        if key.endswith(".zip"):
            held.append(service.pack_slots.locked())
        return await original(key, content, content_type)

    monkeypatch.setattr(assets, "write_once", recording)
    assert client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"}).status_code == 200
    assert held == [True]


@pytest.mark.parametrize("finished", [True, False])
def test_a_release_racing_a_delete_leaves_no_zip_behind(monkeypatch, finished):
    """`finished`: the delete completed before the zip landed; otherwise it had
    marked the asset and removed its files, but not yet its rows."""
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    asset = ready_to_release(client, assets, id)
    original = assets.write_once

    async def deleted_meanwhile(key, content, content_type):
        if key.endswith(".zip"):
            if finished:
                await service.purge(await service.store.get(id))
            else:
                await service.store.mark_deleting(id)
                for prefix in (f"uploads/assets3d/{id}/", f"outputs/assets3d/{id}/"):
                    await assets.delete_prefix(prefix)
        return await original(key, content, content_type)

    monkeypatch.setattr(assets, "write_once", deleted_meanwhile)
    released = client.post(f"/studio/3d/{id}/release", json={"expected_revision": asset["revision"], "name": "v1"})
    assert released.status_code in (404, 409)
    assert not any(key.startswith(f"outputs/assets3d/{id}/") for key in assets.assets)
    monkeypatch.setattr(assets, "write_once", original)
    if not finished:
        # The half-deleted asset takes no new work, and can be deleted again.
        assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "image/png"}).status_code == 404
        assert client.request("DELETE", f"/studio/3d/{id}", json={"confirm_name": "Crate"}).status_code == 204
    assert asyncio.run(service.store.get(id)) is None
    assert asyncio.run(service.store.stored_bytes("owner")) == 0


def test_a_grant_whose_save_fails_leaves_no_unrecorded_reservation(monkeypatch):
    """The grant and its reservation are one transaction: a save that fails with
    something other than a conflict (a dropped connection) cannot leave a
    reservation that no pending grant records, and so nothing could release."""
    client, service, assets = build()
    id = make_asset(client)["asset_id"]

    async def dropped(*args, **kwargs):
        raise ConnectionError("database connection lost")

    monkeypatch.setattr(service.store, "append", dropped)
    try:
        response = client.post(f"/studio/3d/{id}/uploads", json={"content_type": "model/gltf-binary"})
    except ConnectionError:
        response = None
    monkeypatch.undo()
    asset = asyncio.run(service.store.get(id))
    accounted = asyncio.run(service.store.accounted_keys(id))
    # Every reservation belongs to a grant the asset records as pending.
    assert accounted <= set(asset.pending_uploads)
    if response is not None and response.status_code == 200:
        assert response.json()["asset_key"] in asset.pending_uploads


def test_a_release_never_stores_a_zip_for_a_revision_the_guard_replaced():
    """Giving back expired grants saves a new revision of the asset; a release
    that read its revision before that must stop before building the zip."""
    client, service, assets = build()
    id = make_asset(client)["asset_id"]
    ready_to_release(client, assets, id)
    for _ in range(6):
        assert client.post(f"/studio/3d/{id}/uploads", json={"content_type": "model/gltf-binary"}).status_code == 200

    async def age():
        current = await service.store.get(id)
        current.pending_uploads = {key: "2026-01-01T00:00:00+00:00" for key in current.pending_uploads}
        return await service.store.append(current, current.revision)

    revision = asyncio.run(age()).revision
    service.work_guard = ledger_guard(service, GIB)
    refused = client.post(f"/studio/3d/{id}/release", json={"expected_revision": revision, "name": "v1"})
    assert refused.status_code == 409
    assert not any(key.startswith(f"outputs/assets3d/{id}/releases/") for key in assets.assets)
    current = client.get(f"/studio/3d/{id}").json()
    assert current["pending_uploads"] == {} and current["revision"] > revision
    released = client.post(f"/studio/3d/{id}/release", json={"expected_revision": current["revision"], "name": "v1"})
    assert released.status_code == 200
