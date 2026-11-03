import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from magic_video_editor.contracts import (
    GpuGenerationRequest,
    GpuGenerationResult,
    GpuOperationProgress,
    GpuTelemetry,
)


class RetryableGpuError(RuntimeError):
    """A transient GPU response that is safe for the job queue to retry."""


class TerminalGpuError(RuntimeError):
    """A deterministic GPU contract failure that must not be retried."""


class _HttpClient(Protocol):
    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...


GPU_CONTRACT_VERSION = "immutable-idempotency-v2"
GPU_STATUS_CONTRACT_VERSION = "gpu-status-v1"
GPU_OUTER_REQUEST_DEADLINE_SECONDS = 900
GPU_REQUEST_BUDGET_SECONDS = 840
GPU_IDENTITY_TOKEN_TIMEOUT_SECONDS = 10
GPU_WRITE_TIMEOUT_SECONDS = 30
GPU_POOL_TIMEOUT_SECONDS = 5


class _BoundedGoogleRequest:
    def __init__(
        self,
        request: Callable[..., object],
        *,
        timeout_seconds: float,
    ) -> None:
        self._request = request
        self._timeout_seconds = timeout_seconds

    def __call__(self, *args: object, **kwargs: object) -> object:
        requested_timeout = kwargs.get("timeout")
        kwargs["timeout"] = (
            min(requested_timeout, self._timeout_seconds)
            if isinstance(requested_timeout, (int, float))
            else self._timeout_seconds
        )
        return self._request(*args, **kwargs)


def _fetch_identity_token(
    audience: str,
    *,
    timeout_seconds: float = GPU_IDENTITY_TOKEN_TIMEOUT_SECONDS,
) -> str:
    request = _BoundedGoogleRequest(
        Request(),
        timeout_seconds=timeout_seconds,
    )
    return id_token.fetch_id_token(request, audience)


