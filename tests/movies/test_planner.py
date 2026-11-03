import httpx
import pytest

from magic_video_editor.movies.planner import HttpMoviePlanner, PlanError


async def test_planner_sends_separate_opening_and_motion_contract_and_validates_budget():
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "shots": [
                    {
                        "shot_id": "arrival",
                        "name": "Arrival",
                        "opening_prompt": "An empty landing platform at dusk.",
                        "prompt": "The ship settles onto the platform.",
                        "duration_seconds": 4,
                        "continuation_of": None,
                    }
                ]
            },
        )

    planner = HttpMoviePlanner(
        "https://planner.test/plan",
        "planner-audience",
        token_provider=lambda audience: "test-token",
        transport=httpx.MockTransport(respond),
    )
    result = await planner.plan(brief="A ship arrives.", style="Ash grey", shot_count=1)
    assert result[0].prompt != result[0].opening_prompt
    assert captured[0].headers["Authorization"] == "Bearer test-token"
    assert b"one clear action" in captured[0].content
    assert b"max_duration_seconds" in captured[0].content


@pytest.mark.parametrize(
    "shots",
    [
        [{"shot_id": "a", "name": "A", "prompt": "Move", "duration_seconds": 15.5}],
        [
            {
                "shot_id": "a",
                "name": "A",
                "prompt": "Move",
                "duration_seconds": 4,
                "continuation_of": "a",
            }
        ],
        [
            {
                "shot_id": "a",
                "name": "A",
                "prompt": "Move",
                "duration_seconds": 4,
                "continuation_of": "missing",
            }
        ],
        [],
        # Fits the old style+action check but not the structured motion prompt H3 receives.
        [{"shot_id": "a", "name": "A", "prompt": "Move " * 390, "duration_seconds": 4}],
    ],
)
async def test_invalid_provider_plan_is_rejected_without_fallback(shots):
    planner = HttpMoviePlanner(
        "https://planner.test/plan",
        "aud",
        token_provider=lambda _: "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"shots": shots})
        ),
    )
    with pytest.raises(PlanError):
        await planner.plan(brief="A story", style="", shot_count=1)


async def test_provider_errors_do_not_expose_response_body():
    planner = HttpMoviePlanner(
        "https://planner.test/plan",
        "aud",
        token_provider=lambda _: "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="private provider diagnostics")
        ),
    )
    with pytest.raises(PlanError, match="unavailable") as failure:
        await planner.plan(brief="Story", style="", shot_count=1)
    assert "private provider" not in str(failure.value)
