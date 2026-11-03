import httpx
import pytest

from magic_video_editor.audio import freesound
from magic_video_editor.audio.freesound import (
    FreesoundClient,
    FreesoundRateLimited,
    FreesoundRefused,
    FreesoundUnavailable,
    licence_for,
    parse_sound,
)

CC0 = "http://creativecommons.org/publicdomain/zero/1.0/"
BY4 = "https://creativecommons.org/licenses/by/4.0/"
NC = "https://creativecommons.org/licenses/by-nc/4.0/"


def item(id=1, license=CC0, preview="https://cdn.freesound.org/previews/0/1_1-hq.mp3", duration=2.5):
    return {
        "id": id, "name": f"glass {id}", "username": "qubodup", "license": license,
        "duration": duration, "previews": {"preview-hq-mp3": preview},
        "url": f"https://freesound.org/people/qubodup/sounds/{id}/", "tags": ["glass", "crack"],
    }


def test_only_cc0_passes_a_cc0_set_and_cc_by_needs_the_opt_in():
    assert licence_for(CC0, "cc0") == ("CC0-1.0", False)
    assert licence_for(BY4, "cc0") is None
    assert licence_for(BY4, "cc0_cc_by") == ("CC-BY-4.0", True)
    assert licence_for(NC, "cc0_cc_by") is None
    assert licence_for("https://example.com/publicdomain/zero/1.0/", "cc0") is None
    assert licence_for("https://creativecommons.org/licenses/sampling+/1.0/", "cc0_cc_by") is None


def test_a_result_with_a_preview_off_the_cdn_or_a_bad_licence_is_dropped():
    assert parse_sound(item(), "cc0").spdx == "CC0-1.0"
    assert parse_sound(item(preview="https://evil.example/x.mp3"), "cc0") is None
    assert parse_sound(item(preview="http://cdn.freesound.org/previews/x.mp3"), "cc0") is None
    assert parse_sound(item(license=NC), "cc0_cc_by") is None
    assert parse_sound({"id": "x"}, "cc0") is None


def transport(results, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/apiv2/sounds/"):
            return httpx.Response(200, json=results[0])
        return httpx.Response(200, json={"results": results})
    return httpx.MockTransport(handler)


async def test_search_filters_by_licence_in_the_query_and_again_per_result():
    seen = []
    client = FreesoundClient("token-123", transport=transport([item(1), item(2, license=NC), item(3, license=BY4)], seen))
    found = await client.search("owner", "glass crack", "cc0", min_seconds=1, max_seconds=10)
    assert [s.source_id for s in found] == ["1"]
    request = seen[0]
    assert request.headers["authorization"] == "Token token-123"
    assert "token-123" not in str(request.url)
    assert request.url.params["filter"] == 'license:"Creative Commons 0" duration:[1 TO 10]'
    with_by = FreesoundClient("t", transport=transport([item(1), item(3, license=BY4)], []))
    assert [s.spdx for s in await with_by.search("owner", "glass", "cc0_cc_by")] == ["CC0-1.0", "CC-BY-4.0"]


async def test_identical_searches_are_cached_and_each_account_has_a_budget(monkeypatch):
    seen = []
    client = FreesoundClient("t", transport=transport([item(1)], seen))
    await client.search("a", "Glass", "cc0")
    await client.search("b", "glass ", "cc0")
    assert len(seen) == 1
    monkeypatch.setattr(freesound, "SEARCHES_PER_MINUTE", 2)
    await client.search("a", "two", "cc0")
    with pytest.raises(FreesoundRateLimited):
        await client.search("a", "three", "cc0")
    await client.search("b", "three", "cc0")


async def test_without_a_token_search_is_unavailable_and_a_licence_change_is_refused():
    with pytest.raises(FreesoundUnavailable):
        await FreesoundClient(None).search("a", "glass", "cc0")
    assert FreesoundClient("  ").available is False
    client = FreesoundClient("t", transport=transport([item(7, license=BY4)], []))
    with pytest.raises(FreesoundRefused):
        await client.sound("a", "7", "cc0")
    with pytest.raises(FreesoundRefused):
        await client.sound("a", "../7", "cc0")


async def test_upstream_failures_are_unavailable_not_crashes():
    client = FreesoundClient("t", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(FreesoundUnavailable):
        await client.search("a", "glass", "cc0")
    limited = FreesoundClient("t", transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(FreesoundRateLimited):
        await limited.sound("a", "1", "cc0")


async def test_imports_spend_the_same_budget_as_searches(monkeypatch):
    seen = []
    client = FreesoundClient("t", transport=transport([item(1)], seen))
    monkeypatch.setattr(freesound, "SEARCHES_PER_MINUTE", 2)
    await client.search("a", "glass", "cc0")
    await client.sound("a", "1", "cc0")
    with pytest.raises(FreesoundRateLimited):
        await client.sound("a", "999", "cc0")
    assert len(seen) == 2
    await client.sound("b", "1", "cc0")
