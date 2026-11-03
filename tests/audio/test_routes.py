import io
import json
import subprocess
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.audio.freesound import FreesoundClient
from magic_video_editor.audio.media import AudioMedia
from magic_video_editor.audio.routes import create_audio_router
from magic_video_editor.audio.service import AudioService
from magic_video_editor.audio.store import MemoryAudioStore
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.studio.accounts import AccountPolicy, AccountTier
from magic_video_editor.studio.routes import studio_actor_id

CC0 = "http://creativecommons.org/publicdomain/zero/1.0/"
BY4 = "https://creativecommons.org/licenses/by/4.0/"


def tone(fmt: str, seconds: float = 1.5, frequency: int = 660) -> bytes:
    codec = ["-c:a", "libmp3lame", "-b:a", "128k"] if fmt == "mp3" else ["-c:a", "pcm_s16le"]
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}",
         "-ar", "44100", *codec, "-f", fmt, "-"],
        check=True, capture_output=True,
    ).stdout


@pytest.fixture(scope="module")
def preview() -> bytes:
    return tone("mp3")


def fake_freesound(preview: bytes, licences: dict[int, str]) -> FreesoundClient:
    def sound(id: int) -> dict:
        return {
            "id": id, "name": f"glass {id}", "username": "qubodup", "license": licences[id],
            "duration": 1.5, "url": f"https://freesound.org/people/qubodup/sounds/{id}/", "tags": ["glass"],
            "previews": {"preview-hq-mp3": f"https://cdn.freesound.org/previews/0/{id}-hq.mp3"},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.freesound.org":
            return httpx.Response(200, content=preview)
        if request.url.path.startswith("/apiv2/sounds/"):
            return httpx.Response(200, json=sound(int(request.url.path.split("/")[3])))
        return httpx.Response(200, json={"results": [sound(i) for i in licences]})

    return FreesoundClient("token", transport=httpx.MockTransport(handler))


def build(preview, *, public=False, licences=None, media=True):
    assets = MemoryAssetStore()

    async def tier(owner):
        return AccountTier.PUBLIC if public else AccountTier.INVITED

    policy = AccountPolicy(tier_for=tier)
    movies = MovieService(MemoryMovieStore(), assets, media=MovieMedia(assets), account_policy=policy)
    service = AudioService(
        MemoryAudioStore(), assets,
        freesound=fake_freesound(preview, licences or {101: CC0, 202: BY4}),
        media=AudioMedia() if media else None,
        account_policy=policy, movie_service=movies,
    )
    app = FastAPI()
    app.include_router(create_audio_router(service))
    app.dependency_overrides[studio_actor_id] = lambda: "owner"
    return TestClient(app), service, assets, movies


def make_set(client, cues=None, policy="cc0"):
    audio_set = client.post("/studio/audio", json={"name": "Glass"}).json()
    return client.put(f"/studio/audio/{audio_set['set_id']}", json={
        "expected_revision": 1, "name": "Glass", "licence_policy": policy,
        "cues": cues or [{"cue_id": "crack", "name": "Glass crack", "prompt": "glass crack", "keep": 1}],
    }).json()


def test_import_review_select_release_produces_an_immutable_licensed_pack(preview):
    client, service, assets, _ = build(preview)
    audio_set = make_set(client)
    id = audio_set["set_id"]
    results = client.get(f"/studio/audio/{id}/free-sounds", params={"q": "glass crack"}).json()
    assert [r["source_id"] for r in results] == ["101"]
    imported = client.post(f"/studio/audio/{id}/cues/crack/import",
                           json={"expected_revision": 2, "source_id": "101"}).json()
    candidate = imported["candidates"][0]
    assert candidate["licence"]["spdx"] == "CC0-1.0"
    assert candidate["licence"]["variant"] == "preview"
    assert candidate["licence"]["author"] == "qubodup"
    assert candidate["rendition_key"].endswith(".wav")
    assert candidate["measured"]["integrated_lufs"] is not None
    assert client.post(f"/studio/audio/{id}/cues/crack/import",
                       json={"expected_revision": 3, "source_id": "101"}).status_code == 409
    # A CC-BY sound cannot enter a CC0 set.
    assert client.post(f"/studio/audio/{id}/cues/crack/import",
                       json={"expected_revision": 3, "source_id": "202"}).status_code == 422
    cid = candidate["candidate_id"]
    # Selection takes only kept candidates.
    assert client.put(f"/studio/audio/{id}/cues/crack/selection",
                      json={"expected_revision": 3, "candidate_ids": [cid]}).status_code == 409
    assert client.get(f"/studio/audio/{id}/readiness").json()["ready"] is False
    reviewed = client.post(f"/studio/audio/{id}/candidates/{cid}/review",
                           json={"expected_revision": 3, "outcome": "keep", "note": "clean"}).json()
    assert reviewed["candidates"][0]["review"]["actor_kind"] == "session"
    client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]})
    readiness = client.get(f"/studio/audio/{id}/readiness").json()
    assert readiness == {"ready": True, "problems": [], "files": 1, "attribution_required": False, "previews": 1}
    released = client.post(f"/studio/audio/{id}/release", json={"expected_revision": 5, "name": "v1"}).json()
    release = released["releases"][0]
    assert release["file_count"] == 1 and release["attribution_required"] is False
    assert client.post(f"/studio/audio/{id}/release", json={"expected_revision": 6, "name": "v1"}).status_code == 409
    download = client.get(f"/studio/audio/{id}/releases/{release['release_id']}/download")
    assert download.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert sorted(archive.namelist()) == [
        "CREDITS.md", "audio/crack/crack_01.wav", "manifest.json", "originals/crack/crack_01.mp3",
    ]
    manifest = json.loads(archive.read("manifest.json"))
    entry = manifest["files"][0]
    assert entry["licence"]["source_url"] == "https://freesound.org/people/qubodup/sounds/101/"
    assert entry["review"]["reviewer"] == "person"
    assert archive.read("originals/crack/crack_01.mp3") == preview
    assert "Attribution required" not in archive.read("CREDITS.md").decode()
    # The pack is stored create-once: a second write to its key changes nothing.
    import asyncio
    assert asyncio.run(assets.write_once(release["asset_key"], b"tampered", "application/zip")) is False
    assert client.get(f"/studio/audio/{id}/releases/{release['release_id']}/download").content == download.content


