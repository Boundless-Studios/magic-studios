"""Higgsfield's hosted generation API, called with the owner's own key.

Contract (Higgsfield's Apache-2.0 `higgsfield-client`, checked 2026-10-18):
`POST {base}/{model path}` with a JSON body queues a request and answers
`request_id`; `GET {base}/requests/{id}/status` reports `queued`,
`in_progress`, `completed`, `failed`, `nsfw` or `canceled`, with
`video.url` once completed; `POST {base}/requests/{id}/cancel` cancels a
request that has not started; `POST {base}/files/generate-upload-url` returns a
pre-signed `upload_url` and the `public_url` the file is then served from. The
`Authorization` header is `Key <key>`, where the key is used exactly as copied.

Model paths and their argument names follow Higgsfield's public catalog. None
of these models has been benchmarked in Magic Studios; their outputs are
recorded as provider-reported, not worker-attested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from magic_video_editor.studio.models import MotionGenerationSettings

HIGGSFIELD_BASE_URL = "https://api.higgsfield.ai"
EXECUTION_PROVIDER_KEY = "execution_provider"
MODEL_KEY = "higgsfield_model"
TERMINAL_STATUSES = frozenset({"completed", "failed", "nsfw", "canceled"})
# Submission runs inside the generate request, so its calls stay short: a whole
# batch is submitted concurrently and must finish well inside the API timeout.
_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_UPLOAD_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
# Machine-binding parameters belong to the GPU path; a Higgsfield request that
# carried one would claim a recipe it never ran.
_MACHINE_PARAMETERS = frozenset(
    {"worker_id", "recipe_version", "configuration_version", "execution_profile"}
)
# Everything a Higgsfield request may carry. Anything else (a pasted `api_key`,
# say) is refused rather than stored in the job and echoed back in responses.
_HOSTED_PARAMETERS = frozenset({EXECUTION_PROVIDER_KEY, MODEL_KEY, "closed_loop", "framing_fill"})


@dataclass(frozen=True)
class HiggsfieldModel:
    model_id: str
    label: str
    path: str
    end_frame_argument: str | None
    min_duration: int
    max_duration: int
    fixed_arguments: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "label": self.label,
            "path": self.path,
            "closed_loop": self.end_frame_argument is not None,
            "duration_seconds": {"min": self.min_duration, "max": self.max_duration},
            "evidence": "Higgsfield public API; not benchmarked in Magic Studios",
        }


MODELS: dict[str, HiggsfieldModel] = {
    model.model_id: model
    for model in (
        HiggsfieldModel(
            "kling-3-std", "Kling 3.0 Standard", "kling-video/v3.0/std/image-to-video",
            "last_image_url", 3, 15,
            {"sound": "off", "cfg_scale": 0.5, "multi_shots": False},
        ),
        HiggsfieldModel(
            "kling-3-pro", "Kling 3.0 Pro", "kling-video/v3.0/pro/image-to-video",
            "last_image_url", 3, 15,
            {"sound": "off", "cfg_scale": 0.5, "multi_shots": False},
        ),
        HiggsfieldModel(
            "seedance-2", "Seedance 2.0", "bytedance/seedance-2.0/image-to-video",
            "end_image_url", 4, 15,
            {"generate_audio": False, "resolution": "720p"},
        ),
    )
}
DEFAULT_MODEL = "kling-3-std"


def is_higgsfield(parameters: dict[str, Any]) -> bool:
    return parameters.get(EXECUTION_PROVIDER_KEY) == "higgsfield"


def default_settings(model_id: str = DEFAULT_MODEL) -> dict[str, Any]:
    """The settings discovery hands out; pass them unchanged to generate."""
    return {
        "preset": "standard", "backdrop": "chroma_green", "width": 512, "height": 512,
        "duration_seconds": 4, "seed": 42, "steps": 20,
        "provider_parameters": {EXECUTION_PROVIDER_KEY: "higgsfield", MODEL_KEY: model_id},
    }


def resolve_settings(settings: MotionGenerationSettings) -> tuple[MotionGenerationSettings, HiggsfieldModel]:
    """Validate a Higgsfield request before any job exists. Never substitutes."""
    parameters = settings.provider_parameters
    bound = sorted(_MACHINE_PARAMETERS & parameters.keys())
    if bound:
        raise ValueError(f"Higgsfield generation cannot bind machine parameters: {', '.join(bound)}")
    undeclared = sorted(parameters.keys() - _HOSTED_PARAMETERS)
    if undeclared:
        raise ValueError(f"Higgsfield generation does not accept: {', '.join(undeclared)}")
    model_id = parameters.get(MODEL_KEY, DEFAULT_MODEL)
    model = MODELS.get(model_id) if isinstance(model_id, str) else None
    if model is None:
        raise ValueError(f"unknown Higgsfield model; choose one of: {', '.join(MODELS)}")
    if not model.min_duration <= settings.duration_seconds <= model.max_duration:
        raise ValueError(
            f"{model.label} accepts {model.min_duration}–{model.max_duration} seconds"
        )
    if parameters.get("closed_loop") is True and model.end_frame_argument is None:
        raise ValueError(f"{model.label} cannot condition on a last frame")
    resolved = settings.model_copy(
        update={"provider_parameters": {**parameters, MODEL_KEY: model.model_id}}
    )
    return resolved, model


def build_arguments(
    model: HiggsfieldModel, *, prompt: str, image_url: str, duration_seconds: int,
    end_image_url: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "prompt": prompt, "image_url": image_url, "duration": duration_seconds,
        **model.fixed_arguments,
    }
    if end_image_url is not None:
        if model.end_frame_argument is None:
            raise ValueError(f"{model.label} cannot condition on a last frame")
        arguments[model.end_frame_argument] = end_image_url
    return arguments


class HiggsfieldError(RuntimeError):
    """A failed call. The message never contains the key or a signed URL."""

    def __init__(self, message: str, *, status_code: int | None = None, permanent: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        # True when retrying the same call cannot succeed (bad URL, 4xx, too large, empty).
        self.permanent = permanent


@dataclass(frozen=True)
class HiggsfieldStatus:
    status: str
    video_url: str | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class HiggsfieldClient:
    def __init__(
        self, api_key: str, *, base_url: str = HIGGSFIELD_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Key {api_key}"}
        self._transport = transport

    def _client(self, timeout: httpx.Timeout = _REQUEST_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=timeout)

    async def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.request(
                    method, f"{self._base_url}/{path.lstrip('/')}", headers=self._headers, json=body
                )
        except httpx.HTTPError as exc:
            raise HiggsfieldError(f"Higgsfield could not be reached ({type(exc).__name__})") from exc
        if response.status_code in {401, 403}:
            raise HiggsfieldError("Higgsfield rejected the saved API key", status_code=response.status_code)
        if response.status_code == 402:
            raise HiggsfieldError("Higgsfield reports insufficient balance on this key", status_code=402)
        if not response.is_success:
            raise HiggsfieldError(
                f"Higgsfield answered {response.status_code}{_detail(response)}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HiggsfieldError("Higgsfield answered with invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HiggsfieldError("Higgsfield answered with an unexpected shape")
        return payload

    async def upload(self, content: bytes, content_type: str) -> str:
        grant = await self._json("POST", "files/generate-upload-url", {"content_type": content_type})
        upload_url, public_url = grant.get("upload_url"), grant.get("public_url")
        if not isinstance(upload_url, str) or not isinstance(public_url, str):
            raise HiggsfieldError("Higgsfield did not return an upload grant")
        # The owner's artwork only ever leaves over HTTPS.
        if not upload_url.startswith("https://") or not public_url.startswith("https://"):
            raise HiggsfieldError("Higgsfield returned a non-HTTPS upload grant", permanent=True)
        headers = grant.get("upload_headers")
        headers = headers if isinstance(headers, dict) else {"Content-Type": content_type}
        try:
            async with self._client(_UPLOAD_TIMEOUT) as client:
                response = await client.put(upload_url, content=content, headers=headers)
        except httpx.HTTPError as exc:
            raise HiggsfieldError(f"the opening frame upload failed ({type(exc).__name__})") from exc
        if not response.is_success:
            raise HiggsfieldError(f"the opening frame upload failed ({response.status_code})")
        return public_url

    async def submit(self, path: str, arguments: dict[str, Any]) -> str:
        queued = await self._json("POST", path, arguments)
        request_id = queued.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise HiggsfieldError("Higgsfield did not return a request ID")
        return request_id

    async def status(self, request_id: str) -> HiggsfieldStatus:
        payload = await self._json("GET", f"requests/{request_id}/status")
        status = payload.get("status")
        if not isinstance(status, str):
            raise HiggsfieldError("Higgsfield returned a status without a state")
        video = payload.get("video")
        url = video.get("url") if isinstance(video, dict) else None
        error = payload.get("error")
        return HiggsfieldStatus(
            status=status,
            video_url=url if isinstance(url, str) else None,
            error=str(error)[:500] if error is not None else None,
        )

    async def cancel(self, request_id: str) -> bool:
        try:
            await self._json("POST", f"requests/{request_id}/cancel")
        except HiggsfieldError:
            return False
        return True

    async def download(self, url: str, max_bytes: int) -> bytes:
        if not url.startswith("https://"):
            raise HiggsfieldError("Higgsfield returned a non-HTTPS result URL", permanent=True)
        chunks: list[bytes] = []
        size = 0
        try:
            # CDNs redirect; follow them, but only to HTTPS, and accept nothing
            # but a 200 so a redirect or error body is never stored as the video.
            async with (
                httpx.AsyncClient(transport=self._transport, timeout=_DOWNLOAD_TIMEOUT,
                                  follow_redirects=True, max_redirects=5) as client,
                client.stream("GET", url) as response,
            ):
                if response.url.scheme != "https" or response.status_code != 200:
                    transient = response.status_code == 429 or response.status_code >= 500
                    raise HiggsfieldError(f"the result download failed ({response.status_code})",
                                          status_code=response.status_code,
                                          permanent=response.url.scheme != "https" or not transient)
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HiggsfieldError("the Higgsfield result exceeds the motion size limit", permanent=True)
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise HiggsfieldError(f"the result download failed ({type(exc).__name__})") from exc
        if not chunks:
            raise HiggsfieldError("the Higgsfield result was empty", permanent=True)
        return b"".join(chunks)


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return ""
    return f": {str(detail)[:200]}" if isinstance(detail, str) and detail else ""
