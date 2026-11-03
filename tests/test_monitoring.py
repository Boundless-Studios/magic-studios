"""Unit tests for Sentry configuration, scrubbing, and route/job capture.

Mirrors a sibling service's Sentry-init test pattern: config/
no-op assertions patch ``sentry_sdk.init`` rather than performing a real
network-touching initialization, so these tests never depend on process-wide
Sentry state bleeding across test order.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch
from uuid import uuid4

import pytest
import sentry_sdk
from fastapi.testclient import TestClient

from magic_video_editor import monitoring as monitoring_module
from magic_video_editor.contracts import JobOperation, JobRecord, JobStatus, VideoJob
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "SENTRY_RELEASE",
        "MAGIC_VIDEO_EDITOR_RELEASE",
        "SENTRY_TRACES_SAMPLE_RATE",
        "SENTRY_PROFILES_SAMPLE_RATE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch


def test_config_defaults(clean_env: pytest.MonkeyPatch) -> None:
    config = monitoring_module.SentryConfig.from_env()

    assert config.dsn == ""
    assert config.environment == "development"
    assert config.release is None
    assert config.traces_sample_rate == monitoring_module.DEFAULT_TRACES_SAMPLE_RATE
    assert config.profiles_sample_rate == monitoring_module.DEFAULT_PROFILES_SAMPLE_RATE


def test_config_reads_env_and_release_fallback(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SENTRY_DSN", "https://abc@example.ingest.sentry.io/123")
    clean_env.setenv("SENTRY_ENVIRONMENT", "production")
    clean_env.setenv("MAGIC_VIDEO_EDITOR_RELEASE", "service@sha256:deadbeef")
    clean_env.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.2")
    clean_env.setenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")

    config = monitoring_module.SentryConfig.from_env()

    assert config.dsn == "https://abc@example.ingest.sentry.io/123"
    assert config.environment == "production"
    assert config.release == "service@sha256:deadbeef"
    assert config.traces_sample_rate == 0.2
    assert config.profiles_sample_rate == 0.1


def test_config_explicit_release_overrides_fallback(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SENTRY_RELEASE", "explicit-release")
    clean_env.setenv("MAGIC_VIDEO_EDITOR_RELEASE", "fallback-release")

    config = monitoring_module.SentryConfig.from_env()

    assert config.release == "explicit-release"


def test_configure_sentry_is_noop_without_dsn(clean_env: pytest.MonkeyPatch) -> None:
    config = monitoring_module.SentryConfig(
        dsn="",
        environment="development",
        release=None,
        traces_sample_rate=0.05,
        profiles_sample_rate=0.0,
    )

    with patch.object(monitoring_module.sentry_sdk, "init") as mock_init:
        result = monitoring_module.configure_sentry(config, "api")

    assert result is False
    mock_init.assert_not_called()
    assert sentry_sdk.is_initialized() is False


def test_configure_sentry_initializes_with_dsn() -> None:
    config = monitoring_module.SentryConfig(
        dsn="https://abc@example.ingest.sentry.io/123",
        environment="production",
        release="service@1",
        traces_sample_rate=0.2,
        profiles_sample_rate=0.1,
    )

    with patch.object(monitoring_module.sentry_sdk, "init") as mock_init, \
         patch.object(monitoring_module.sentry_sdk, "set_tag") as mock_set_tag:
        result = monitoring_module.configure_sentry(config, "worker")

    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == config.dsn
    assert kwargs["environment"] == "production"
    assert kwargs["release"] == "service@1"
    assert kwargs["send_default_pii"] is False
    assert kwargs["traces_sample_rate"] == 0.2
    assert kwargs["profiles_sample_rate"] == 0.1
    assert kwargs["in_app_include"] == ["magic_video_editor"]
    assert kwargs["before_send"] is monitoring_module._before_send
    mock_set_tag.assert_called_once_with("service_role", "worker")


def test_before_send_scrubs_headers_and_token_fields() -> None:
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-Studio-Actor-Id": "actor-123",
                "X-Studio-Release-Id": "release-456",
                "Content-Type": "application/json",
            },
            "query_string": {"token": "leaked-query-token", "project_id": "p1"},
            "data": {"nested": {"token": "leaked-body-token"}, "keep": "me"},
        }
    }

    scrubbed = monitoring_module._before_send(event, {})

    assert scrubbed is not None
    headers = scrubbed["request"]["headers"]
    assert headers["Authorization"] == "[Filtered]"
    assert headers["X-Studio-Actor-Id"] == "[Filtered]"
    assert headers["X-Studio-Release-Id"] == "[Filtered]"
    assert headers["Content-Type"] == "application/json"
    assert scrubbed["request"]["query_string"]["token"] == "[Filtered]"
    assert scrubbed["request"]["query_string"]["project_id"] == "p1"
    assert scrubbed["request"]["data"]["nested"]["token"] == "[Filtered]"
    assert scrubbed["request"]["data"]["keep"] == "me"


class _RecordingTransport(sentry_sdk.transport.Transport):
    """Test double capturing every envelope sent, in-process, no network."""

    def __init__(self) -> None:
        super().__init__({})
        self.envelopes: list[sentry_sdk.envelope.Envelope] = []

    def capture_envelope(self, envelope: sentry_sdk.envelope.Envelope) -> None:
        self.envelopes.append(envelope)


def _events_from(transport: _RecordingTransport) -> list[dict]:
    events = []
    for envelope in transport.envelopes:
        for item in envelope.items:
            if item.headers.get("type") == "event":
                events.append(item.payload.json)
    return events


@pytest.fixture
def sentry_test_client(monkeypatch: pytest.MonkeyPatch):
    """Real Sentry init (stub transport, no network) wrapping a fresh app."""
    monkeypatch.setenv("SENTRY_DSN", "https://abc@example.ingest.sentry.io/123")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test")
    saved_client = sentry_sdk.get_global_scope().client

    app_module = importlib.import_module("magic_video_editor.app")
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    app = app_module.create_app(runtime)

    client = sentry_sdk.get_client()
    transport = _RecordingTransport()
    client.transport = transport

    try:
        yield app, transport
    finally:
        sentry_sdk.get_global_scope().set_client(saved_client)


def test_unhandled_route_error_is_captured_with_service_role(
    sentry_test_client,
) -> None:
    app, transport = sentry_test_client

    async def _boom(self, job_id: str) -> JobRecord | None:  # noqa: ANN001
        raise RuntimeError("synthetic route failure")

    with patch.object(MemoryJobStore, "get", _boom):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/jobs/{uuid4()}", headers={"X-Studio-Actor-Id": "owner-1"})

    assert response.status_code == 500
    events = _events_from(transport)
    assert len(events) == 1
    assert events[0]["tags"]["service_role"] == "all"
    assert events[0]["exception"]["values"][0]["type"] == "RuntimeError"


def test_health_reports_observability_sentry_flag(clean_env: pytest.MonkeyPatch) -> None:
    from magic_video_editor import app as app_module

    with patch.object(monitoring_module.sentry_sdk, "init"):
        runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
        client = TestClient(app_module.create_app(runtime))

        assert client.get("/health").json()["observability"] == {"sentry": False}

    clean_env.setenv("SENTRY_DSN", "https://abc@example.ingest.sentry.io/123")
    with patch.object(monitoring_module.sentry_sdk, "init"), \
         patch.object(monitoring_module.sentry_sdk, "set_tag"), \
         patch.object(app_module, "is_sentry_enabled", return_value=True):
        runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
        client = TestClient(app_module.create_app(runtime))

        assert client.get("/health").json()["observability"] == {"sentry": True}


def test_job_failure_captured_with_job_tags() -> None:
    from magic_video_editor import app as app_module

    job = VideoJob(
        job_id="job-1",
        source_key="sources/a.mp4",
        output_key="outputs/a.mp4",
        claim_token="token-1",
        operation=JobOperation.PACKAGE,
        project_id="project-1",
        attempts=5,
        attempt_budget=5,
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    error = RuntimeError("boom")

    async def _run() -> None:
        with patch.object(app_module, "capture_job_failure") as mock_capture:
            await app_module._handle_processing_failure(runtime, job, None, None, error)
        mock_capture.assert_called_once_with(
            job_id="job-1",
            operation="package",
            project_id="project-1",
            retryable=False,
            error=error,
        )

    import asyncio

    asyncio.run(_run())


def test_terminal_failure_captured_with_message() -> None:
    from magic_video_editor import app as app_module

    job = VideoJob(
        job_id="job-2",
        source_key="sources/a.mp4",
        output_key="outputs/a.mp4",
        claim_token="token-2",
        operation=JobOperation.GENERATE_MOTION,
        project_id=None,
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())

    async def _run() -> None:
        with patch.object(app_module, "capture_job_failure") as mock_capture:
            await app_module._fail_terminal_operation(
                runtime,
                job,
                "unsupported version",
                app_module.JobFailureCode.UNSUPPORTED_OPERATION,
                "not supported",
            )
        mock_capture.assert_called_once_with(
            job_id="job-2",
            operation="generate_motion",
            project_id=None,
            retryable=False,
            message="unsupported version",
        )

    import asyncio

    asyncio.run(_run())