def test_cc_by_opt_in_writes_credits_and_dropping_it_clears_the_selection(preview):
    client, *_ = build(preview)
    audio_set = make_set(client, policy="cc0_cc_by")
    id = audio_set["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "202"}).json()
    cid = s["candidates"][0]["candidate_id"]
    assert s["candidates"][0]["licence"]["attribution_required"] is True
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    s = client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]}).json()
    assert client.get(f"/studio/audio/{id}/readiness").json()["attribution_required"] is True
    released = client.post(f"/studio/audio/{id}/release", json={"expected_revision": 5, "name": "v1"}).json()
    rid = released["releases"][0]["release_id"]
    credits = zipfile.ZipFile(io.BytesIO(client.get(f"/studio/audio/{id}/releases/{rid}/download").content)).read("CREDITS.md").decode()
    assert "## Attribution required" in credits and "CC-BY-4.0" in credits and "qubodup" in credits
    narrowed = client.put(f"/studio/audio/{id}", json={
        "expected_revision": 6, "name": "Glass", "licence_policy": "cc0",
        "cues": [{"cue_id": "crack", "name": "Glass crack"}],
    }).json()
    assert narrowed["cues"][0]["selected"] == []
    assert narrowed["releases"][0]["release_id"] == rid


def test_uploads_api_key_reviews_and_replacing_a_preview(preview):
    client, service, assets, _ = build(preview)
    audio_set = make_set(client)
    id = audio_set["set_id"]
    grant = client.post(f"/studio/audio/{id}/uploads", json={"content_type": "audio/wav"}).json()
    import asyncio
    asyncio.run(assets.write_once(grant["asset_key"], tone("wav", 2), "audio/wav"))
    s = client.post(f"/studio/audio/{id}/cues/crack/upload",
                    json={"expected_revision": grant["revision"], "asset_key": grant["asset_key"], "filename": "mine.wav"}).json()
    upload = s["candidates"][0]
    assert upload["origin"] == "upload" and upload["licence"]["spdx"] == "LicenseRef-Owner-Upload"
    assert client.post(f"/studio/audio/{id}/cues/crack/upload",
                       json={"expected_revision": s["revision"], "asset_key": "uploads/audio/x/y"}).status_code == 403
    reviewed = client.post(f"/studio/audio/{id}/candidates/{upload['candidate_id']}/review",
                           headers={"Authorization": "Bearer msk_test"},
                           json={"expected_revision": s["revision"], "outcome": "reject", "reason": "wrong source"}).json()
    assert reviewed["candidates"][0]["review"]["actor_kind"] == "api_key"
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": reviewed["revision"], "source_id": "101"}).json()
    preview_candidate = s["candidates"][1]
    pid = preview_candidate["candidate_id"]
    s = client.post(f"/studio/audio/{id}/candidates/{pid}/review", json={"expected_revision": s["revision"], "outcome": "keep"}).json()
    wrong = client.post(f"/studio/audio/{id}/uploads", json={"content_type": "audio/wav"}).json()
    asyncio.run(assets.write_once(wrong["asset_key"], tone("wav", 4), "audio/wav"))
    assert client.post(f"/studio/audio/{id}/candidates/{pid}/original",
                       json={"expected_revision": wrong["revision"], "asset_key": wrong["asset_key"]}).status_code == 422
    right = client.post(f"/studio/audio/{id}/uploads", json={"content_type": "audio/wav"}).json()
    original = tone("wav", 1.5)
    asyncio.run(assets.write_once(right["asset_key"], original, "audio/wav"))
    s = client.post(f"/studio/audio/{id}/candidates/{pid}/original",
                    json={"expected_revision": right["revision"], "asset_key": right["asset_key"]}).json()
    replaced = s["candidates"][1]
    assert replaced["licence"]["variant"] == "original" and replaced["review"] is None
    assert replaced["media_type"] == "audio/wav"
    assert replaced["provenance"]["preview_sha256"] == preview_candidate["sha256"]
    assert client.get(f"/studio/audio/{id}/assets/{replaced['asset_key']}").content == original


