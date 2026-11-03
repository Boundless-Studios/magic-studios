"""HTTP client for the Magic Video Studio API.

Owns the two things every caller of that API otherwise has to reimplement: reading
the project's current revision before a mutation, and retrying once when the write
loses the optimistic-concurrency race (409).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "https://api.magicstudios.ai"
DEFAULT_TIMEOUT_SECONDS = 60.0
RECOMMENDED_PRESETS: tuple[str, ...] = (
    "idle",
    "walk",
    "attack",
    "guard",
    "cast",
    "hurt",
)
_LOOPING_PRESETS = frozenset({"idle", "walk", "run"})


class StudioApiError(RuntimeError):
    """A studio API call failed. Carries the status and the server's own detail."""

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"studio API returned {status_code}: {detail}")


class StudioClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        headers = {"Accept": "application/json", "X-Studio-Setup-Version": "1"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    @classmethod
    def from_env(
        cls, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> StudioClient:
        return cls(
            base_url=os.environ.get("MAGIC_STUDIOS_URL", DEFAULT_BASE_URL),
            api_key=os.environ.get("MAGIC_STUDIOS_API_KEY"),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, path: str, *, json: Any | None = None
    ) -> Any:
        response = await self._http.request(method, path, json=json)
        return _unwrap(response)

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def current_revision(self, project_id: str) -> int:
        project = await self.get(f"/studio/projects/{project_id}")
        return int(project["revision"])

    async def mutate(
        self,
        method: str,
        path: str,
        *,
        project_id: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """A mutation that supplies `expected_revision` itself and retries once on 409.

        One retry is the right number: a second stale read means something else is
        writing continuously, and a caller that keeps retrying would mask that.
        """
        payload = dict(body or {})
        payload["expected_revision"] = await self.current_revision(project_id)
        try:
            return await self.request(method, path, json=payload)
        except StudioApiError as error:
            if error.status_code != 409:
                raise
            refreshed = _revision_from_conflict(error.detail)
            if refreshed is None:
                refreshed = await self.current_revision(project_id)
            payload["expected_revision"] = refreshed
            return await self.request(method, path, json=payload)

    async def movie_revision(self, movie_id: str) -> int:
        movie = await self.get(f"/studio/movies/{quote(movie_id, safe='')}")
        return int(movie["revision"])

    async def mutate_movie(
        self, method: str, path: str, *, movie_id: str, body: dict[str, Any] | None = None
    ) -> Any:
        """A movie mutation with `expected_revision`, retried once on a revision conflict.

        Only a 409 that reports `current_revision` is a stale read. Any other 409 (an
        active take, a busy shot) is a real refusal and is raised unchanged.
        """
        return await self._mutate(method, path, await self.movie_revision(movie_id), body)

    async def audio_revision(self, set_id: str) -> int:
        audio_set = await self.get(f"/studio/audio/{quote(set_id, safe='')}")
        return int(audio_set["revision"])

    async def mutate_audio(
        self, method: str, path: str, *, set_id: str, body: dict[str, Any] | None = None
    ) -> Any:
        """An audio set mutation, with the same single retry on a stale revision."""
        return await self._mutate(method, path, await self.audio_revision(set_id), body)

    async def _mutate(
        self, method: str, path: str, revision: int, body: dict[str, Any] | None
    ) -> Any:
        payload = dict(body or {})
        payload["expected_revision"] = revision
        try:
            return await self.request(method, path, json=payload)
        except StudioApiError as error:
            refreshed = (
                _revision_from_conflict(error.detail) if error.status_code == 409 else None
            )
            if refreshed is None:
                raise
            payload["expected_revision"] = refreshed
            return await self.request(method, path, json=payload)

    async def put_file(
        self, upload_url: str, data: bytes, content_type: str, headers: dict[str, str] | None = None
    ) -> None:
        """Upload bytes to a signed URL. Absolute, so it bypasses the client base URL.
        `headers` are the grant's `upload_headers` (a signed size ceiling), sent unchanged."""
        # A signed storage URL authenticates itself; never forward the Studio API key.
        response = await self._http.send(httpx.Request(
            "PUT", self._http.base_url.join(upload_url), content=data,
            headers={**(headers or {}), "Content-Type": content_type},
        ))
        if response.status_code >= 400:
            raise StudioApiError(response.status_code, response.text[:500])

    async def download(self, url: str) -> bytes:
        """GET a file. Studio file links are API paths authorized per request, so
        the API key is sent to the Studio API itself and to no other host."""
        target = self._http.base_url.join(url)
        base = self._http.base_url
        if (target.scheme, target.host, target.port) == (base.scheme, base.host, base.port):
            response = await self._http.get(target)
        else:
            response = await self._http.send(httpx.Request("GET", target))
        if response.status_code >= 400:
            raise StudioApiError(response.status_code, response.text[:500])
        return response.content


def _unwrap(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise StudioApiError(response.status_code, _detail(response))
    if not response.content:
        return None
    return response.json()


def _detail(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    return body.get("detail", body) if isinstance(body, dict) else body


def _revision_from_conflict(detail: Any) -> int | None:
    """The current revision a 409 body reports, when it reports one."""
    if isinstance(detail, dict) and isinstance(detail.get("current_revision"), int):
        return int(detail["current_revision"])
    return None


def recommended_motions() -> list[dict[str, Any]]:
    """The six-preset recommended set, in the studio's own order."""
    return [
        {
            "motion_id": preset,
            "name": preset.capitalize(),
            "kind": "preset",
            "preset": preset,
            "semantic_slot": preset,
            "loop_policy": "loop" if preset in _LOOPING_PRESETS else "once",
        }
        for preset in RECOMMENDED_PRESETS
    ]
