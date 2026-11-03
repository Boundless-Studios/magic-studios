"""Audio set tools against a scripted transport."""

from __future__ import annotations

import hashlib
import json

import httpx
from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioClient


class Script(httpx.MockTransport):
    def __init__(self, responses):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        return self._responses.pop(0)

    def body(self, index):
        return json.loads(self.requests[index].content)


def _client(responses):
    transport = Script(responses)
    return StudioClient(base_url="https://studio.test", api_key="mvs_" + "a" * 32, transport=transport), transport


def _set(revision=1, **overrides):
    return {"set_id": "s-1", "name": "Glass", "brief": "", "licence_policy": "cc0", "revision": revision,
            "cues": [], "candidates": [], "releases": [], **overrides}


async def test_server_registers_audio_tools():
    from magic_video_studio_mcp import server

    names = {tool.name for tool in await server.mcp.list_tools()}
    assert {
        "get_audio_capabilities", "create_audio_set", "get_audio_set", "set_audio_set",
        "search_free_sounds", "import_free_sound", "upload_audio_candidate", "replace_audio_original",
        "review_audio_candidate", "select_audio_candidates", "get_audio_readiness",
        "release_audio_set", "download_audio_release", "send_audio_to_movie",
    } <= names


async def test_set_audio_set_keeps_the_licence_policy_and_drops_output_fields():
    current = _set(3, licence_policy="cc0_cc_by", brief="glass")
    client, transport = _client([httpx.Response(200, json=current), httpx.Response(200, json=_set(4))])
    await tools.set_audio_set(client, "s-1", [{"cue_id": "crack", "name": "Crack", "selected": ["x"]}])
    body = transport.body(1)
    assert transport.requests[1].method == "PUT" and transport.requests[1].url.path == "/studio/audio/s-1"
    assert body == {"name": "Glass", "brief": "glass", "licence_policy": "cc0_cc_by",
                    "cues": [{"cue_id": "crack", "name": "Crack"}], "expected_revision": 3}
    await client.aclose()


async def test_a_stale_revision_is_retried_once_with_the_reported_revision():
    conflict = httpx.Response(409, json={"detail": {"message": "stale", "current_revision": 6}})
    listened = _set(5, candidates=[{"candidate_id": "c-1", "sha256": "a" * 64}])
    client, transport = _client([httpx.Response(200, json=listened), conflict, httpx.Response(200, json=_set(7))])
    await tools.review_audio_candidate(client, "s-1", "c-1", "keep", note="clean")
    assert transport.body(1)["expected_revision"] == 5
    # The retry keeps the hash of the file that was read, so a replaced file is refused.
    assert transport.body(2) == {"outcome": "keep", "note": "clean", "reason": "", "sha256": "a" * 64, "expected_revision": 6}
    assert transport.requests[2].url.path == "/studio/audio/s-1/candidates/c-1/review"
    await client.aclose()


async def test_search_quotes_the_query_and_import_names_freesound():
    client, transport = _client([httpx.Response(200, json=[]), httpx.Response(200, json=_set(2)),
                                 httpx.Response(200, json=_set(3))])
    await tools.search_free_sounds(client, "s-1", "glass & crack", 1, 10)
    assert transport.requests[0].url.params["q"] == "glass & crack"
    assert transport.requests[0].url.params["max_seconds"] == "10"
    await tools.import_free_sound(client, "s-1", "crack", "101")
    assert transport.body(2) == {"source": "freesound", "source_id": "101", "expected_revision": 2}
    await client.aclose()


async def test_upload_puts_bytes_to_the_grant_without_credentials(tmp_path):
    source = tmp_path / "mine.wav"
    source.write_bytes(b"RIFF....WAVE")
    grant = {"asset_key": "uploads/audio/s-1/u", "upload_url": "https://storage.test/put", "upload_headers": {}, "revision": 2}
    client, transport = _client([httpx.Response(200, json=grant), httpx.Response(200),
                                 httpx.Response(200, json=_set(2)), httpx.Response(200, json=_set(3))])
    await tools.upload_audio_candidate(client, "s-1", "crack", str(source))
    assert transport.body(0) == {"content_type": "audio/wav"}
    assert str(transport.requests[1].url) == "https://storage.test/put"
    assert "Authorization" not in transport.requests[1].headers
    assert transport.body(3) == {"asset_key": "uploads/audio/s-1/u", "filename": "mine.wav", "expected_revision": 2}
    await client.aclose()


async def test_download_and_send_to_movie(tmp_path):
    client, transport = _client([
        httpx.Response(200, content=b"zipbytes"),
        httpx.Response(200, json={"movie_id": "m", "revision": 4}),
        httpx.Response(409, json={"detail": {"message": "stale", "current_revision": 5}}),
        httpx.Response(200, json={"movie_id": "m", "revision": 7}),
    ])
    result = await tools.download_audio_release(client, "s-1", "r-1", str(tmp_path / "pack.zip"))
    assert result["sha256"] == hashlib.sha256(b"zipbytes").hexdigest()
    assert transport.requests[0].url.path == "/studio/audio/s-1/releases/r-1/download"
    moved = await tools.send_audio_to_movie(client, "s-1", "c-1", "m")
    assert moved["revision"] == 7
    assert transport.body(2) == {"movie_id": "m", "movie_revision": 4}
    assert transport.body(3) == {"movie_id": "m", "movie_revision": 5}
    await client.aclose()


async def test_set_audio_set_rebuilds_kept_settings_from_fresh_state_after_a_conflict():
    """A browser switched the policy to cc0 between the read and the write: the retry
    keeps the new policy instead of restoring the old one with a newer revision."""
    conflict = httpx.Response(409, json={"detail": {"message": "stale", "current_revision": 4}})
    client, transport = _client([
        httpx.Response(200, json=_set(3, licence_policy="cc0_cc_by")), conflict,
        httpx.Response(200, json=_set(4, licence_policy="cc0")), httpx.Response(200, json=_set(5)),
    ])
    await tools.set_audio_set(client, "s-1", [{"cue_id": "crack", "name": "Crack"}])
    assert transport.body(1)["expected_revision"] == 3 and transport.body(1)["licence_policy"] == "cc0_cc_by"
    assert transport.body(3)["expected_revision"] == 4 and transport.body(3)["licence_policy"] == "cc0"
    await client.aclose()


async def test_a_verdict_on_a_replaced_file_is_not_retried():
    changed = httpx.Response(409, json={"detail": {"code": "candidate_file_changed", "message": "changed"}})
    listened = _set(5, candidates=[{"candidate_id": "c-1", "sha256": "a" * 64}])
    client, transport = _client([httpx.Response(200, json=listened), changed])
    import pytest
    from magic_video_studio_mcp.client import StudioApiError
    with pytest.raises(StudioApiError):
        await tools.review_audio_candidate(client, "s-1", "c-1", "keep")
    assert len(transport.requests) == 2
    await client.aclose()
