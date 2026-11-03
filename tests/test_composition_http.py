import time

import httpx
import pytest

from magic_video_editor.contracts import CompositionGenerationRequest
from magic_video_editor.plugins.composition_http import (
    COMPOSITION_IDENTITY_TOKEN_TIMEOUT_SECONDS,
    COMPOSITION_OUTER_REQUEST_DEADLINE_SECONDS,
    COMPOSITION_REQUEST_BUDGET_SECONDS,
    HttpCompositionGenerator,
    RetryableCompositionProviderError,
    TerminalCompositionProviderError,
)


class _Client:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: dict[str, object] | None = None

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.request = {"url": url, **kwargs}
        return self.response


def _request() -> CompositionGenerationRequest:
    return CompositionGenerationRequest(
        subject_key="outputs/studio/p/subjects/op.png",
        reference_keys=("sources/studio/p/composition-references/a.jpg",),
        output_key="outputs/studio/p/compositions/oc.png",
        prompt="hero at the gate, torch-lit",
        model="nanobanana-2-lite",
        idempotency_key="p:composition:op",
    )


def _success_payload() -> dict[str, object]:
    return {
        "output_key": "outputs/studio/p/compositions/oc.png",
        "idempotency_key": "p:composition:op",
        "contract_version": "composition-immutable-v1",
        "request_digest": HttpCompositionGenerator.request_digest(_request()),
        "publication_mode": "immutable_create_once",
    }


async def test_constructs_an_authenticated_bounded_request() -> None:
    client = _Client(httpx.Response(200, json=_success_payload()))
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        connect_timeout_seconds=3,
        read_timeout_seconds=100,
        token_provider=lambda audience: f"token-for:{audience}",
        client=client,
    )

    output_key = await provider.compose_frame(_request())

    assert output_key == "outputs/studio/p/compositions/oc.png"
    assert client.request is not None
    assert client.request["url"] == "https://composition.internal/generate"
    assert client.request["headers"] == {
        "Authorization": "Bearer token-for:https://composition.internal",
        "Idempotency-Key": "p:composition:op",
        "X-Composition-Contract-Version": "composition-immutable-v1",
    }
    timeout = client.request["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3
    assert timeout.read == 100
    assert client.request["json"] == {
        "subject_key": "outputs/studio/p/subjects/op.png",
        "reference_keys": ("sources/studio/p/composition-references/a.jpg",),
        "output_key": "outputs/studio/p/compositions/oc.png",
        "prompt": "hero at the gate, torch-lit",
        "model": "nanobanana-2-lite",
        "idempotency_key": "p:composition:op",
    }


def test_composition_timeout_stays_below_outer_task_deadline() -> None:
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(200, json=_success_payload())),
    )

    timeout = provider._timeout
    total_phase_budget = sum(
        value
        for value in (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if value is not None
    )
    assert COMPOSITION_IDENTITY_TOKEN_TIMEOUT_SECONDS + total_phase_budget <= (
        COMPOSITION_REQUEST_BUDGET_SECONDS
    )
    assert COMPOSITION_REQUEST_BUDGET_SECONDS <= (
        COMPOSITION_OUTER_REQUEST_DEADLINE_SECONDS - 60
    )

    with pytest.raises(ValueError, match="timeout"):
        HttpCompositionGenerator(
            endpoint="https://composition.internal/generate",
            audience="https://composition.internal",
            read_timeout_seconds=241,
        )
    with pytest.raises(ValueError, match="timeout"):
        HttpCompositionGenerator(
            endpoint="https://composition.internal/generate",
            audience="https://composition.internal",
            connect_timeout_seconds=100,
        )


async def test_identity_token_acquisition_has_an_explicit_short_timeout() -> None:
    def slow_token_provider(_audience: str) -> str:
        time.sleep(0.05)
        return "late-token"

    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_timeout_seconds=0.01,
        token_provider=slow_token_provider,
        client=_Client(httpx.Response(200, json=_success_payload())),
    )

    with pytest.raises(
        RetryableCompositionProviderError, match="identity token acquisition timed out"
    ):
        await provider.compose_frame(_request())


async def test_adapter_has_one_end_to_end_wall_clock_budget() -> None:
    class _SlowClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            import asyncio

            await asyncio.sleep(0.05)
            return httpx.Response(200, json=_success_payload())

    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        wall_clock_budget_seconds=0.01,
        token_timeout_seconds=0.005,
        token_provider=lambda _audience: "token",
        client=_SlowClient(),
    )

    with pytest.raises(RetryableCompositionProviderError, match="wall-clock budget"):
        await provider.compose_frame(_request())


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_composition_responses_are_typed(status: int) -> None:
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(status, text="busy")),
    )

    with pytest.raises(RetryableCompositionProviderError, match=str(status)):
        await provider.compose_frame(_request())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_terminal_composition_responses_are_typed(status: int) -> None:
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(status, text="bad request")),
    )

    with pytest.raises(TerminalCompositionProviderError, match=str(status)):
        await provider.compose_frame(_request())


async def test_composition_error_body_preview_is_bounded() -> None:
    huge_body = "x" * 10_000
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(500, text=huge_body)),
    )

    with pytest.raises(RetryableCompositionProviderError) as excinfo:
        await provider.compose_frame(_request())

    assert len(str(excinfo.value)) < 500


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_success_payload(), "output_key": "outputs/studio/p/compositions/other.png"},
        {**_success_payload(), "idempotency_key": "different-key"},
        {**_success_payload(), "contract_version": "legacy-v0"},
        {**_success_payload(), "request_digest": "different-request"},
        {**_success_payload(), "publication_mode": "overwrite"},
    ],
)
async def test_invalid_composition_output_is_terminal(payload: dict[str, str]) -> None:
    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=_Client(httpx.Response(200, json=payload)),
    )

    with pytest.raises(
        TerminalCompositionProviderError,
        match="output|idempotency|contract|request|create-once|incomplete",
    ):
        await provider.compose_frame(_request())


async def test_http_contract_requires_provider_idempotency_capability_evidence() -> None:
    seen_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_requests
        seen_requests += 1
        assert request.headers["Idempotency-Key"] == _request().idempotency_key
        return httpx.Response(200, json=_success_payload())

    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _audience: "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    first = await provider.compose_frame(_request())
    replay = await provider.compose_frame(_request())

    assert first == replay
    assert seen_requests == 2


async def test_composition_provider_requires_create_once_capability_attestation() -> None:
    request = _request()
    payload = {**_success_payload(), "publication_mode": "overwrite"}

    provider = HttpCompositionGenerator(
        endpoint="https://composition.internal/generate",
        audience="https://composition.internal",
        token_provider=lambda _: "token",
        client=_Client(httpx.Response(200, json=payload)),
    )
    with pytest.raises(TerminalCompositionProviderError, match="create-once"):
        await provider.compose_frame(request)


def test_endpoint_and_audience_are_required() -> None:
    with pytest.raises(ValueError, match="endpoint and audience"):
        HttpCompositionGenerator(endpoint="", audience="aud")
    with pytest.raises(ValueError, match="endpoint and audience"):
        HttpCompositionGenerator(endpoint="https://composition.internal", audience="")
