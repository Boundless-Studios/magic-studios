"""Optional hosted shot director. Suggestions never submit GPU work or save a plan."""

import asyncio
from collections.abc import Callable, Mapping

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from pydantic import ValidationError

from magic_video_editor.movies.dialogue import MAX_SHOT_SECONDS
from magic_video_editor.movies import motion
from magic_video_editor.movies.models import ShotInput

DIRECTOR_INSTRUCTIONS = """Plan a coherent short movie with a limited clip budget.
Give every shot one clear action and one restrained camera move. Do not put cuts
inside a shot. Keep subject identity, props, palette and environment consistent.
Write opening_prompt as only the visible state at time zero; keep subsequent
actions exclusively in prompt. The opening must not show the outcome prematurely.
Use continuation_of only when a shot must start from its predecessor's final frame;
independent scenes should have null so they can run across different GPU workers.
Reference only earlier shot_ids, with no cycles. Preserve the brief's point of
view, opening event and beat order. Spend a separate shot on essential motivation
or story information rather than hiding it in background details. Each shot must
fit max_duration_seconds; never silently turn a long shot into a stitched request.
Return JSON {shots:[{shot_id,name,opening_prompt,prompt,duration_seconds,continuation_of}]}.
"""


class PlanError(Exception):
    """A safe, public planner failure; the manual shot plan remains available."""


class HttpMoviePlanner:
    def __init__(
        self,
        endpoint: str,
        audience: str,
        *,
        token_provider: Callable[[str], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not endpoint.startswith("https://") or not audience:
            raise ValueError("Movie planner requires an HTTPS endpoint and audience")
        self.endpoint = endpoint
        self.audience = audience
        self.token_provider = token_provider or (
            lambda target: id_token.fetch_id_token(Request(), target)
        )
        self.transport = transport

    async def plan(self, *, brief: str, style: str, shot_count: int) -> list[ShotInput]:
        if not brief.strip() or not 1 <= shot_count <= 20:
            raise PlanError("Describe the movie and choose between 1 and 20 shots.")
        try:
            async with asyncio.timeout(60):
                token = await asyncio.to_thread(self.token_provider, self.audience)
                async with (
                    httpx.AsyncClient(
                        timeout=45, trust_env=False, transport=self.transport
                    ) as client,
                    client.stream(
                        "POST",
                        self.endpoint,
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "brief": brief,
                            "style": style,
                            "shot_count": shot_count,
                            "min_duration_seconds": 1.5,
                            "duration_step_seconds": 0.5,
                            "max_duration_seconds": MAX_SHOT_SECONDS,
                            "instructions": DIRECTOR_INSTRUCTIONS,
                        },
                    ) as response,
                ):
                    if response.status_code != 200:
                        raise PlanError(
                            "The shot planner is unavailable. Try again or write the shots manually."
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 256_000:
                            raise PlanError(
                                "The shot planner returned an oversized plan."
                            )
                    payload = httpx.Response(200, content=bytes(content)).json()
        except PlanError:
            raise
        except Exception as exc:
            raise PlanError(
                "The shot planner is unavailable. Try again or write the shots manually."
            ) from exc
        try:
            shots = [ShotInput.model_validate(shot) for shot in payload["shots"]]
            if len(shots) != shot_count:
                raise ValueError("Wrong shot count")
            seen: set[str] = set()
            for shot in shots:
                if shot.shot_id in seen or (
                    shot.continuation_of is not None
                    and shot.continuation_of not in seen
                ):
                    raise ValueError("Invalid continuity")
                if not shot.dialogue and len(
                    motion.build_prompt(shot, continues=shot.continuation_of is not None)
                ) > 2000:
                    raise ValueError("Prompt exceeds renderer budget")
                seen.add(shot.shot_id)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            raise PlanError(
                "The suggested shots do not fit the generation limits. Try again or edit the plan manually."
            ) from exc
        return shots


def configured_planner(values: Mapping[str, str]) -> HttpMoviePlanner | None:
    endpoint = values.get("MAGIC_VIDEO_EDITOR_MOVIE_PLANNER_URL")
    audience = values.get("MAGIC_VIDEO_EDITOR_MOVIE_PLANNER_AUDIENCE")
    return HttpMoviePlanner(endpoint, audience) if endpoint and audience else None
