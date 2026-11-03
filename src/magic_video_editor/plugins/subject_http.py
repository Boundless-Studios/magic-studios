"""Fail-closed HTTP adapter for immutable subject generation."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Protocol

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from magic_video_editor.contracts import SubjectGenerationRequest

SUBJECT_CONTRACT_VERSION = "subject-immutable-v1"


class RetryableSubjectProviderError(RuntimeError):
    pass


class TerminalSubjectProviderError(RuntimeError):
    pass


class _Client(Protocol):
    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...


class HttpSubjectGenerator:
    def __init__(
        self, *, endpoint: str, audience: str,
        token_provider: Callable[[str], str] | None = None,
        client: _Client | None = None,
    ) -> None:
        if not endpoint or not audience:
            raise ValueError("subject provider endpoint and audience are required")
        self._endpoint = endpoint; self._audience = audience
        self._token_provider = token_provider or (lambda target: id_token.fetch_id_token(Request(), target))
        self._client = client or httpx.AsyncClient(trust_env=False)

    async def generate_subject(self, request: SubjectGenerationRequest) -> str:
        try:
            token = await asyncio.to_thread(self._token_provider, self._audience)
            response = await self._client.post(
                self._endpoint, json=asdict(request),
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": request.idempotency_key, "X-Subject-Contract-Version": SUBJECT_CONTRACT_VERSION},
                timeout=httpx.Timeout(connect=5, read=780, write=30, pool=5),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableSubjectProviderError("subject provider transport failed") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableSubjectProviderError(f"subject provider failed ({response.status_code})")
        if response.status_code >= 400:
            raise TerminalSubjectProviderError(f"subject provider rejected request ({response.status_code})")
        try:
            payload = response.json()
            evidence = (
                payload["output_key"], payload["idempotency_key"], payload["request_digest"],
                payload["contract_version"], payload["publication_mode"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TerminalSubjectProviderError("subject provider response is incomplete") from exc
        digest = hashlib.sha256(json.dumps(asdict(request), separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        if evidence[0] != request.output_key or evidence[1] != request.idempotency_key or evidence[2] != digest or evidence[3] != SUBJECT_CONTRACT_VERSION:
            raise TerminalSubjectProviderError("subject provider capability evidence does not match request")
        if evidence[4] != "immutable_create_once":
            raise TerminalSubjectProviderError("subject provider did not attest create-once publication")
        return request.output_key
