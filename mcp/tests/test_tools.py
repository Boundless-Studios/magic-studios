"""Tool behaviour against a mock transport, including the 409 retry."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioApiError, StudioClient


async def test_uploads_never_forward_api_credentials():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b'asset')
    client = StudioClient(base_url='http://127.0.0.1:5173', api_key='private',
                          transport=httpx.MockTransport(handle))
    await client.put_file('/local-assets/reference.png', b'png', 'image/png')
    assert str(requests[0].url) == 'http://127.0.0.1:5173/local-assets/reference.png'
    assert 'Authorization' not in requests[0].headers
    await client.aclose()


async def test_downloads_authenticate_to_the_studio_api_and_to_no_other_host():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b'asset')
    client = StudioClient(base_url='https://studio.test', api_key='private',
                          transport=httpx.MockTransport(handle))
    assert await client.download('/studio/projects/p/files?key=outputs/studio/p/a.png') == b'asset'
    assert await client.download('https://elsewhere.test/a.png') == b'asset'
    assert requests[0].headers['Authorization'] == 'Bearer private'
    assert 'Authorization' not in requests[1].headers
    await client.aclose()


class RecordingTransport(httpx.MockTransport):
    """Answers from a queued script and records every request it served."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(f"unscripted request: {request.method} {request.url}")
        return self._responses.pop(0)

    def body(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _client(responses: list[httpx.Response]) -> tuple[StudioClient, RecordingTransport]:
    transport = RecordingTransport(responses)
    client = StudioClient(
        base_url="https://studio.test", api_key="mvs_" + "a" * 32, transport=transport
    )
    return client, transport


def _project(revision: int, **overrides: Any) -> dict[str, Any]:
    project: dict[str, Any] = {
        "project_id": "p-1",
        "revision": revision,
        "primary_action": "add_motion",
        "active_operations": [],
        "manifest": {
            "name": "Ember Knight",
            "subject": {"approval": {"state": "approved"}},
            "set_approval": {"state": "not_ready"},
            "set_assessment": None,
            "set_assessment_fresh": False,
            "motions": [],
        },
    }
    project.update(overrides)
    return project


async def test_list_projects_sends_the_bearer_key_and_compacts_the_rows() -> None:
    client, transport = _client(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "project_id": "p-1",
                        "name": "Ember Knight",
                        "revision": 3,
                        "subject_state": "approved",
                        "motion_count": 6,
                        "release_count": 0,
                        "updated_at": "2026-09-06T15:45:31.536757Z",
                        "created_at": "2026-09-06T15:45:31.536757Z",
                        "created_by": "iap:1",
                        "external_subject": None,
                        "activity": {"running": 0},
                        "unreadable": False,
                    }
                ],
            )
        ]
    )

    result = await tools.list_projects(client)

    assert result == [
        {
            "project_id": "p-1",
            "name": "Ember Knight",
            "revision": 3,
            "subject_state": "approved",
            "motion_count": 6,
            "release_count": 0,
            "updated_at": "2026-09-06T15:45:31.536757Z",
        }
    ]
    assert transport.requests[0].headers["Authorization"] == f"Bearer mvs_{'a' * 32}"
    assert str(transport.requests[0].url) == "https://studio.test/studio/projects"


async def test_add_recommended_motions_reads_the_revision_then_sends_the_six() -> None:
    client, transport = _client(
        [
            httpx.Response(200, json=_project(4)),
            httpx.Response(200, json=_project(5)),
        ]
    )

    result = await tools.add_recommended_motions(client, "p-1")

    assert transport.requests[0].method == "GET"
    body = transport.body(1)
    assert body["expected_revision"] == 4
    assert [motion["motion_id"] for motion in body["motions"]] == [
        "idle",
        "walk",
        "attack",
        "guard",
        "cast",
        "hurt",
    ]
    assert body["motions"][0]["loop_policy"] == "loop"
    assert body["motions"][2]["loop_policy"] == "once"
    assert result["revision"] == 5


async def test_a_stale_revision_is_retried_once_with_the_revision_the_409_reports() -> (
    None
):
    conflict = httpx.Response(
        409,
        json={
            "detail": {"message": "expected revision is stale", "current_revision": 9}
        },
    )
    client, transport = _client(
        [
            httpx.Response(200, json=_project(4)),
            conflict,
            httpx.Response(200, json=_project(10)),
        ]
    )

    result = await tools.approve_subject(client, "p-1")

    assert len(transport.requests) == 3
    assert transport.body(1)["expected_revision"] == 4
    # No second GET: the conflict body already carried the current revision.
    assert transport.requests[2].method == "POST"
    assert transport.body(2)["expected_revision"] == 9
    assert result["revision"] == 10


async def test_a_second_conflict_is_raised_rather_than_retried_forever() -> None:
    conflict = httpx.Response(
        409,
        json={
            "detail": {"message": "expected revision is stale", "current_revision": 9}
        },
    )
    client, _ = _client(
        [httpx.Response(200, json=_project(4)), conflict, conflict]
    )

    with pytest.raises(StudioApiError) as raised:
        await tools.approve_subject(client, "p-1")

    assert raised.value.status_code == 409


async def test_a_non_conflict_failure_is_not_retried() -> None:
    client, transport = _client(
        [
            httpx.Response(200, json=_project(4)),
            httpx.Response(422, json={"detail": "subject_assessment_missing"}),
        ]
    )

    with pytest.raises(StudioApiError) as raised:
        await tools.approve_subject(client, "p-1")

    assert raised.value.status_code == 422
    assert raised.value.detail == "subject_assessment_missing"
    assert len(transport.requests) == 2


async def test_upload_reference_requests_a_target_then_puts_the_bytes(tmp_path) -> None:
    source = tmp_path / "reference.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    client, transport = _client(
        [
            httpx.Response(200, json=_project(1)),
            httpx.Response(
                200,
                json={"key": "sources/studio/p-1/x.png", "upload_url": "https://up/x"},
            ),
            httpx.Response(200),
        ]
    )

    result = await tools.upload_reference(client, "p-1", str(source))

    assert transport.body(1) == {"content_type": "image/png", "expected_revision": 1}
    put = transport.requests[2]
    assert put.method == "PUT"
    assert str(put.url) == "https://up/x"
    assert put.headers["Content-Type"] == "image/png"
    assert put.content == b"\x89PNG\r\n\x1a\nfake"
    assert result == {
        "key": "sources/studio/p-1/x.png",
        "bytes": 12,
        "content_type": "image/png",
    }


