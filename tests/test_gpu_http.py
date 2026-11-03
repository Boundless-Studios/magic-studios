import time
from dataclasses import replace

import httpx
import pytest

from magic_video_editor.contracts import GpuGenerationRequest, MotionReferencePayload
from magic_video_editor.plugins.gpu_http import (
    GPU_IDENTITY_TOKEN_TIMEOUT_SECONDS,
    GPU_OUTER_REQUEST_DEADLINE_SECONDS,
    GPU_REQUEST_BUDGET_SECONDS,
    HttpGpuProcessor,
    RetryableGpuError,
    TerminalGpuError,
    _BoundedGoogleRequest,
)


class _Client:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: dict[str, object] | None = None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.request = {"url": url, **kwargs}
        return self.response


def _request() -> GpuGenerationRequest:
    return GpuGenerationRequest(
        source_key="sources/pose.png",
        output_key="outputs/clip.mp4",
        prompt="advance with shield raised",
        width=864,
        height=480,
        duration_seconds=5,
        seed=42,
        idempotency_key="project-1:motion-1:attempt-1",
        motion_id="motion-1",
        loop=True,
        preset="high",
        steps=28,
        provider_parameters={"motion_strength": 0.75},
    )


def _success_payload() -> dict[str, object]:
    return {
        "output_key": "outputs/clip.mp4",
        "idempotency_key": "project-1:motion-1:attempt-1",
        "contract_version": "immutable-idempotency-v2",
        "request_digest": HttpGpuProcessor.request_digest(_request()),
        "publication_mode": "immutable_create_once",
    }


async def test_constructs_an_authenticated_bounded_request() -> None:
    client = _Client(httpx.Response(200, json=_success_payload()))
    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        connect_timeout_seconds=3,
        read_timeout_seconds=120,
        token_provider=lambda audience: f"token-for:{audience}",
        client=client,
    )

    result = await processor.generate(_request())

    assert result.output_key == "outputs/clip.mp4"
    assert client.request is not None
    assert client.request["url"] == "https://gpu.internal/generate"
    assert client.request["headers"] == {
        "Authorization": "Bearer token-for:https://gpu.internal",
        "Idempotency-Key": "project-1:motion-1:attempt-1",
        "X-GPU-Contract-Version": "immutable-idempotency-v2",
    }
    timeout = client.request["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3
    assert timeout.read == 120
    assert client.request["json"] == {
        "source_key": "sources/pose.png",
        "output_key": "outputs/clip.mp4",
        "prompt": "advance with shield raised",
        "width": 864,
        "height": 480,
        "duration_seconds": 5,
        "seed": 42,
        "idempotency_key": "project-1:motion-1:attempt-1",
        "motion_id": "motion-1",
        "loop": True,
        "preset": "high",
        "steps": 28,
        "provider_parameters": {"motion_strength": 0.75},
        "start_frame_key": None,
        "start_frame_sha256": None,
        "composition_revision_id": None,
        "references": (),
    }


def test_request_digest_changes_with_conditioning_fields() -> None:
    """T2: the canonical digest must bind the shared start frame and
    reference images, or a provider could silently ignore conditioning
    without the mismatch being detectable."""
    base = _request()
    conditioned = replace(
        base,
        start_frame_key="outputs/studio/project-1/compositions/comp-1.png",
        start_frame_sha256="c" * 64,
        composition_revision_id="comp-1",
        references=(
            MotionReferencePayload(key="sources/ref-1.png", sha256="d" * 64, role="start"),
        ),
    )

    assert HttpGpuProcessor.request_digest(base) != HttpGpuProcessor.request_digest(conditioned)


async def test_http_contract_requires_provider_idempotency_capability_evidence() -> None:
    seen_requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal seen_requests
        seen_requests += 1
        assert request.headers["Idempotency-Key"] == _request().idempotency_key
        return httpx.Response(200, json=_success_payload())

    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        token_provider=lambda _audience: "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )

    first = await processor.generate(_request())
    replay = await processor.generate(_request())

    assert first == replay
    assert seen_requests == 2


def test_gpu_timeout_stays_below_outer_task_deadline() -> None:
    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(200, json=_success_payload())),
    )

    timeout = processor._timeout
    total_phase_budget = sum(
        value
        for value in (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if value is not None
    )
    assert GPU_IDENTITY_TOKEN_TIMEOUT_SECONDS + total_phase_budget <= (
        GPU_REQUEST_BUDGET_SECONDS
    )
    assert GPU_REQUEST_BUDGET_SECONDS <= GPU_OUTER_REQUEST_DEADLINE_SECONDS - 60
    assert timeout.read == 780

    with pytest.raises(ValueError, match="timeout"):
        HttpGpuProcessor(
            endpoint="https://gpu.internal/generate",
            audience="https://gpu.internal",
            read_timeout_seconds=841,
        )
    with pytest.raises(ValueError, match="timeout"):
        HttpGpuProcessor(
            endpoint="https://gpu.internal/generate",
            audience="https://gpu.internal",
            connect_timeout_seconds=100,
        )


async def test_identity_token_acquisition_has_an_explicit_short_timeout() -> None:
    def slow_token_provider(_audience: str) -> str:
        time.sleep(0.05)
        return "late-token"

    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        token_timeout_seconds=0.01,
        token_provider=slow_token_provider,
        client=_Client(httpx.Response(200, json=_success_payload())),
    )

    with pytest.raises(RetryableGpuError, match="identity token acquisition timed out"):
        await processor.generate(_request())


def test_google_auth_transport_cannot_fall_back_to_120_second_default() -> None:
    calls: list[float] = []

    def request(*_args: object, timeout: float, **_kwargs: object) -> object:
        calls.append(timeout)
        return object()

    bounded_request = _BoundedGoogleRequest(request, timeout_seconds=10)

    bounded_request("https://metadata.invalid")
    bounded_request("https://metadata.invalid", timeout=120)
    bounded_request("https://metadata.invalid", timeout=5)

    assert calls == [10, 10, 5]


async def test_adapter_has_one_end_to_end_wall_clock_budget() -> None:
    class _SlowClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            await __import__("asyncio").sleep(0.05)
            return httpx.Response(200, json=_success_payload())

    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        wall_clock_budget_seconds=0.01,
        token_timeout_seconds=0.005,
        token_provider=lambda _audience: "token",
        client=_SlowClient(),
    )

    with pytest.raises(RetryableGpuError, match="wall-clock budget"):
        await processor.generate(_request())


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_gpu_responses_are_typed(status: int) -> None:
    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(status, text="busy")),
    )

    with pytest.raises(RetryableGpuError, match=str(status)):
        await processor.generate(_request())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_success_payload(), "output_key": "outputs/other.mp4"},
        {**_success_payload(), "idempotency_key": "different-key"},
        {**_success_payload(), "contract_version": "legacy-v0"},
        {**_success_payload(), "request_digest": "different-request"},
        {**_success_payload(), "publication_mode": "overwrite"},
    ],
)
async def test_invalid_gpu_output_is_terminal(payload: dict[str, str]) -> None:
    processor = HttpGpuProcessor(
        endpoint="https://gpu.internal/generate",
        audience="https://gpu.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(200, json=payload)),
    )

    with pytest.raises(
        TerminalGpuError,
        match="output|idempotency|contract|request|create-once",
    ):
        await processor.generate(_request())