class HttpGpuProcessor:
    def __init__(
        self,
        *,
        endpoint: str,
        audience: str,
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 780,
        token_timeout_seconds: float = GPU_IDENTITY_TOKEN_TIMEOUT_SECONDS,
        wall_clock_budget_seconds: float = GPU_REQUEST_BUDGET_SECONDS,
        token_provider: Callable[[str], str] | None = None,
        client: _HttpClient | None = None,
    ) -> None:
        if not endpoint or not audience:
            raise ValueError("GPU endpoint and audience are required")
        timeout_budget = (
            token_timeout_seconds
            + connect_timeout_seconds
            + read_timeout_seconds
            + GPU_WRITE_TIMEOUT_SECONDS
            + GPU_POOL_TIMEOUT_SECONDS
        )
        if (
            token_timeout_seconds <= 0
            or connect_timeout_seconds <= 0
            or read_timeout_seconds <= 0
            or timeout_budget > GPU_REQUEST_BUDGET_SECONDS
        ):
            raise ValueError(
                "GPU timeout phases must be positive and total no more than "
                f"{GPU_REQUEST_BUDGET_SECONDS} seconds"
            )
        if not 0 < wall_clock_budget_seconds <= GPU_REQUEST_BUDGET_SECONDS:
            raise ValueError(
                "GPU wall-clock budget must be positive and no greater than "
                f"{GPU_REQUEST_BUDGET_SECONDS} seconds"
            )
        self._endpoint = endpoint
        self._audience = audience
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=GPU_WRITE_TIMEOUT_SECONDS,
            pool=GPU_POOL_TIMEOUT_SECONDS,
        )
        self._token_provider = token_provider or (
            lambda target_audience: _fetch_identity_token(
                target_audience,
                timeout_seconds=token_timeout_seconds,
            )
        )
        self._token_timeout_seconds = token_timeout_seconds
        self._wall_clock_budget_seconds = wall_clock_budget_seconds
        self._client = client or httpx.AsyncClient(trust_env=False)

    async def generate(
        self, request: GpuGenerationRequest
    ) -> GpuGenerationResult:
        try:
            async with asyncio.timeout(self._wall_clock_budget_seconds):
                return await self._generate_within_budget(request)
        except TimeoutError as exc:
            raise RetryableGpuError(
                "GPU adapter wall-clock budget exhausted"
            ) from exc

    async def status(self) -> GpuTelemetry | None:
        try:
            token = await asyncio.to_thread(self._token_provider, self._audience)
            response = await self._client.post(
                f"{self._endpoint.rstrip('/')}/status",
                json={"contract_version": GPU_STATUS_CONTRACT_VERSION},
                headers={"Authorization": f"Bearer {token}", "X-GPU-Status-Version": GPU_STATUS_CONTRACT_VERSION},
                timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5),
            )
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError):
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
            if payload["contract_version"] != GPU_STATUS_CONTRACT_VERSION or payload["attestation"] != "provider_observed":
                return None
            sampled_at = datetime.fromisoformat(str(payload["sampled_at"]).replace("Z", "+00:00"))
            if sampled_at.tzinfo is None or sampled_at > datetime.now(UTC) + timedelta(minutes=2):
                return None
            values = {name: payload.get(name) for name in ("vram_used_mb", "vram_total_mb", "vram_reserved_mb", "utilization_percent")}
            if any(value is not None and (not isinstance(value, int) or value < 0) for value in values.values()):
                return None
            if values["utilization_percent"] is not None and values["utilization_percent"] > 100:
                return None
            total = values["vram_total_mb"]
            if total is not None and (
                (values["vram_used_mb"] is not None and values["vram_used_mb"] > total)
                or (values["vram_reserved_mb"] is not None and values["vram_reserved_mb"] > total)
            ):
                return None
            raw_progress = payload.get("operation_progress", {})
            if not isinstance(raw_progress, dict) or len(raw_progress) > 100:
                return None
            progress: dict[str, GpuOperationProgress] = {}
            for identity, item in raw_progress.items():
                if not isinstance(identity, str) or len(identity) > 255 or not isinstance(item, dict):
                    return None
                percent = item.get("percent")
                stage = item.get("stage")
                if (percent is not None and (not isinstance(percent, int) or not 0 <= percent <= 100)) or not isinstance(stage, str):
                    return None
                progress[identity] = GpuOperationProgress(percent=percent, stage=stage[:100])
            return GpuTelemetry(
                provider=str(payload["provider"])[:100], worker_healthy=payload["worker_healthy"] is True,
                device_name=str(payload["device_name"])[:200] if payload.get("device_name") else None,
                vram_used_mb=values["vram_used_mb"], vram_total_mb=values["vram_total_mb"],
                vram_reserved_mb=values["vram_reserved_mb"], utilization_percent=values["utilization_percent"],
                active_operation_count=max(0, int(payload["active_operation_count"])),
                sampled_at=sampled_at, stale=False,
                operation_progress=progress,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    async def _generate_within_budget(
        self,
        request: GpuGenerationRequest,
    ) -> GpuGenerationResult:
        try:
            async with asyncio.timeout(self._token_timeout_seconds):
                token = await asyncio.to_thread(
                    self._token_provider,
                    self._audience,
                )
        except TimeoutError as exc:
            raise RetryableGpuError(
                "GPU identity token acquisition timed out"
            ) from exc
        try:
            response = await self._client.post(
                self._endpoint,
                json=asdict(request),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": request.idempotency_key,
                    "X-GPU-Contract-Version": GPU_CONTRACT_VERSION,
                },
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableGpuError("GPU request transport failed") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableGpuError(
                f"GPU request failed ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise TerminalGpuError(
                f"GPU request rejected ({response.status_code}): {response.text[:300]}"
            )
        try:
            payload = response.json()
            output_key = payload["output_key"]
            response_idempotency_key = payload["idempotency_key"]
            contract_version = payload["contract_version"]
            request_digest = payload["request_digest"]
            publication_mode = payload["publication_mode"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TerminalGpuError("GPU response has no valid output key") from exc
        if output_key != request.output_key:
            raise TerminalGpuError("GPU response output key does not match request")
        if response_idempotency_key != request.idempotency_key:
            raise TerminalGpuError("GPU response idempotency evidence does not match request")
        if contract_version != GPU_CONTRACT_VERSION:
            raise TerminalGpuError("GPU provider contract version is not supported")
        if request_digest != self.request_digest(request):
            raise TerminalGpuError("GPU response request evidence does not match request")
        if publication_mode != "immutable_create_once":
            raise TerminalGpuError("GPU provider did not attest create-once publication")
        return GpuGenerationResult(output_key=output_key)

    @staticmethod
    def request_digest(request: GpuGenerationRequest) -> str:
        canonical_request = json.dumps(
            asdict(request),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical_request).hexdigest()
