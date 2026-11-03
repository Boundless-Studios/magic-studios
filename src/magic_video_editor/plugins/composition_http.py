"""Fail-closed HTTP adapter for immutable opening-frame composition (T15/T18)."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Protocol

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from magic_video_editor.contracts import CompositionGenerationRequest

COMPOSITION_CONTRACT_VERSION = "composition-immutable-v1"
COMPOSITION_OUTER_REQUEST_DEADLINE_SECONDS = 300
COMPOSITION_REQUEST_BUDGET_SECONDS = 240
COMPOSITION_IDENTITY_TOKEN_TIMEOUT_SECONDS = 10
COMPOSITION_WRITE_TIMEOUT_SECONDS = 30
COMPOSITION_POOL_TIMEOUT_SECONDS = 5
COMPOSITION_ERROR_BODY_PREVIEW_BYTES = 300


class RetryableCompositionProviderError(RuntimeError):
    """A transient composition response that is safe for the job queue to retry."""


class TerminalCompositionProviderError(RuntimeError):
    """A deterministic composition contract failure that must not be retried."""


class _Client(Protocol):
    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...


class HttpCompositionGenerator:
    def __init__(
        self,
        *,
        endpoint: str,
        audience: str,
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 180,
        token_timeout_seconds: float = COMPOSITION_IDENTITY_TOKEN_TIMEOUT_SECONDS,
        wall_clock_budget_seconds: float = COMPOSITION_REQUEST_BUDGET_SECONDS,
        token_provider: Callable[[str], str] | None = None,
        client: _Client | None = None,
    ) -> None:
        if not endpoint or not audience:
            raise ValueError("composition provider endpoint and audience are required")
        timeout_budget = (
            token_timeout_seconds
            + connect_timeout_seconds
            + read_timeout_seconds
            + COMPOSITION_WRITE_TIMEOUT_SECONDS
            + COMPOSITION_POOL_TIMEOUT_SECONDS
        )
        if (
            token_timeout_seconds <= 0
            or connect_timeout_seconds <= 0
            or read_timeout_seconds <= 0
            or timeout_budget > COMPOSITION_REQUEST_BUDGET_SECONDS
        ):
            raise ValueError(
                "composition timeout phases must be positive and total no more "
                f"than {COMPOSITION_REQUEST_BUDGET_SECONDS} seconds"
            )
        if not 0 < wall_clock_budget_seconds <= COMPOSITION_REQUEST_BUDGET_SECONDS:
            raise ValueError(
                "composition wall-clock budget must be positive and no greater "
                f"than {COMPOSITION_REQUEST_BUDGET_SECONDS} seconds"
            )
        self._endpoint = endpoint
        self._audience = audience
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=COMPOSITION_WRITE_TIMEOUT_SECONDS,
            pool=COMPOSITION_POOL_TIMEOUT_SECONDS,
        )
        self._token_provider = token_provider or (
            lambda target: id_token.fetch_id_token(Request(), target)
        )
        self._token_timeout_seconds = token_timeout_seconds
        self._wall_clock_budget_seconds = wall_clock_budget_seconds
        self._client = client or httpx.AsyncClient(trust_env=False)

    async def compose_frame(self, request: CompositionGenerationRequest) -> str:
        try:
            async with asyncio.timeout(self._wall_clock_budget_seconds):
                return await self._compose_within_budget(request)
        except TimeoutError as exc:
            raise RetryableCompositionProviderError(
                "composition adapter wall-clock budget exhausted"
            ) from exc

    async def _compose_within_budget(
        self, request: CompositionGenerationRequest
    ) -> str:
        try:
            async with asyncio.timeout(self._token_timeout_seconds):
                token = await asyncio.to_thread(self._token_provider, self._audience)
        except TimeoutError as exc:
            raise RetryableCompositionProviderError(
                "composition identity token acquisition timed out"
            ) from exc
        try:
            response = await self._client.post(
                self._endpoint,
                json=asdict(request),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": request.idempotency_key,
                    "X-Composition-Contract-Version": COMPOSITION_CONTRACT_VERSION,
                },
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableCompositionProviderError(
                "composition provider transport failed"
            ) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableCompositionProviderError(
                f"composition provider failed ({response.status_code}): "
                f"{response.text[:COMPOSITION_ERROR_BODY_PREVIEW_BYTES]}"
            )
        if response.status_code >= 400:
            raise TerminalCompositionProviderError(
                f"composition provider rejected request ({response.status_code}): "
                f"{response.text[:COMPOSITION_ERROR_BODY_PREVIEW_BYTES]}"
            )
        try:
            payload = response.json()
            output_key = payload["output_key"]
            response_idempotency_key = payload["idempotency_key"]
            request_digest = payload["request_digest"]
            contract_version = payload["contract_version"]
            publication_mode = payload["publication_mode"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TerminalCompositionProviderError(
                "composition provider response is incomplete"
            ) from exc
        if output_key != request.output_key:
            raise TerminalCompositionProviderError(
                "composition provider response output key does not match request"
            )
        if response_idempotency_key != request.idempotency_key:
            raise TerminalCompositionProviderError(
                "composition provider response idempotency evidence does not match request"
            )
        if request_digest != self.request_digest(request):
            raise TerminalCompositionProviderError(
                "composition provider response request evidence does not match request"
            )
        if contract_version != COMPOSITION_CONTRACT_VERSION:
            raise TerminalCompositionProviderError(
                "composition provider contract version is not supported"
            )
        if publication_mode != "immutable_create_once":
            raise TerminalCompositionProviderError(
                "composition provider did not attest create-once publication"
            )
        return output_key

    @staticmethod
    def request_digest(request: CompositionGenerationRequest) -> str:
        canonical_request = json.dumps(
            asdict(request),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical_request).hexdigest()