def test_public_accounts_keep_originals_only_and_cannot_use_service_ffmpeg(preview):
    client, *_ = build(preview, public=True)
    caps = client.get("/studio/audio/capabilities").json()
    assert caps["account_tier"] == "public"
    assert caps["processing_available"] is False and caps["send_to_movie_available"] is False
    audio_set = make_set(client)
    id = audio_set["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    candidate = s["candidates"][0]
    assert candidate["rendition_key"] is None and candidate["measured"] is None
    assert candidate["duration_seconds"] == 1.5
    cid = candidate["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]})
    released = client.post(f"/studio/audio/{id}/release", json={"expected_revision": 5, "name": "v1"}).json()
    rid = released["releases"][0]["release_id"]
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/studio/audio/{id}/releases/{rid}/download").content))
    assert sorted(archive.namelist()) == ["CREDITS.md", "audio/crack/crack_01.mp3", "manifest.json"]
    assert client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                       json={"movie_id": "m", "movie_revision": 1}).status_code == 403


def test_send_to_movie_adds_a_soundtrack_cue(preview):
    client, service, assets, movies = build(preview)
    audio_set = make_set(client)
    id = audio_set["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    cid = s["candidates"][0]["candidate_id"]
    import asyncio
    movie = asyncio.run(movies.create("owner", "Film"))
    assert client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                       json={"movie_id": movie.movie_id, "movie_revision": 1}).status_code == 409
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    sent = client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                       json={"movie_id": movie.movie_id, "movie_revision": 1})
    assert sent.status_code == 200
    assert len(sent.json()["soundtracks"]) == 1
    assert client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                       json={"movie_id": movie.movie_id, "movie_revision": 1}).status_code == 409