async def test_wait_for_operations_reconciles_once_the_queue_is_empty() -> None:
    client, transport = _client(
        [
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"operations": [], "next_cursor": None}),
            httpx.Response(200, json=[]),
        ]
    )

    result = await tools.wait_for_operations(client, "p-1", 5.0, poll_interval_s=0.0)

    assert result == {"settled": True, "in_flight": 0, "failed": [], "recent": []}
    assert transport.requests[1].method == "POST"
    assert transport.requests[1].url.path == "/studio/projects/p-1/activity/reconcile"


async def test_wait_reconciles_completed_work_that_is_still_listed_as_active():
    client, transport = _client([
        httpx.Response(200, json=[{'status': 'completed', 'stage': 'awaiting_settlement'}]),
        httpx.Response(200, json={'operations': []}),
        httpx.Response(200, json=[]),
    ])
    result = await tools.wait_for_operations(client, 'p-1', 5, poll_interval_s=0)
    assert result['settled'] is True
    assert transport.requests[1].method == 'POST'


@pytest.mark.parametrize("initial_status", ["queued", "processing"])
async def test_wait_advances_hosted_jobs_without_a_browser(initial_status):
    fields = {"operation": "generate_motion", "motion_id": "walk",
              "failure_code": None, "failure_explanation": None}
    initial = {**fields, "job_id": "gpu-job", "status": initial_status, "stage": initial_status}
    processing = {**fields, "job_id": "gpu-job", "status": "processing", "stage": "generating"}
    completed = {**fields, "job_id": "gpu-job", "status": "completed", "stage": "completed"}
    client, transport = _client([
        httpx.Response(200, json=[initial]),
        httpx.Response(200, json={"operations": [processing]}),
        httpx.Response(200, json=[processing]),
        httpx.Response(200, json=[processing]),
        httpx.Response(200, json={"operations": [completed]}),
        httpx.Response(200, json=[]),
    ])
    result = await tools.wait_for_operations(client, 'p-1', 5, poll_interval_s=0)
    assert result['settled'] is True
    assert result['recent'][0]['job_id'] == 'gpu-job'
    assert result['recent'][0]['status'] == 'completed'
    assert [r.method for r in transport.requests] == ['GET', 'POST', 'GET'] * 2


async def test_wait_settles_on_stage_and_reports_recent_failures():
    # The operations list keeps recent failed and cancelled work. A project with a
    # past failure must still settle once nothing is queued, generating or awaiting
    # settlement, and it must still say what failed.
    fields = {"operation": "generate_motion", "motion_id": "walk",
              "failure_code": None, "failure_explanation": None}
    failed = {**fields, "job_id": "old", "status": "failed", "stage": "failed",
              "failure_code": "provider_failed", "failure_explanation": "GPU failed"}
    cancelled = {**fields, "job_id": "dropped", "status": "cancelled", "stage": "cancelled"}
    running = {**fields, "job_id": "new", "status": "processing", "stage": "generating"}
    client, transport = _client([
        httpx.Response(200, json=[running, failed, cancelled]),
        httpx.Response(200, json={"operations": [running, failed]}),
        httpx.Response(200, json=[running, failed, cancelled]),
        httpx.Response(200, json=[failed, cancelled]),
        httpx.Response(200, json={"operations": [failed]}),
        httpx.Response(200, json=[failed, cancelled]),
    ])
    result = await tools.wait_for_operations(client, 'p-1', 5, poll_interval_s=0)
    assert result['settled'] is True
    assert result['in_flight'] == 0
    assert [item['job_id'] for item in result['failed']] == ['old', 'dropped']
    assert result['failed'][0]['failure_explanation'] == 'GPU failed'
    # Reconcile ran on both passes, not only once the list emptied.
    assert [r.method for r in transport.requests] == ['GET', 'POST', 'GET'] * 2


async def test_wait_timeout_counts_only_active_stages():
    fields = {"operation": "generate_motion", "motion_id": "walk",
              "failure_code": None, "failure_explanation": None}
    client, _ = _client([
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"operations": []}),
        httpx.Response(200, json=[
            {**fields, "job_id": "new", "status": "queued", "stage": "queued"},
            {**fields, "job_id": "old", "status": "failed", "stage": "failed"},
        ]),
    ])
    result = await tools.wait_for_operations(client, 'p-1', 0)
    assert result['settled'] is False
    assert result['in_flight'] == 1
    assert [item['job_id'] for item in result['failed']] == ['old']


async def test_wait_timeout_reports_state_after_reconciliation():
    client, _ = _client([
        httpx.Response(200, json=[{'job_id': 'old', 'status': 'queued'}]),
        httpx.Response(200, json={"operations": []}),
        httpx.Response(200, json=[{'job_id': 'running', 'status': 'processing',
            'stage': 'generating', 'operation': 'generate_motion', 'motion_id': 'walk',
            'failure_code': None, 'failure_explanation': None}]),
    ])
    result = await tools.wait_for_operations(client, 'p-1', 0)
    assert result['settled'] is False
    assert result['in_flight'] == 1
    assert result['recent'][0]['job_id'] == 'running'
    assert result['recent'][0]['status'] == 'processing'


async def test_export_release_recovers_the_source_revision_from_the_conflict() -> None:
    client, transport = _client(
        [
            httpx.Response(
                409, json={"detail": {"message": "stale", "current_revision": 17}}
            ),
            httpx.Response(
                200,
                json={
                    "export_id": "e-1",
                    "release_id": "r-1",
                    "profile_name": "png-json",
                    "profile_version": "2",
                    "artifact_key": "exports/studio/x.zip",
                    "sha256": "abc",
                    "size_bytes": 12,
                },
            ),
        ]
    )

    result = await tools.export_release(client, "r-1")

    assert transport.body(0)["expected_revision"] == 1
    assert transport.body(1)["expected_revision"] == 17
    assert result["profile"] == "png-json-2"
    assert result["export_id"] == "e-1"


async def test_download_export_writes_the_file_and_reports_its_digest(tmp_path) -> None:
    client, _ = _client(
        [
            httpx.Response(200, json={"key": "k", "upload_url": "https://dl/x.zip"}),
            httpx.Response(200, content=b"zipbytes"),
        ]
    )
    destination = tmp_path / "out" / "release.zip"

    result = await tools.download_export(client, "e-1", str(destination))

    assert destination.read_bytes() == b"zipbytes"
    assert result["bytes"] == 8
    assert result["sha256"] == hashlib.sha256(b"zipbytes").hexdigest()


