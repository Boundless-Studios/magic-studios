"""`local_studio` sign-in: the gateway header is the whole identity locally, and no
Auth0 tenant is needed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from magic_video_editor.local_studio import create_local_studio_app


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", raising=False)
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_AUTH_COOKIE_SECURE", "false")
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_ID", raising=False)
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_SECRET", raising=False)


def test_the_gateway_identity_still_answers_without_signing_in() -> None:
    with TestClient(create_local_studio_app()) as client:
        session = client.get("/studio/auth/session")

    assert session.status_code == 200
    assert session.json()["actor_id"] == "local-e2e-user"


def test_a_project_can_be_created_over_the_gateway_identity() -> None:
    with TestClient(create_local_studio_app()) as client:
        created = client.post(
            "/studio/projects",
            json={"name": "Ember"},
            headers={"Origin": "http://testserver"},
        )

    assert created.status_code == 200
    assert created.json()["manifest"]["owner_id"] == "local-e2e-user"


def test_the_login_route_is_unavailable_without_a_tenant() -> None:
    with TestClient(create_local_studio_app()) as client:
        response = client.get("/studio/auth/login", follow_redirects=False)

    assert response.status_code == 503
