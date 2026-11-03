"""Browser sign-in through Auth0: the redirect, the callback, sessions, and CSRF."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.api_keys import MemoryStudioApiKeyStore
from magic_video_editor.studio.auth import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    Auth0Client,
    Auth0Identity,
    Auth0Settings,
    EmailAllowlist,
    MemoryStudioAuthStore,
    SignInMethod,
)
from magic_video_editor.studio.auth_service import StudioAuthService
from magic_video_editor.studio.limits import (
    DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
    DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.routes import create_studio_router
from magic_video_editor.studio.service import StudioService

ORIGIN = "https://testserver"
AUTH0_DOMAIN = "studio.example.auth0.com"
CLIENT_ID = "studio-client-id"
ALLOWLIST = EmailAllowlist(emails=frozenset(), domains=frozenset({"example.com"}))
CLOSED_ALLOWLIST = EmailAllowlist(emails=frozenset(), domains=frozenset())
GOOGLE_IDENTITY = Auth0Identity(
    subject="google-oauth2|112233",
    email="Painter@Example.com",
    email_verified=True,
    display_name="Painter",
)


class FakeAuth0Provider:
    """Stands in for the token exchange and JWKS verification so no test reaches
    the tenant."""

    def __init__(self, identity: Auth0Identity) -> None:
        self.identity = identity
        self.settings = Auth0Settings(
            domain=AUTH0_DOMAIN, client_id=CLIENT_ID, client_secret="not-a-secret"
        )
        self.exchanges: list[tuple[str, str, str]] = []

    def authorization_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        signup: bool = False,
    ) -> str:
        # The redirect shape is the real client's, so the route test asserts what
        # the tenant would actually receive.
        return Auth0Client(self.settings).authorization_url(
            state=state,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            signup=signup,
        )

    def logout_url(self, return_to: str) -> str:
        return self.settings.logout_url(return_to)

    async def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> Auth0Identity:
        self.exchanges.append((code, code_verifier, redirect_uri))
        return self.identity


@pytest.fixture(autouse=True)
def _session_actor_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", "session")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_PUBLIC_BASE_URL", ORIGIN)
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_IAP_AUDIENCE", raising=False)


def _service() -> StudioService:
    return StudioService(
        store=MemoryStudioStore(),
        assets=MemoryAssetStore(),
        jobs=MemoryJobStore(),
        dispatcher=None,
    )


def _auth_service(
    *,
    allowlist: EmailAllowlist = ALLOWLIST,
    identity: Auth0Identity = GOOGLE_IDENTITY,
    auth0: FakeAuth0Provider | None = None,
    projects: StudioService | None = None,
    store: MemoryStudioAuthStore | None = None,
) -> StudioAuthService:
    return StudioAuthService(
        store if store is not None else MemoryStudioAuthStore(),
        allowlist=allowlist,
        auth0=auth0 if auth0 is not None else FakeAuth0Provider(identity),
        projects=projects,
    )


def _client(auth: StudioAuthService, service: StudioService | None = None) -> TestClient:
    studio = service if service is not None else _service()
    api_keys = MemoryStudioApiKeyStore()
    application = FastAPI()
    application.state.studio_api_key_store = api_keys
    application.state.studio_auth = auth
    application.include_router(
        create_studio_router(studio, api_keys=api_keys, auth=auth)
    )
    return TestClient(application, base_url=ORIGIN)


def _sign_in(client: TestClient, *, return_to: str | None = None) -> None:
    started = client.get(
        "/studio/auth/login",
        params={} if return_to is None else {"return_to": return_to},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


def test_the_login_route_redirects_to_the_tenant_with_state_and_pkce() -> None:
    client = _client(_auth_service())

    started = client.get(
        "/studio/auth/login",
        params={"return_to": "/projects"},
        follow_redirects=False,
    )

    assert started.status_code == 302
    location = urlparse(started.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == AUTH0_DOMAIN
    assert location.path == "/authorize"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ORIGIN}/studio/auth/callback"]
    assert query["scope"] == ["openid profile email"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["select_account"]
    assert query["state"][0]
    assert query["code_challenge"][0]
    assert OAUTH_STATE_COOKIE_NAME in client.cookies


def test_the_signup_route_opens_the_tenant_sign_up_form() -> None:
    client = _client(_auth_service())

    started = client.get(
        "/studio/auth/login",
        params={"return_to": "/projects", "signup": "true"},
        follow_redirects=False,
    )

    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["location"]).query)
    assert query["screen_hint"] == ["signup"]
    assert "prompt" not in query
    assert query["code_challenge_method"] == ["S256"]
    assert OAUTH_STATE_COOKIE_NAME in client.cookies


def test_signing_in_does_not_ask_for_the_sign_up_form() -> None:
    client = _client(_auth_service())

    started = client.get("/studio/auth/login", follow_redirects=False)

    assert "screen_hint" not in parse_qs(urlparse(started.headers["location"]).query)


def test_an_off_origin_return_path_is_discarded_and_lands_on_the_root() -> None:
    client = _client(_auth_service())
    started = client.get(
        "/studio/auth/login",
        params={"return_to": "https://evil.invalid/steal"},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    finished = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert finished.headers["location"] == "/"


def test_the_callback_creates_a_session_and_the_session_route_describes_it() -> None:
    provider = FakeAuth0Provider(GOOGLE_IDENTITY)
    client = _client(_auth_service(auth0=provider))

    started = client.get(
        "/studio/auth/login", params={"return_to": "/projects"}, follow_redirects=False
    )
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    finished = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert finished.status_code == 303
    assert finished.headers["location"] == "/projects"
    assert provider.exchanges[0][0] == "auth-code"
    assert provider.exchanges[0][2] == f"{ORIGIN}/studio/auth/callback"
    assert client.get("/studio/auth/session").json() == {
        "actor_id": "iap:accounts.google.com:112233",
        "email": "painter@example.com",
        "display_name": "Painter",
        "sign_in_method": SignInMethod.GOOGLE.value,
        "usage": {
            "stored_bytes": 0,
            "storage_limit_bytes": DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
            "running_jobs": 0,
            "concurrent_job_limit": DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
            "account_tier": "invited",
        },
        "admin": False,
    }


def test_the_session_cookie_is_http_only_secure_and_same_site_lax() -> None:
    client = _client(_auth_service())
    started = client.get("/studio/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    finished = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    cookie = next(
        value
        for _, value in finished.headers.multi_items()
        if value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=2592000" in cookie


def test_a_callback_with_a_forged_state_lands_on_the_sign_in_error() -> None:
    provider = FakeAuth0Provider(GOOGLE_IDENTITY)
    client = _client(_auth_service(auth0=provider))
    client.get("/studio/auth/login", follow_redirects=False)

    forged = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": "not-the-state"},
        follow_redirects=False,
    )

    assert forged.status_code == 303
    assert forged.headers["location"] == "/login?error=auth0"
    assert provider.exchanges == []
    assert SESSION_COOKIE_NAME not in client.cookies


def test_an_unverified_email_is_refused() -> None:
    unverified = GOOGLE_IDENTITY.model_copy(update={"email_verified": False})
    client = _client(_auth_service(identity=unverified))

    _sign_in(client)

    assert client.get("/studio/auth/session").status_code == 401


def test_an_unverified_email_is_asked_to_verify_before_signing_in() -> None:
    unverified = GOOGLE_IDENTITY.model_copy(update={"email_verified": False})
    client = _client(_auth_service(identity=unverified))
    started = client.get("/studio/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    refused = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert refused.headers["location"] == "/login?error=verify_email"
    assert SESSION_COOKIE_NAME not in client.cookies


def test_an_address_outside_the_allowlist_is_told_it_is_not_allowed() -> None:
    client = _client(_auth_service(allowlist=CLOSED_ALLOWLIST))
    started = client.get("/studio/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    refused = client.get(
        "/studio/auth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )

    assert refused.status_code == 303
    assert refused.headers["location"] == "/login?error=not_allowed"
    assert SESSION_COOKIE_NAME not in client.cookies


def test_a_non_google_subject_signs_in_under_its_email_identity() -> None:
    identity = Auth0Identity(
        subject="auth0|abc123", email="painter@example.com", email_verified=True
    )
    client = _client(_auth_service(identity=identity))

    _sign_in(client)

    assert client.get("/studio/auth/session").json() == {
        "actor_id": "email:painter@example.com",
        "email": "painter@example.com",
        "display_name": None,
        "sign_in_method": SignInMethod.AUTH0.value,
        "usage": {
            "stored_bytes": 0,
            "storage_limit_bytes": DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
            "running_jobs": 0,
            "concurrent_job_limit": DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
            "account_tier": "invited",
        },
        "admin": False,
    }


def test_a_google_sign_in_takes_over_the_projects_the_email_identity_owned() -> None:
    studio = _service()
    store = MemoryStudioAuthStore()
    email_identity = Auth0Identity(
        subject="auth0|abc123", email="painter@example.com", email_verified=True
    )
    email_auth = _auth_service(identity=email_identity, projects=studio, store=store)
    email_client = _client(email_auth, studio)
    _sign_in(email_client)
    project_id = email_client.post(
        "/studio/projects", json={"name": "Ember"}, headers={"Origin": ORIGIN}
    ).json()["project_id"]

    google_auth = _auth_service(projects=studio, store=store)
    google_client = _client(google_auth, studio)
    _sign_in(google_client)

    assert google_client.get("/studio/auth/session").json()["actor_id"] == (
        "iap:accounts.google.com:112233"
    )
    listed = google_client.get("/studio/projects").json()
    assert [item["project_id"] for item in listed] == [project_id]
    project = google_client.get(f"/studio/projects/{project_id}").json()
    assert project["manifest"]["owner_id"] == "iap:accounts.google.com:112233"


def test_a_cross_origin_post_with_a_session_cookie_is_refused() -> None:
    client = _client(_auth_service())
    _sign_in(client)

    forged = client.post(
        "/studio/projects",
        json={"name": "Ember"},
        headers={"Origin": "https://evil.invalid"},
    )
    missing_origin = client.post("/studio/projects", json={"name": "Ember"})
    read = client.get("/studio/projects")

    assert forged.status_code == 403
    assert forged.json()["detail"]["code"] == "csrf_origin_mismatch"
    assert missing_origin.status_code == 403
    assert read.status_code == 200


def test_an_api_key_bearer_is_unaffected_by_the_cookie_rules() -> None:
    studio = _service()
    client = _client(_auth_service(projects=studio), studio)
    _sign_in(client)
    key = client.post(
        "/studio/api-keys", json={"name": "cli"}, headers={"Origin": ORIGIN}
    ).json()["key"]
    client.cookies.clear()

    # No cookie, no Origin header: a bearer key is exempt from the CSRF check.
    created = client.post(
        "/studio/projects",
        json={"name": "Ember"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert created.status_code == 200
    assert created.json()["manifest"]["owner_id"] == "iap:accounts.google.com:112233"


def test_sign_out_revokes_the_session_and_hands_back_the_auth0_logout_url() -> None:
    client = _client(_auth_service())
    _sign_in(client)
    cookie = client.cookies[SESSION_COOKIE_NAME]

    signed_out = client.post("/studio/auth/sign-out", headers={"Origin": ORIGIN})

    assert signed_out.status_code == 200
    assert signed_out.json() == {
        "logout_url": (
            f"https://{AUTH0_DOMAIN}/v2/logout?client_id={CLIENT_ID}"
            f"&returnTo={ORIGIN.replace('://', '%3A%2F%2F')}%2Flogin"
        )
    }
    assert SESSION_COOKIE_NAME not in client.cookies
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    assert client.get("/studio/auth/session").status_code == 401


def test_an_unauthenticated_request_is_401_when_the_source_is_session() -> None:
    client = _client(_auth_service())

    assert client.get("/studio/projects").status_code == 401


def test_an_iap_assertion_provisions_a_user_and_hands_back_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_IAP_AUDIENCE", "/projects/1/global/backendServices/2"
    )
    monkeypatch.setattr(
        "magic_video_editor.studio.routes._verify_iap_assertion",
        lambda _assertion, _audience: {
            "iss": "https://cloud.google.com/iap",
            "sub": "accounts.google.com:112233",
            "email": "Painter@example.com",
        },
    )
    client = _client(_auth_service())

    handed_over = client.get(
        "/studio/auth/session", headers={"X-Goog-IAP-JWT-Assertion": "signed"}
    )

    assert handed_over.json() == {
        "actor_id": "iap:accounts.google.com:112233",
        "email": "painter@example.com",
        "display_name": None,
        "sign_in_method": SignInMethod.IAP.value,
        "usage": {
            "stored_bytes": 0,
            "storage_limit_bytes": DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
            "running_jobs": 0,
            "concurrent_job_limit": DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
            "account_tier": "invited",
        },
        "admin": False,
    }
    assert SESSION_COOKIE_NAME in client.cookies
    # The cookie alone now answers, with no assertion in play.
    assert client.get("/studio/auth/session").json()["actor_id"] == (
        "iap:accounts.google.com:112233"
    )


def test_a_stale_session_cookie_is_refused_and_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_auth_service())
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-live-session")

    assert client.get("/studio/auth/session").status_code == 401

    # Under the local gateway source the request still succeeds, so the response
    # carries the cookie deletion the browser needs.
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", "gateway")
    answered = client.get(
        "/studio/auth/session", headers={"X-Studio-Actor-Id": "local-user"}
    )

    assert answered.json()["actor_id"] == "local-user"
    assert f'{SESSION_COOKIE_NAME}=""' in answered.headers["set-cookie"]