def test_owner_isolation_limits_and_deletion(preview):
    client, service, assets, _ = build(preview)
    audio_set = make_set(client)
    id = audio_set["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    key = s["candidates"][0]["asset_key"]
    assert client.put(f"/studio/audio/{id}/cues/crack/selection",
                      json={"expected_revision": 3, "candidate_ids": ["a", "b"]}).status_code == 422
    assert client.put(f"/studio/audio/{id}", json={
        "expected_revision": 3, "name": "Glass",
        "cues": [{"cue_id": "a", "name": "a"}, {"cue_id": "a", "name": "b"}],
    }).status_code == 422
    assert client.put(f"/studio/audio/{id}", json={"expected_revision": 1, "name": "x", "cues": []}).json()["detail"]["current_revision"] == 3
    import asyncio
    assert asyncio.run(service.store.stored_bytes("owner")) > 0
    client.app.dependency_overrides[studio_actor_id] = lambda: "other"
    assert client.get(f"/studio/audio/{id}").status_code == 404
    assert client.get(f"/studio/audio/{id}/assets/{key}").status_code == 404
    assert client.get("/studio/audio").json() == []
    client.app.dependency_overrides[studio_actor_id] = lambda: "owner"
    assert client.get(f"/studio/audio/{id}/assets/outputs/audio/{id}/nope").status_code == 404
    assert client.request("DELETE", f"/studio/audio/{id}", json={"confirm_name": "nope"}).status_code == 422
    assert client.request("DELETE", f"/studio/audio/{id}", json={"confirm_name": "Glass"}).status_code == 204
    assert client.get(f"/studio/audio/{id}").status_code == 404
    assert asyncio.run(assets.describe(key)) is None
    assert asyncio.run(service.store.stored_bytes("owner")) == 0


def test_without_a_freesound_token_search_says_unavailable(preview):
    client, service, *_ = build(preview)
    service.freesound = FreesoundClient(None)
    assert client.get("/studio/audio/capabilities").json()["free_sounds_available"] is False
    id = make_set(client)["set_id"]
    assert client.get(f"/studio/audio/{id}/free-sounds", params={"q": "glass"}).status_code == 503


def test_the_release_limit_counts_every_copy_a_reused_file_makes(preview, monkeypatch):
    from magic_video_editor.audio import service as audio_service

    cues = [{"cue_id": f"c{i}", "name": f"Cue {i}", "keep": 1} for i in range(3)]
    client, service, assets, _ = build(preview)
    id = make_set(client, cues=cues)["set_id"]
    import asyncio
    grant = client.post(f"/studio/audio/{id}/uploads", json={"content_type": "audio/wav"}).json()
    asyncio.run(assets.write_once(grant["asset_key"], tone("wav", 1), "audio/wav"))
    s = client.get(f"/studio/audio/{id}").json()
    for cue in cues:
        s = client.post(f"/studio/audio/{id}/cues/{cue['cue_id']}/upload",
                        json={"expected_revision": s["revision"], "asset_key": grant["asset_key"]}).json()
    keys = {c["asset_key"] for c in s["candidates"]}
    assert len(keys) == 1  # one stored file, selected by three cues
    for candidate in s["candidates"]:
        s = client.post(f"/studio/audio/{id}/candidates/{candidate['candidate_id']}/review",
                        json={"expected_revision": s["revision"], "outcome": "keep"}).json()
        s = client.put(f"/studio/audio/{id}/cues/{candidate['cue_id']}/selection",
                       json={"expected_revision": s["revision"], "candidate_ids": [candidate["candidate_id"]]}).json()
    one = s["candidates"][0]
    per_copy = asyncio.run(assets.describe(one["asset_key"])).size + asyncio.run(assets.describe(one["rendition_key"])).size
    # Two copies fit, three do not: counting the stored file once would have passed.
    monkeypatch.setattr(audio_service, "MAX_RELEASE_BYTES", per_copy * 2 + 1)
    refused = client.post(f"/studio/audio/{id}/release", json={"expected_revision": s["revision"], "name": "v1"})
    assert refused.status_code == 413
    # The manifest and credits count too: room for every audio copy but not for them.
    monkeypatch.setattr(audio_service, "MAX_RELEASE_BYTES", per_copy * 3 + 10)
    assert client.post(f"/studio/audio/{id}/release", json={"expected_revision": s["revision"], "name": "v1"}).status_code == 413
    monkeypatch.setattr(audio_service, "MAX_RELEASE_BYTES", per_copy * 3 + 100_000)
    released = client.post(f"/studio/audio/{id}/release", json={"expected_revision": s["revision"], "name": "v1"})
    assert released.status_code == 200 and released.json()["releases"][0]["file_count"] == 3


def test_a_rendition_too_large_for_later_reads_is_not_kept(preview, monkeypatch):
    from magic_video_editor.audio import service as audio_service

    # The MP3 preview fits; its WAV rendition is several times larger.
    monkeypatch.setattr(audio_service, "MAX_AUDIO_BYTES", len(preview) + 1000)
    client, *_ = build(preview)
    id = make_set(client)["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    candidate = s["candidates"][0]
    assert candidate["rendition_key"] is None
    assert candidate["measured"]["integrated_lufs"] is not None
    cid = candidate["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]})
    assert client.post(f"/studio/audio/{id}/release", json={"expected_revision": 5, "name": "v1"}).status_code == 200


def test_a_verdict_names_the_file_it_was_given_on(preview):
    client, service, assets, _ = build(preview)
    id = make_set(client)["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    candidate = s["candidates"][0]
    cid = candidate["candidate_id"]
    import asyncio
    grant = client.post(f"/studio/audio/{id}/uploads", json={"content_type": "audio/wav"}).json()
    asyncio.run(assets.write_once(grant["asset_key"], tone("wav", 1.5), "audio/wav"))
    s = client.post(f"/studio/audio/{id}/candidates/{cid}/original",
                    json={"expected_revision": grant["revision"], "asset_key": grant["asset_key"]}).json()
    # A verdict on the preview someone listened to is not moved onto the original.
    stale = client.post(f"/studio/audio/{id}/candidates/{cid}/review",
                        json={"expected_revision": s["revision"], "outcome": "keep", "sha256": candidate["sha256"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "candidate_file_changed"
    kept = client.post(f"/studio/audio/{id}/candidates/{cid}/review",
                       json={"expected_revision": s["revision"], "outcome": "keep", "sha256": s["candidates"][0]["sha256"]})
    assert kept.status_code == 200


def test_cc_by_sounds_are_not_sent_to_movies_without_their_credit(preview):
    client, service, assets, movies = build(preview)
    id = make_set(client, policy="cc0_cc_by")["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "202"}).json()
    cid = s["candidates"][0]["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    import asyncio
    movie = asyncio.run(movies.create("owner", "Film"))
    refused = client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                          json={"movie_id": movie.movie_id, "movie_revision": 1})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "attribution_required"
    after = asyncio.run(movies.store.get(movie.movie_id))
    assert after.soundtracks == [] and after.upload_keys == []


def test_send_to_movie_respects_the_storage_guard(preview):
    from magic_video_editor.audio.service import AudioError

    client, service, assets, movies = build(preview)
    id = make_set(client)["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    cid = s["candidates"][0]["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    import asyncio
    movie = asyncio.run(movies.create("owner", "Film"))
    before = set(assets.assets)

    async def full(owner, requested):
        raise AudioError("Storage limit reached. Free storage before starting new work.", 507)

    service.work_guard = full
    refused = client.post(f"/studio/audio/{id}/candidates/{cid}/send-to-movie",
                          json={"movie_id": movie.movie_id, "movie_revision": 1})
    assert refused.status_code == 507
    assert set(assets.assets) == before


def test_releases_are_zipped_off_the_event_loop(preview, monkeypatch):
    import asyncio
    from magic_video_editor.audio import release as packs

    client, *_ = build(preview)
    id = make_set(client)["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    cid = s["candidates"][0]["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]})
    on_loop = []
    original = packs.build_pack

    def recording(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return original(*args, **kwargs)

    monkeypatch.setattr(packs, "build_pack", recording)
    released = client.post(f"/studio/audio/{id}/release", json={"expected_revision": 5, "name": "v1"})
    assert released.status_code == 200
    # Compression ran in a worker thread, where no event loop is running.
    assert on_loop == [False]


def test_a_waiting_release_reads_no_files_until_the_slot_is_free(preview, monkeypatch):
    import asyncio

    client, service, assets, _ = build(preview)
    id = make_set(client)["set_id"]
    s = client.post(f"/studio/audio/{id}/cues/crack/import", json={"expected_revision": 2, "source_id": "101"}).json()
    cid = s["candidates"][0]["candidate_id"]
    client.post(f"/studio/audio/{id}/candidates/{cid}/review", json={"expected_revision": 3, "outcome": "keep"})
    client.put(f"/studio/audio/{id}/cues/crack/selection", json={"expected_revision": 4, "candidate_ids": [cid]})
    reads = []
    original = assets.read_limited

    async def recording(key, limit):
        reads.append(key)
        return await original(key, limit)

    monkeypatch.setattr(assets, "read_limited", recording)

    async def scenario():
        await service.pack_slots.acquire()
        waiting = asyncio.create_task(service.release(id, "owner", 5, "v1"))
        for _ in range(20):
            await asyncio.sleep(0)
        # Another release holds the slot: this one has not loaded any audio yet.
        assert reads == [] and not waiting.done()
        service.pack_slots.release()
        return await waiting

    released = asyncio.run(scenario())
    assert reads and released.releases[0].name == "v1"