async def test_attach_movie_voice_uploads_without_leaking_key(tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"test audio")
    client, transport = _client([
        httpx.Response(200, json={"characters": [{"character_id": "man"}]}),
        httpx.Response(200, json={
            "upload_url": "https://storage.test/signed", "asset_key": "uploads/voice",
            "revision": 7,
        }),
        httpx.Response(200),
        httpx.Response(200, json={"revision": 8, "characters": [{
            "character_id": "man", "voice_asset_key": "outputs/frozen.wav",
            "voice_seconds": 5.0,
        }]}),
    ])
    result = await tools.attach_movie_voice(client, "movie", "man", str(source))
    assert result["voice_seconds"] == 5.0
    assert transport.requests[2].content == b"test audio"
    assert "authorization" not in transport.requests[2].headers
    assert transport.requests[3].headers["authorization"].startswith("Bearer ")
    assert transport.body(3) == {"expected_revision": 7, "asset_key": "uploads/voice"}
    assert transport.requests[3].url.path == "/studio/movies/movie/characters/man/voice"


async def test_attach_movie_voice_rejects_missing_character_before_upload(tmp_path):
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"audio")
    client, transport = _client([httpx.Response(200, json={"characters": []})])
    with pytest.raises(ValueError, match="character not found"):
        await tools.attach_movie_voice(client, "movie", "missing", str(source))
    assert len(transport.requests) == 1


