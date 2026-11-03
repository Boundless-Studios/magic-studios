"""Sentry error and performance monitoring for the service.

Mirrors the conventions of a sibling service's monitoring module (same
integration set, ``before_send`` scrubbing shape, tag-on-init pattern) but
keeps this repo's typed, frozen-dataclass style and a much smaller scrub
surface: this service does not carry that sibling's RabbitMQ/LLM
noise-filtering rules, only the header/field scrubbing this repo's own
request surface needs.

Configuration is env-only by design — this repo is public, so no DSN, token,
or org name may ever live in source. ``configure_sentry`` is a documented
no-op whenever ``SentryConfig.dsn`` is empty, which is the default for local
development and CI.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)

DEFAULT_TRACES_SAMPLE_RATE = 0.05
DEFAULT_PROFILES_SAMPLE_RATE = 0.0
DEFAULT_ENVIRONMENT = "development"

# Header names whose values must never reach Sentry verbatim.
_SCRUBBED_HEADER_NAMES = frozenset({
    "authorization",
    "x-studio-actor-id",
    "x-studio-release-id",
    "stripe-signature",
})

# Query/body field names whose values must never reach Sentry verbatim.
_SCRUBBED_FIELD_NAMES = frozenset({"token"})

_REDACTED = "[Filtered]"


@dataclass(frozen=True, slots=True)
class SentryConfig:
    """Env-derived Sentry configuration. Never construct with a literal DSN."""

    dsn: str
    environment: str
    release: str | None
    traces_sample_rate: float
    profiles_sample_rate: float

    @classmethod
    def from_env(cls) -> "SentryConfig":
        return cls(
            dsn=os.getenv("SENTRY_DSN", "").strip(),
            environment=os.getenv("SENTRY_ENVIRONMENT", "").strip() or DEFAULT_ENVIRONMENT,
            release=(
                os.getenv("SENTRY_RELEASE", "").strip()
                or os.getenv("MAGIC_VIDEO_EDITOR_RELEASE", "").strip()
                or None
            ),
            traces_sample_rate=_parse_rate(
                "SENTRY_TRACES_SAMPLE_RATE", DEFAULT_TRACES_SAMPLE_RATE
            ),
            profiles_sample_rate=_parse_rate(
                "SENTRY_PROFILES_SAMPLE_RATE", DEFAULT_PROFILES_SAMPLE_RATE
            ),
        )


def _parse_rate(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        rate = float(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r: expected a float between 0 and 1", name, raw_value)
        return default
    if not 0 <= rate <= 1:
        logger.warning("Ignoring invalid %s=%r: expected a float between 0 and 1", name, raw_value)
        return default
    return rate


def _scrub_headers(headers: dict[str, Any]) -> None:
    for key in list(headers):
        if key.lower() in _SCRUBBED_HEADER_NAMES:
            headers[key] = _REDACTED


def _scrub_mapping_fields(mapping: Any) -> None:
    if not isinstance(mapping, dict):
        return
    for key, value in list(mapping.items()):
        if isinstance(key, str) and key.lower() in _SCRUBBED_FIELD_NAMES:
            mapping[key] = _REDACTED
        elif isinstance(value, dict):
            _scrub_mapping_fields(value)
        elif isinstance(value, list):
            for item in value:
                _scrub_mapping_fields(item)


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub actor/auth headers and any ``token`` field before it leaves the process."""
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            _scrub_headers(headers)
        for key in ("query_string", "data"):
            _scrub_mapping_fields(request.get(key))

    return event


def _build_integrations() -> list[Any]:
    return [
        FastApiIntegration(),
        StarletteIntegration(),
        HttpxIntegration(),
        AsyncioIntegration(),
    ]


def configure_sentry(config: SentryConfig, service_role: str) -> bool:
    """Initialize Sentry exactly once when a DSN is configured.

    Documented no-op when ``config.dsn`` is empty — the default for local
    development, tests, and any environment that has not been given a DSN.
    Must be called before the FastAPI application is constructed so the
    FastAPI/Starlette ASGI integrations wrap it (BOU convention mirrored
    from ``gaia.infra.monitoring.sentry.init_sentry``).
    """
    if not config.dsn:
        logger.info("Sentry disabled: SENTRY_DSN is not set")
        return False

    sentry_sdk.init(
        dsn=config.dsn,
        environment=config.environment,
        release=config.release,
        integrations=_build_integrations(),
        send_default_pii=False,
        before_send=_before_send,
        traces_sample_rate=config.traces_sample_rate,
        profiles_sample_rate=config.profiles_sample_rate,
        in_app_include=["magic_video_editor"],
    )
    sentry_sdk.set_tag("service_role", service_role)
    logger.info(
        "Sentry initialized | environment=%s service_role=%s traces=%s profiles=%s",
        config.environment,
        service_role,
        config.traces_sample_rate,
        config.profiles_sample_rate,
    )
    return True


def is_sentry_enabled() -> bool:
    """Whether Sentry is currently initialized in this process."""
    return sentry_sdk.is_initialized()


def capture_job_failure(
    *,
    job_id: str,
    operation: str,
    project_id: str | None,
    retryable: bool,
    error: BaseException | None = None,
    message: str | None = None,
) -> None:
    """Capture a job execution failure with job-scoped tags.

    Additive only — callers keep their existing failure recording
    (``runtime.jobs.fail``/``release``/``complete``) exactly as before; this
    only mirrors the failure into Sentry when Sentry is configured.
    """
    if not is_sentry_enabled():
        return

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job_id", job_id)
        scope.set_tag("operation", operation)
        scope.set_tag("project_id", project_id or "")
        scope.set_tag("classification", "retryable" if retryable else "terminal")
        if error is not None:
            sentry_sdk.capture_exception(error)
        else:
            sentry_sdk.capture_message(message or "video job failed")