async def test_attach_movie_voice_does_not_retry_conflicting_attachment(tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    client, transport = _client([
        httpx.Response(200, json={"characters": [{"character_id": "man"}]}),
        httpx.Response(200, json={
            "upload_url": "https://storage.test/signed", "asset_key": "uploads/voice",
            "revision": 7,
        }),
        httpx.Response(200),
        httpx.Response(409, json={"detail": "Revision conflict"}),
    ])
    with pytest.raises(StudioApiError):
        await tools.attach_movie_voice(client, "movie", "man", str(source))
    assert len(transport.requests) == 4


async def test_mutate_movie_retries_once_on_revision_conflict():
    client, transport = _client([
        httpx.Response(200, json={"revision": 3}),
        httpx.Response(409, json={"detail": {
            "message": "Revision conflict; current revision is 4", "current_revision": 4}}),
        httpx.Response(200, json={"revision": 5}),
    ])
    result = await client.mutate_movie(
        "PUT", "/studio/movies/m/story", movie_id="m", body={"shots": []})
    assert result == {"revision": 5}
    assert transport.requests[0].url.path == "/studio/movies/m"
    assert transport.body(1)["expected_revision"] == 3
    assert transport.body(2)["expected_revision"] == 4


async def test_mutate_movie_does_not_retry_other_conflicts():
    client, transport = _client([
        httpx.Response(200, json={"revision": 3}),
        httpx.Response(409, json={"detail": "Shot already has an active take"}),
    ])
    with pytest.raises(StudioApiError):
        await client.mutate_movie("POST", "/studio/movies/m/generate", movie_id="m", body={})
    assert len(transport.requests) == 2


async def test_server_registers_movie_tools():
    from magic_video_studio_mcp import server

    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {
        "create_movie", "get_movie", "set_movie_cast", "set_movie_story", "upload_movie_asset",
        "set_shot_opening", "build_movie", "generate_takes", "select_movie_take",
        "wait_for_movie", "set_movie_cut", "set_movie_soundtracks", "assemble_movie",
        "release_movie", "download_movie_release", "attach_movie_voice", "get_movie_capabilities",
    } <= names


async def test_scene_flight_capabilities_reads_what_the_server_can_make():
    client, transport = _client([httpx.Response(200, json={"generation_available": False, "image_types": ["image/png"]})])
    assert (await tools.get_scene_flight_capabilities(client))["generation_available"] is False
    assert transport.requests[0].url.path == "/studio/scene-flights/capabilities"
    from magic_video_studio_mcp import server
    assert "get_scene_flight_capabilities" in {tool.name for tool in await server.mcp.list_tools()}


async def test_movie_capabilities_reads_the_account_limits():
    client, transport = _client([httpx.Response(200, json={"dialogue_available": False, "max_batch_size": 3})])
    result = await tools.get_movie_capabilities(client)
    assert result == {"dialogue_available": False, "max_batch_size": 3}
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url.path == "/studio/movies/capabilities"


def _movie(revision=1, **fields):
    base = {"movie_id": "m", "revision": revision, "characters": [], "shots": [],
            "build": None, "assembly": None, "compositions": {}, "releases": []}
    base.update(fields)
    return base


async def test_wait_for_movie_reconciles_until_settled():
    running = _movie(shots=[{"shot_id": "a", "takes": [{"take_id": "t", "status": "running"}]}])
    done = _movie(shots=[{"shot_id": "a", "takes": [{"take_id": "t", "status": "succeeded"}]}])
    client, transport = _client([
        httpx.Response(200, json=running), httpx.Response(200, json=done)])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True and result["failed"] == []
    assert [r.url.path for r in transport.requests] == ["/studio/movies/m/reconcile"] * 2
    assert sleeps == [5]


async def test_wait_for_movie_stops_on_failure():
    failed = _movie(
        shots=[{"shot_id": "a", "takes": [{"take_id": "t", "status": "failed"}]}],
        assembly={"status": "failed"})
    client, _ = _client([httpx.Response(200, json=failed)])

    async def sleep(seconds):
        raise AssertionError("must not wait")

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True
    assert result["failed"] == ["take a/t", "assembly"]


async def test_wait_for_movie_returns_unsettled_at_timeout():
    running = _movie(build={"status": "generating"})
    client, _ = _client([httpx.Response(200, json=running)] * 3)
    times = iter([0.0, 30.0, 61.0])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_movie(
        client, "m", 60, 30, sleep=sleep, clock=lambda: next(times))
    assert result["settled"] is False


async def test_upload_movie_asset_rejects_unknown_type_before_request(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("x")
    client, transport = _client([])
    with pytest.raises(ValueError, match="png, jpeg, webp, mp4, mp3 or wav"):
        await tools.upload_movie_asset(client, "m", str(source))
    assert transport.requests == []


async def test_upload_movie_asset_puts_bytes_without_key(tmp_path):
    source = tmp_path / "plate.png"
    source.write_bytes(b"png")
    client, transport = _client([
        httpx.Response(200, json={"upload_url": "https://storage.test/up",
                                  "asset_key": "uploads/movies/m/p", "revision": 4}),
        httpx.Response(200),
    ])
    result = await tools.upload_movie_asset(client, "m", str(source))
    assert result == {"asset_key": "uploads/movies/m/p", "revision": 4}
    assert transport.body(0) == {"content_type": "image/png"}
    assert transport.requests[1].content == b"png"
    assert "authorization" not in transport.requests[1].headers


async def test_download_movie_release_streams_from_the_studio_api(tmp_path):
    client, transport = _client([httpx.Response(200, content=b"mp4bytes")])
    result = await tools.download_movie_release(client, "m", "r", str(tmp_path / "out.mp4"))
    assert (tmp_path / "out.mp4").read_bytes() == b"mp4bytes"
    assert result["sha256"] == hashlib.sha256(b"mp4bytes").hexdigest()
    # The release is authorized per request: the key goes to the API, nowhere else.
    assert transport.requests[0].url.path == "/studio/movies/m/releases/r/download"
    assert len(transport.requests) == 1


async def test_wait_for_movie_ignores_settled_composition_history():
    movie = _movie(compositions={
        "c1": {"status": "succeeded", "provider": "pool"},
        "c2": {"status": "superseded", "provider": "pool", "settled": True},
    })
    client, _ = _client([httpx.Response(200, json=movie)])

    async def sleep(seconds):
        raise AssertionError("must not wait")

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True


async def test_wait_for_movie_waits_for_pending_composition():
    pending = _movie(compositions={"c": {"status": "pending", "provider": "pool"}})
    done = _movie(compositions={"c": {"status": "succeeded", "provider": "pool"}})
    client, transport = _client([httpx.Response(200, json=pending), httpx.Response(200, json=done)])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True and len(transport.requests) == 2


async def test_wait_for_movie_old_failure_does_not_end_wait_for_new_take():
    old = {"take_id": "t1", "status": "failed"}
    running = _movie(shots=[{"shot_id": "a", "takes": [old, {"take_id": "t2", "status": "running"}]}])
    done = _movie(shots=[{"shot_id": "a", "takes": [old, {"take_id": "t2", "status": "succeeded"}]}])
    client, transport = _client([httpx.Response(200, json=running), httpx.Response(200, json=done)])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True
    assert result["failed"] == []
    assert len(transport.requests) == 2


async def test_wait_for_movie_keeps_waiting_while_other_work_runs_after_a_failure():
    running = _movie(shots=[
        {"shot_id": "a", "takes": [{"take_id": "t1", "status": "failed"}]},
        {"shot_id": "b", "takes": [{"take_id": "t2", "status": "running"}]}])
    done = _movie(shots=[
        {"shot_id": "a", "takes": [{"take_id": "t1", "status": "failed"}]},
        {"shot_id": "b", "takes": [{"take_id": "t2", "status": "succeeded"}]}])
    client, transport = _client([httpx.Response(200, json=running), httpx.Response(200, json=done)])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["failed"] == ["take a/t1"] and len(transport.requests) == 2


async def test_wait_for_movie_retries_busy_reconcile():
    done = _movie()
    client, transport = _client([
        httpx.Response(409, json={"detail": "Movie is busy; reconcile again"}),
        httpx.Response(200, json=done),
    ])

    async def sleep(seconds):
        pass

    result = await tools.wait_for_movie(client, "m", 60, 5, sleep=sleep, clock=lambda: 0.0)
    assert result["settled"] is True and len(transport.requests) == 2


async def test_set_movie_story_keeps_omitted_settings_and_strips_output_fields():
    current = _movie(3, style="ash noir", brief="b", batch_size=2, compute_target="my_machines")
    shot = {"shot_id": "a", "name": "A", "prompt": "p", "takes": [{"take_id": "t"}],
            "opening_asset_key": "k", "selected_take_id": "t",
            "dialogue": [{"character_id": "mara", "text": "Quiet."}]}
    client, transport = _client([
        httpx.Response(200, json=current),
        httpx.Response(200, json={"revision": 3}),
        httpx.Response(200, json=_movie(4)),
    ])
    await tools.set_movie_story(client, "m", [shot])
    assert transport.body(2) == {
        "expected_revision": 3, "brief": "b", "style": "ash noir", "batch_size": 2,
        "compute_target": "my_machines",
        "shots": [{"shot_id": "a", "name": "A", "prompt": "p",
                   "dialogue": [{"character_id": "mara", "text": "Quiet."}]}],
    }


async def test_set_movie_story_keeps_motion_direction_fields():
    shot = {"shot_id": "a", "name": "A", "prompt": "p", "camera": "Static Shot.",
            "still": "the hull walls", "beats": [{"at_seconds": 1.5, "action": "a shudder", "extra": 1}],
            "takes": []}
    client, transport = _client([
        httpx.Response(200, json=_movie(3, style="s", brief="b", batch_size=2)),
        httpx.Response(200, json={"revision": 3}),
        httpx.Response(200, json=_movie(4)),
    ])
    await tools.set_movie_story(client, "m", [shot])
    assert transport.body(2)["shots"] == [{
        "shot_id": "a", "name": "A", "prompt": "p", "camera": "Static Shot.",
        "still": "the hull walls", "beats": [{"at_seconds": 1.5, "action": "a shudder"}],
    }]


async def test_set_movie_cast_keeps_attached_voice_and_strips_output_fields():
    current = _movie(2, characters=[{"character_id": "mara", "name": "Mara",
                                      "voice_asset_key": "outputs/v.wav", "voice_seconds": 5.0}])
    client, transport = _client([
        httpx.Response(200, json=current),
        httpx.Response(200, json={"revision": 2}),
        httpx.Response(200, json=_movie(3)),
    ])
    await tools.set_movie_cast(client, "m", [
        {"character_id": "mara", "name": "Mara", "appearance": "red coat", "voice_seconds": 5.0}])
    assert transport.body(2)["characters"] == [{
        "character_id": "mara", "name": "Mara", "appearance": "red coat",
        "voice_asset_key": "outputs/v.wav"}]


async def test_upload_movie_asset_maps_mp3_and_rejects_mpeg(tmp_path):
    cue = tmp_path / "bed.mp3"
    cue.write_bytes(b"mp3")
    client, transport = _client([
        httpx.Response(200, json={"upload_url": "https://storage.test/up",
                                  "asset_key": "uploads/movies/m/c", "revision": 2}),
        httpx.Response(200),
    ])
    await tools.upload_movie_asset(client, "m", str(cue))
    assert transport.body(0) == {"content_type": "audio/mpeg"}
    clip = tmp_path / "clip.mpeg"
    clip.write_bytes(b"x")
    with pytest.raises(ValueError):
        await tools.upload_movie_asset(client, "m", str(clip))


async def test_generate_takes_defaults_to_a_fresh_request_id():
    client, transport = _client([
        httpx.Response(200, json=_movie(2)), httpx.Response(200, json=_movie(3)),
        httpx.Response(200, json=_movie(3)), httpx.Response(200, json=_movie(4)),
    ])
    await tools.generate_takes(client, "m", ["a"])
    await tools.generate_takes(client, "m", ["a"])
    first, second = transport.body(1)["request_id"], transport.body(3)["request_id"]
    assert first and second and first != second


async def test_generate_takes_sends_request_and_revision():
    client, transport = _client([
        httpx.Response(200, json=_movie(2)), httpx.Response(200, json=_movie(3))])
    await tools.generate_takes(client, "m", ["a", "b"], "req-1")
    assert transport.requests[1].url.path == "/studio/movies/m/generate"
    assert transport.body(1) == {"expected_revision": 2, "shot_ids": ["a", "b"],
                                 "request_id": "req-1", "select_when_ready": True}


async def test_frame_inspection_is_bounded_and_uses_native_endpoint():
    client, transport = _client([httpx.Response(200, json={"frames": []})])
    await tools.inspect_candidate_frames(client, 'p-1', 'attack', 'c-1', 5, 8, 3)
    assert transport.requests[0].url.path.endswith('/candidates/c-1/frames')
    assert transport.requests[0].url.params['stride'] == '3'
    with pytest.raises(ValueError):
        await tools.inspect_candidate_frames(client, 'p-1', 'attack', 'c-1', limit=107)
    await client.aclose()


async def test_loop_suggestion_is_read_only_and_keeps_incomplete_warning():
    expected = {"status": "incomplete", "warnings": ["missing recovery"]}
    client, transport = _client([httpx.Response(200, json=expected)])
    assert await tools.suggest_motion_loop(client, 'p-1', 'attack', 'c-1', 'attack') == expected
    assert transport.body(0) == {"mode": "attack", "target_frame_count": 12}
    await client.aclose()


async def test_frame_revision_forwards_normalization_and_exact_indices():
    candidate = {"candidate_id": "new", "recipe": {"op": "normalize"}}
    project = {"revision": 4, "manifest": {"motions": [{"motion_id": "attack", "selected_candidate_id": "new", "candidates": [candidate]}]}}
    client, transport = _client([httpx.Response(200, json={"revision": 3}), httpx.Response(200, json=project)])
    normalization = {"scale": .7, "source_anchor_x": 256, "source_anchor_y": 420, "target_anchor_x": 128, "target_anchor_y": 220}
    result = await tools.create_frame_revision(client, 'p-1', 'attack', 'old', [12, 15, 18], 100,
        target_frame_count=12, cell_size=256, normalization=normalization)
    assert transport.body(1)['normalization'] == normalization
    assert transport.body(1)['ordered_frames'] == [12, 15, 18]
    assert result['selected_candidate_id'] == 'new'
    await client.aclose()


def test_studio_request_ids_are_fresh_for_repeated_generation():
    assert tools._new_studio_request_id('motions') != tools._new_studio_request_id('motions')


async def test_frame_tool_returns_mcp_images_not_base64_json(monkeypatch):
    import base64
    from magic_video_studio_mcp import server
    client, _ = _client([httpx.Response(200, json={"frame_count": 1, "frames": [
        {"index": 0, "timestamp_ms": 0, "pose": [0], "edge_contact": False,
         "image": "data:image/png;base64," + base64.b64encode(b'png').decode()}]})])
    monkeypatch.setattr(server, '_client', client)
    result = await server.mcp.call_tool('inspect_candidate_frames',
        {'project_id': 'p-1', 'motion_id': 'attack', 'candidate_id': 'c-1'})
    assert any(getattr(block, 'type', None) == 'image' for block in result)
    await client.aclose()


async def test_download_returns_reference_pivot_without_modifying_walk_candidate():
    walk = {'candidate_id': 'walk', 'sha256': 'a'*64, 'facing': 'right',
            'metadata': {'media_kind': 'sheet', 'width': 3072, 'height': 256, 'frame_count': 12}}
    attack = {'candidate_id': 'attack', 'recipe': {'normalization': {
        'reference_candidate_id': 'walk', 'reference_sha256': 'a'*64,
        'target_anchor_x': 130, 'target_anchor_y': 236}}}
    project = {'manifest': {'motions': [
        {'motion_id': 'walk', 'selected_candidate_id': 'walk', 'candidates': [walk]},
        {'motion_id': 'attack', 'selected_candidate_id': 'attack', 'candidates': [attack]},
    ]}}
    client, _ = _client([httpx.Response(200, json={'upload_url': 'https://assets.test/walk.webm'}),
                         httpx.Response(200, json=project)])
    result = await tools.get_candidate_download(client, 'p-1', 'walk', 'walk', 'webm')
    assert result['frame_geometry'] == {'frame_width': 256, 'frame_height': 256, 'pivot_x': 130, 'pivot_y': 236}
    assert 'pivot_x' not in walk['metadata']
    await client.aclose()


async def test_generation_config_reads_server_defaults_without_mutation():
    expected = {'scope': 'desktop', 'default_settings': {'steps': 8, 'provider_parameters': {'execution_profile': 'h3-w4a8-16gb-lightx8'}}}
    client, transport = _client([httpx.Response(200, json=expected)])
    assert await tools.get_generation_config(client) == expected
    assert transport.requests[0].method == 'GET'
    assert transport.requests[0].url.path == '/studio/generation-config'
    await client.aclose()


async def test_list_takes_preserves_recipe_identity_and_geometry_for_remote_review():
    candidate = dict(candidate_id='take', artifact_key='outputs/take.png', duration_ms=1200,
                     facing='right', source='generated', assessment=None, sha256='a'*64,
                     metadata={'frame_count': 12, 'pivot_x': 130, 'pivot_y': 236},
                     generation_settings={'steps': 8}, recipe=None, ordered_frames=None,
                     frame_duration_ms=100, loop_policy='loop')
    project = _project(1)
    project['manifest']['motions'] = [{'motion_id': 'walk', 'selected_candidate_id': 'take', 'candidates': [candidate]}]
    client, _ = _client([httpx.Response(200, json=project)])
    take = (await tools.list_takes(client, 'p-1', 'walk'))['takes'][0]
    assert take['sha256'] == candidate['sha256']
    assert take['generation_settings']['steps'] == 8
    assert take['metadata']['pivot_x'] == 130
    await client.aclose()


async def test_quality_summary_uses_project_assessment_not_a_candidate():
    project = _project(2)
    project['manifest'].update(set_assessment={'policy_version': 'set-v1', 'findings': []}, set_assessment_fresh=True)
    client, _ = _client([httpx.Response(200, json=_project(1)), httpx.Response(200, json=project)])
    result = await tools.run_quality(client, 'p-1')
    assert result['fresh'] is True
    assert result['policy_version'] == 'set-v1'
    await client.aclose()


def _view_project(revision=5):
    project = _project(revision)
    project['manifest'].update(sprite_layout='isometric', views=[dict(view_id='N', name='North',
        reference=dict(revision_id='ref-1', key='source-n', sha256='a'*64, width=64, height=80))])
    return project


def _view_motion():
    return dict(motion_id='n-walk', name='Walk', kind='preset', preset='walk',
        view_id='N', view_reference=dict(revision_id='ref-1', key='source-n', sha256='a'*64, width=64, height=80),
        action='walks', prompt='compiled server prompt', prompt_source='preset', semantic_slot='walk',
        loop_policy='loop', required=True, references=[], style='high_fantasy', script='keep both axes',
        candidates=[], selected_candidate_id='chosen', spec_digest='spec-1')


async def test_configure_layout_and_view_omission_and_explicit_clear():
    project = _view_project()
    client, transport = _client([httpx.Response(200, json=project) for _ in range(6)])
    await tools.configure_sprite_layout(client, 'p-1', 'isometric')
    await tools.set_character_view(client, 'p-1', 'N', name='North')
    await tools.set_character_view(client, 'p-1', 'N', clear_reference=True)
    assert transport.body(1) == dict(expected_revision=5, sprite_layout='isometric')
    assert transport.body(3) == dict(expected_revision=5, name='North')
    assert transport.body(5) == dict(expected_revision=5, reference=None)
    assert str(transport.requests[1].url).endswith('/sprite-layout')
    await client.aclose()


async def test_view_upload_binds_matching_bytes_without_credentials_and_same_revision():
    import base64
    project = _view_project()
    client, transport = _client([httpx.Response(200, json=project),
        httpx.Response(200, json={'key': 'source-n-2', 'upload_url': 'https://upload.test/image'}),
        httpx.Response(200), httpx.Response(200, json=project)])
    result = await tools.upload_view_reference_base64(client, 'p-1', 'N', base64.b64encode(b'image').decode())
    assert transport.requests[2].content == b'image'
    assert 'authorization' not in transport.requests[2].headers
    assert transport.body(3) == dict(expected_revision=5, reference=dict(key='source-n-2', sha256=hashlib.sha256(b'image').hexdigest()))
    assert result['views'][0]['reference']['revision_id'] == 'ref-1'
    assert result['sprite_layout'] == 'isometric'
    await client.aclose()


async def test_view_upload_conflict_never_retries_over_concurrent_reference():
    project = _view_project()
    client, transport = _client([httpx.Response(200, json=project),
        httpx.Response(200, json={'key': 'source', 'upload_url': '/upload'}), httpx.Response(200),
        httpx.Response(409, json={'detail': {'current_revision': 6}})])
    with pytest.raises(StudioApiError):
        await tools.upload_view_reference_base64(client, 'p-1', 'N', 'YQ==')
    assert len(transport.requests) == 4
    await client.aclose()


async def test_default_view_motions_have_durable_ids_and_skip_compiled_existing():
    project = _view_project()
    project['manifest']['motions'] = [_view_motion()]
    client, transport = _client([httpx.Response(200, json=project), httpx.Response(200, json=project)])
    result = await tools.add_view_motions(client, 'p-1', 'N')
    assert result['added_motion_ids'] == ['n-idle', 'n-attack']
    assert result['skipped_motion_ids'] == ['n-walk']
    definitions = transport.body(1)['motions']
    assert [m['view_id'] for m in definitions] == ['N', 'N']
    assert definitions[1]['preset'] == 'attack'
    assert definitions[1]['loop_policy'] == 'once'
    assert result['motions'][0]['view_reference']['sha256'] == 'a'*64
    await client.aclose()


async def test_view_motion_conflict_does_not_overwrite():
    project = _view_project()
    motion = _view_motion()
    motion['view_id'] = 'S'
    project['manifest']['motions'] = [motion]
    client, transport = _client([httpx.Response(200, json=project)])
    with pytest.raises(ValueError, match='conflicting'):
        await tools.add_view_motions(client, 'p-1', 'N')
    assert len(transport.requests) == 1
    await client.aclose()


async def test_custom_view_motion_and_patch_preserve_full_spec_and_selection():
    project = _view_project()
    project['manifest']['motions'] = [_view_motion()]
    client, transport = _client([httpx.Response(200, json=project) for _ in range(4)])
    await tools.add_view_motions(client, 'p-1', 'N', [dict(motion_id='n-roar', name='Roar', kind='custom', prompt='roars')])
    assert transport.body(1)['motions'][0]['prompt_source'] == 'custom'
    result = await tools.update_view_motion(client, 'p-1', 'n-walk', {'action': 'walks holding both axes'})
    body = transport.body(3)
    expected = {k: v for k, v in _view_motion().items() if k in tools._MOTION_DEFINITION_FIELDS}
    assert body == {**expected, 'action': 'walks holding both axes', 'expected_revision': 5}
    assert 'view_reference' not in body
    assert result['motions'][0]['selected_candidate_id'] == 'chosen'
    await client.aclose()


@pytest.mark.parametrize('call', [
    lambda c: tools.configure_sprite_layout(c, 'p-1', 'mystery'),
    lambda c: tools.set_character_view(c, 'p-1', 'N', reference={'key':'x','sha256':'a'*64}, clear_reference=True),
    lambda c: tools.upload_view_reference_base64(c, 'p-1', 'N', '!'),
    lambda c: tools.upload_view_reference_base64(c, 'p-1', 'N', 'YQ==', 'text/plain'),
    lambda c: tools.add_view_motions(c, 'p-1', 'N', []),
    lambda c: tools.add_view_motions(c, 'p-1', 'N', [dict(motion_id='n-roar',name='Roar',kind='custom')]),
    lambda c: tools.update_view_motion(c, 'p-1', 'n-walk', {'view_reference':None}),
])
async def test_invalid_view_inputs_do_not_call_api(call):
    client, transport = _client([])
    with pytest.raises(ValueError):
        await call(client)
    assert not transport.requests
    await client.aclose()


async def test_layout_incompatible_view_rejected_without_mutation():
    client, transport = _client([httpx.Response(200, json=_view_project())])
    with pytest.raises(ValueError, match='layout'):
        await tools.set_character_view(client, 'p-1', 'left')
    assert len(transport.requests) == 1
    await client.aclose()


async def test_generate_resume_request_id_and_immutable_evidence_are_exposed():
    operation = dict(job_id='job-1', operation='generate_motion_v1', motion_id='n-walk', status='queued',
        stage='queued', failure_code=None, failure_explanation=None, prompt='immutable actual prompt',
        generation_settings={'steps': 8}, requested_settings={'steps': 8}, resolved_recipe={'worker_id':'worker-1'},
        execution_provenance={'worker_id':'worker-1'}, generation_evidence=dict(motion_spec_digest='old-spec',
            view_id='N', view_reference={'revision_id':'ref-1'}, start_frame_sha256='b'*64))
    client, transport = _client([httpx.Response(200, json=_view_project()),
        httpx.Response(200, json={'queued':[{'motion_id':'n-walk','operation':operation}], 'failed':[]})])
    result = await tools.generate_motions(client, 'p-1', ['n-walk'], settings={'worker_id':'worker-1'}, request_id='durable-1')
    assert transport.body(1)['request_id'] == 'durable-1'
    assert transport.body(1)['settings'] == {'worker_id':'worker-1'}
    assert result['queued'][0]['generation_evidence'] == operation['generation_evidence']
    assert result['queued'][0]['prompt'] == 'immutable actual prompt'
    assert tools._operation_summary(operation)['resolved_recipe'] == {'worker_id':'worker-1'}
    await client.aclose()


async def test_view_tools_typed_discovery_and_wrapper(monkeypatch):
    from magic_video_studio_mcp import server
    registered = {tool.name:tool for tool in await server.mcp.list_tools()}
    for name in ('configure_sprite_layout','set_character_view','upload_view_reference_base64','add_view_motions','update_view_motion'):
        assert name in registered
    assert not {'set_walk_direction','add_directional_walk_motions'} & registered.keys()
    assert registered['set_character_view'].inputSchema['properties']['view_id']['enum'] == ['left','right','N','NE','E','SE','S','SW','W','NW']
    client, transport = _client([httpx.Response(200, json=_view_project()) for _ in range(2)])
    monkeypatch.setattr(server, '_client', client)
    result = json.loads(await server.configure_sprite_layout('p-1','isometric'))
    assert result['sprite_layout'] == 'isometric'
    assert transport.body(1)['sprite_layout'] == 'isometric'
    await client.aclose()


async def test_view_calibration_requires_explicit_same_view_and_legacy_keeps_default():
    project = _view_project()
    project['manifest']['motions'] = [_view_motion(), {**_view_motion(), 'motion_id':'n-attack','preset':'attack'},
        {**_view_motion(), 'motion_id':'walk','view_id':None}]
    client, transport = _client([httpx.Response(200, json=project) for _ in range(2)])
    with pytest.raises(ValueError, match='explicit'):
        await tools.calibrate_motion_body(client, 'p-1', 'n-attack', 'a', 0, 'b')
    with pytest.raises(ValueError, match='same view'):
        await tools.calibrate_motion_body(client, 'p-1', 'n-attack', 'a', 0, 'b', reference_motion_id='walk')
    assert len(transport.requests) == 2
    await client.aclose()
    client, transport = _client([httpx.Response(200, json=project), httpx.Response(200, json={'scale':1}),
        httpx.Response(200, json=project), httpx.Response(200, json={'scale':1})])
    await tools.calibrate_motion_body(client, 'p-1', 'n-attack', 'a', 0, 'b', reference_motion_id='n-walk')
    assert transport.body(1)['reference_motion_id'] == 'n-walk'
    await tools.calibrate_motion_body(client, 'p-1', 'walk', 'a', 0, 'b')
    assert transport.body(3)['reference_motion_id'] == 'walk'
    await client.aclose()


@pytest.mark.parametrize('mode', ['idle', 'walk', 'attack'])
async def test_loop_mode_http_preserves_source_timing_and_proposal(mode):
    proposal = dict(status='suggested', ordered_frames=list(range(12)), frame_duration_ms=84,
        source_frame_rate='24000/1001', source_time_base='1/24000', warnings=['review timed motion'])
    client, transport = _client([httpx.Response(200, json=proposal)])
    assert await tools.suggest_motion_loop(client, 'p-1', 'n-idle', 'c-1', mode) == proposal
    assert transport.body(0) == {'mode':mode, 'target_frame_count':12}
    assert len(transport.requests) == 1  # A suggestion never calls the revision/save route.
    await client.aclose()


async def test_loop_mode_schema_and_idle_wrapper(monkeypatch):
    from magic_video_studio_mcp import server
    registered = {tool.name:tool for tool in await server.mcp.list_tools()}
    assert registered['suggest_motion_loop'].inputSchema['properties']['mode']['enum'] == ['idle','walk','attack']
    client, transport = _client([httpx.Response(200, json={'status':'incomplete','warnings':['frozen source']})])
    monkeypatch.setattr(server, '_client', client)
    result = json.loads(await server.suggest_motion_loop('p-1','n-idle','c-1','idle'))
    assert result['status'] == 'incomplete'
    assert transport.body(0)['mode'] == 'idle'
    await client.aclose()


@pytest.mark.parametrize('mode,count', [('freeze',12), ('idle',9)])
async def test_invalid_loop_mode_or_count_never_calls_api(mode,count):
    client, transport = _client([])
    with pytest.raises(ValueError):
        await tools.suggest_motion_loop(client,'p-1','n-idle','c-1',mode,count)
    assert not transport.requests
    await client.aclose()


async def test_setup_parity_preserves_exact_matrix_and_revision_fences():
    desired = {"schema_version": 1, "sprite_layout": "single_view", "views": [
        {"view_id": "single", "heading": None, "name": "Original view", "motions": [
            {"motion_id": "death", "name": "Death", "preset": "death"}]}]}
    client, transport = _client([httpx.Response(200, json={"schema_versions": [1]}),
        httpx.Response(200, json={"revision": 7, "setup": desired}),
        httpx.Response(200, json={"plan_token": "plan", "expected_revision": 7}),
        httpx.Response(409, json={"detail": {"code": "setup_conflict"}})])
    assert await tools.get_setup_capabilities(client) == {"schema_versions": [1]}
    assert (await tools.get_setup(client, "project"))["setup"] == desired
    await tools.preview_setup(client, "project", 7, desired)
    with pytest.raises(StudioApiError):
        await tools.apply_setup(client, "project", 7, "plan", ["remove-one"])
    assert transport.body(2) == {"expected_revision": 7, "desired_setup": desired}
    assert transport.body(3) == {"expected_revision": 7, "plan_token": "plan", "confirmed_impacts": ["remove-one"]}
    assert len(transport.requests) == 4
    assert all(request.headers["X-Studio-Setup-Version"] == "1" for request in transport.requests)
    await client.aclose()


async def test_setup_reference_staging_uses_same_server_boundary():
    client, transport = _client([httpx.Response(200, json={"revision_id": "verified"})])
    assert await tools.upload_setup_reference_base64(client, "project", "cG5n") == {"revision_id": "verified"}
    assert transport.body(0) == {"content_base64": "cG5n", "content_type": "image/png"}
    assert transport.requests[0].url.path == "/studio/projects/project/setup/references"
    await client.aclose()


async def test_dispatch_recovery_preserves_explicit_revision_and_confirmation_without_retry():
    client, transport = _client([httpx.Response(409, json={"detail": {"code": "setup_blocked"}})])
    with pytest.raises(StudioApiError):
        await tools.abandon_setup_dispatch(client, "project", "operation", 8, True)
    assert transport.body(0) == {"expected_revision": 8, "confirm_dispatch_stopped": True}
    assert len(transport.requests) == 1
    assert transport.requests[0].url.path == "/studio/projects/project/setup/dispatches/operation/abandon"
    await client.aclose()


async def test_upload_sends_the_grants_signed_size_header():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200)
    client = StudioClient(base_url='http://127.0.0.1:5173', api_key='private',
                          transport=httpx.MockTransport(handle))
    await client.put_file('https://storage.invalid/signed', b'png', 'image/png',
                          {'x-goog-content-length-range': '0,20000000'})
    assert requests[0].headers['x-goog-content-length-range'] == '0,20000000'
    assert requests[0].headers['Content-Type'] == 'image/png'
    assert 'Authorization' not in requests[0].headers
    await client.aclose()


TURN = "turnaround-" + "a" * 32
DRAFT = "draft-" + "b" * 32


async def test_a_turnaround_starts_on_the_current_revision_with_a_fresh_request_id() -> None:
    client, transport = _client([httpx.Response(200, json=_project(4)),
                                 httpx.Response(200, json={"turnaround_id": TURN, "operation": {"job_id": "j"}})])
    result = await tools.start_turnaround(client, "p-1", "S", "rev-s", [
        {"item": "axe", "attachment": "hand", "source_picture_side": "left"}])
    assert result["turnaround_id"] == TURN
    sent = transport.body(1)
    assert transport.requests[1].url.path == "/studio/projects/p-1/turnarounds"
    assert sent["expected_revision"] == 4 and sent["source_view_id"] == "S" and sent["source_revision_id"] == "rev-s"
    assert sent["equipment"][0]["item"] == "axe" and sent["request_id"] and "settings" not in sent
    await client.aclose()


async def test_saving_picks_sends_the_pick_revision_and_never_retries_a_stale_one() -> None:
    client, transport = _client([httpx.Response(409, json={"detail": {"message": "stale", "pick_revision": 2}})])
    with pytest.raises(StudioApiError) as stale:
        await tools.save_turnaround_picks(client, "p-1", TURN, 1, {"N": {"frame_index": 52}})
    assert stale.value.status_code == 409 and len(transport.requests) == 1
    assert transport.requests[0].method == "PUT"
    assert transport.requests[0].url.path == f"/studio/projects/p-1/turnarounds/{TURN}/picks"
    assert transport.body(0) == {"expected_pick_revision": 1, "picks": {"N": {"frame_index": 52}}}
    await client.aclose()


async def test_drafts_are_made_checked_and_discarded_on_the_current_revision() -> None:
    draft = {"draft_id": DRAFT, "heading": "N", "bindable": False}
    client, transport = _client([
        httpx.Response(200, json=_project(5)), httpx.Response(200, json={"draft_id": DRAFT, "draft": draft}),
        httpx.Response(200, json=_project(6)), httpx.Response(200, json={**draft, "bindable": True}),
        httpx.Response(200, json=_project(7)), httpx.Response(200, json={**draft, "discarded": True}),
    ])
    created = await tools.create_view_draft(client, "p-1", TURN, 1, "N", request_id="r-1")
    assert created["draft_id"] == DRAFT
    assert transport.body(1) == {"turnaround_id": TURN, "pick_revision": 1, "heading": "N", "standing": False,
                                 "request_id": "r-1", "expected_revision": 5}
    await tools.check_view_draft(client, "p-1", DRAFT, True, True, True)
    assert transport.requests[3].url.path == f"/studio/projects/p-1/view-drafts/{DRAFT}/check"
    assert transport.body(3) == {"facing": True, "equipment": True, "identity": True, "expected_revision": 6}
    await tools.discard_view_draft(client, "p-1", DRAFT)
    assert transport.requests[5].url.path == f"/studio/projects/p-1/view-drafts/{DRAFT}/discard"
    assert transport.body(5) == {"expected_revision": 7}
    await client.aclose()


async def test_turnaround_frames_are_bounded_like_candidate_frames() -> None:
    client, transport = _client([httpx.Response(200, json={"frames": []})])
    with pytest.raises(ValueError):
        await tools.inspect_turnaround_frames(client, "p-1", TURN, limit=13)
    await tools.inspect_turnaround_frames(client, "p-1", TURN, start=40, limit=4, stride=6)
    assert str(transport.requests[0].url).endswith(f"/turnarounds/{TURN}/frames?start=40&limit=4&stride=6")
    await client.aclose()


def test_the_check_tool_tells_agents_it_needs_the_persons_go_ahead() -> None:
    from magic_video_studio_mcp import server
    tools_by_name = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert {"start_turnaround", "get_turnaround", "inspect_turnaround_frames", "save_turnaround_picks",
            "create_view_draft", "list_view_drafts", "check_view_draft", "discard_view_draft"} <= set(tools_by_name)
    assert "explicit go-ahead" in tools_by_name["check_view_draft"].description
    assert "draft-" in tools_by_name["preview_setup"].description
