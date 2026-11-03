"""The auth store contract, the identity rules, and Auth0 id-token verification."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from google.auth import crypt
from google.auth import jwt as google_jwt

from magic_video_editor.studio.auth import (
    Auth0Client,
    Auth0Identity,
    Auth0JwksVerifier,
    Auth0Settings,
    EmailAllowlist,
    MemoryStudioAuthStore,
    OAuthHandoff,
    SignInMethod,
    decode_oauth_handoff,
    email_actor_id,
    encode_oauth_handoff,
    google_actor_id,
    google_sub_from_iap_subject,
    hash_token,
    identity_from_claims,
    pkce_pair,
    safe_return_path,
)
from magic_video_editor.studio.auth_service import (
    EmailNotAllowed,
    SignInRejected,
    StudioAuthService,
)

DOMAIN = "studio.example.auth0.com"
ISSUER = f"https://{DOMAIN}/"
CLIENT_ID = "studio-client-id"
SETTINGS = Auth0Settings(
    domain=DOMAIN, client_id=CLIENT_ID, client_secret="not-a-real-secret"
)


def test_an_allowlist_with_nothing_in_it_admits_nobody() -> None:
    closed = EmailAllowlist(emails=frozenset(), domains=frozenset())

    assert not closed.permits("anyone@example.com")


def test_the_allowlist_matches_addresses_and_domains_case_insensitively() -> None:
    allowlist = EmailAllowlist(
        emails=frozenset({"one@other.invalid"}), domains=frozenset({"example.com"})
    )

    assert allowlist.permits("Painter@Example.COM")
    assert allowlist.permits("ONE@other.invalid")
    assert not allowlist.permits("painter@elsewhere.invalid")


def test_the_allowlist_reads_both_environment_lists() -> None:
    allowlist = EmailAllowlist.from_env(
        {
            "MAGIC_VIDEO_EDITOR_ALLOWED_EMAILS": "a@x.invalid, B@y.invalid",
            "MAGIC_VIDEO_EDITOR_ALLOWED_EMAIL_DOMAINS": "example.com ,",
        }
    )

    assert allowlist.emails == {"a@x.invalid", "b@y.invalid"}
    assert allowlist.domains == {"example.com"}


def test_a_google_identity_keeps_the_actor_id_iap_produced() -> None:
    assert google_sub_from_iap_subject("accounts.google.com:112233") == "112233"
    assert google_actor_id("112233") == "iap:accounts.google.com:112233"
    assert email_actor_id("Painter@Example.com") == "email:painter@example.com"


def test_only_a_google_federated_subject_carries_a_google_sub() -> None:
    google = Auth0Identity(
        subject="google-oauth2|112233", email="p@example.com", email_verified=True
    )
    database = Auth0Identity(
        subject="auth0|abc123", email="p@example.com", email_verified=True
    )

    assert google.google_sub == "112233"
    assert database.google_sub is None


def test_only_a_same_origin_absolute_path_survives_as_a_return_path() -> None:
    assert safe_return_path("/projects/1?tab=motions") == "/projects/1?tab=motions"
    assert safe_return_path("//evil.invalid/steal") is None
    assert safe_return_path("https://evil.invalid") is None
    assert safe_return_path("/back\\slash") is None
    assert safe_return_path(None) is None


def test_the_pkce_challenge_is_url_safe_and_unpadded() -> None:
    verifier, challenge = pkce_pair()

    assert verifier != challenge
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


def test_the_oauth_handoff_round_trips_and_rejects_junk() -> None:
    handoff = OAuthHandoff(state="s", code_verifier="v", return_to="/projects")

    assert decode_oauth_handoff(encode_oauth_handoff(handoff)) == handoff
    assert decode_oauth_handoff("not-base64-json") is None


def test_the_tenant_urls_are_derived_from_the_domain() -> None:
    assert SETTINGS.issuer == ISSUER
    assert SETTINGS.authorization_endpoint == f"https://{DOMAIN}/authorize"
    assert SETTINGS.token_endpoint == f"https://{DOMAIN}/oauth/token"
    assert SETTINGS.jwks_uri == f"https://{DOMAIN}/.well-known/jwks.json"
    assert SETTINGS.logout_url("https://studio.invalid/login") == (
        f"https://{DOMAIN}/v2/logout?client_id={CLIENT_ID}"
        "&returnTo=https%3A%2F%2Fstudio.invalid%2Flogin"
    )


def test_the_tenant_settings_need_every_value_to_be_configured() -> None:
    complete = {
        "MAGIC_VIDEO_EDITOR_AUTH0_DOMAIN": DOMAIN,
        "MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_ID": CLIENT_ID,
        "MAGIC_VIDEO_EDITOR_AUTH0_CLIENT_SECRET": "not-a-real-secret",
    }

    assert Auth0Settings.from_env(complete) == SETTINGS
    for missing in complete:
        assert Auth0Settings.from_env({**complete, missing: ""}) is None


def test_claims_from_another_issuer_are_refused() -> None:
    claims = {
        "iss": "https://someone-else.invalid/",
        "sub": "google-oauth2|1",
        "email": "p@example.com",
    }

    with pytest.raises(ValueError, match="issuer"):
        identity_from_claims(claims, issuer=ISSUER)


def test_claims_without_a_subject_or_email_are_refused() -> None:
    with pytest.raises(ValueError, match="sub or email"):
        identity_from_claims({"iss": ISSUER, "sub": "auth0|1"}, issuer=ISSUER)


class _Tenant:
    """A signing key plus the JWKS document and token endpoint that publish it."""

    def __init__(self, *, kid: str = "key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self._key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
        self._signer = crypt.RSASigner.from_string(pem, kid)
        self.jwks_requests = 0

    def id_token(self, claims: dict[str, Any]) -> str:
        return google_jwt.encode(self._signer, claims).decode("ascii")

    def jwks(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url(numbers.n),
                    "e": _b64url(numbers.e),
                }
            ]
        }

    def client(self, *, id_token: str) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/jwks.json":
                self.jwks_requests += 1
                return httpx.Response(200, json=self.jwks())
            return httpx.Response(200, json={"id_token": id_token})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "google-oauth2|112233",
        "email": "painter@example.com",
        "email_verified": True,
        "name": "Painter",
        "iat": now,
        "exp": now + 300,
        **overrides,
    }


async def test_the_code_exchange_verifies_the_id_token_against_the_tenant_keys() -> None:
    tenant = _Tenant()
    async with tenant.client(id_token=tenant.id_token(_claims())) as http:
        client = Auth0Client(SETTINGS, client=http)

        identity = await client.exchange(
            code="auth-code",
            code_verifier="verifier",
            redirect_uri="https://studio.invalid/studio/auth/callback",
        )

    assert identity == Auth0Identity(
        subject="google-oauth2|112233",
        email="painter@example.com",
        email_verified=True,
        display_name="Painter",
    )


async def test_a_token_for_another_audience_is_refused() -> None:
    tenant = _Tenant()
    forged = tenant.id_token(_claims(aud="someone-else"))
    async with tenant.client(id_token=forged) as http:
        client = Auth0Client(SETTINGS, client=http)

        with pytest.raises(ValueError, match="[Aa]udience"):
            await client.exchange(
                code="auth-code",
                code_verifier="verifier",
                redirect_uri="https://studio.invalid/studio/auth/callback",
            )


async def test_an_expired_token_is_refused() -> None:
    tenant = _Tenant()
    stale = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
    forged = tenant.id_token(_claims(iat=stale, exp=stale + 300))
    async with tenant.client(id_token=forged) as http:
        client = Auth0Client(SETTINGS, client=http)

        with pytest.raises(ValueError):
            await client.exchange(
                code="auth-code",
                code_verifier="verifier",
                redirect_uri="https://studio.invalid/studio/auth/callback",
            )


async def test_a_token_signed_by_a_stranger_is_refused() -> None:
    tenant = _Tenant()
    stranger = _Tenant(kid=tenant.kid)
    async with tenant.client(id_token=stranger.id_token(_claims())) as http:
        verifier = Auth0JwksVerifier(SETTINGS, client=http)

        with pytest.raises(ValueError):
            await verifier.verify(stranger.id_token(_claims()))


async def test_the_signing_keys_are_fetched_once_and_then_cached() -> None:
    tenant = _Tenant()
    token = tenant.id_token(_claims())
    async with tenant.client(id_token=token) as http:
        verifier = Auth0JwksVerifier(SETTINGS, client=http)

        for _ in range(3):
            await verifier.verify(token)

    assert tenant.jwks_requests == 1


async def test_a_token_response_without_an_id_token_is_a_failed_sign_in() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "no-id-token-here"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = Auth0Client(SETTINGS, client=http)

        with pytest.raises(ValueError, match="id_token"):
            await client.exchange(
                code="auth-code",
                code_verifier="verifier",
                redirect_uri="https://studio.invalid/studio/auth/callback",
            )


def test_the_authorization_url_carries_the_pkce_challenge_and_prompt() -> None:
    url = Auth0Client(SETTINGS).authorization_url(
        state="state-value",
        code_challenge="challenge-value",
        redirect_uri="https://studio.invalid/studio/auth/callback",
    )

    assert url.startswith(f"https://{DOMAIN}/authorize?")
    assert "response_type=code" in url
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "prompt=select_account" in url
    assert "scope=openid+profile+email" in url


async def test_a_user_keeps_one_row_across_an_email_and_a_google_sign_in() -> None:
    store = MemoryStudioAuthStore()

    first = await store.upsert_user(email="Painter@Example.com")
    second = await store.upsert_user(
        email="painter@example.com", google_sub="112233", display_name="Painter"
    )

    assert first.user_id == second.user_id
    assert first.actor_id == "email:painter@example.com"
    assert second.actor_id == "iap:accounts.google.com:112233"
    assert await store.get_user_by_google_sub("112233") == second


async def test_an_unverified_or_unlisted_identity_never_gets_a_user() -> None:
    store = MemoryStudioAuthStore()
    service = StudioAuthService(
        store, allowlist=EmailAllowlist(emails=frozenset(), domains=frozenset())
    )

    with pytest.raises(SignInRejected):
        await service.sign_in_with_auth0(
            Auth0Identity(
                subject="auth0|1", email="painter@example.com", email_verified=False
            )
        )
    with pytest.raises(EmailNotAllowed):
        await service.sign_in_with_auth0(
            Auth0Identity(
                subject="auth0|1", email="painter@example.com", email_verified=True
            )
        )

    assert await store.get_user_by_email("painter@example.com") is None


async def test_a_read_burst_costs_at_most_one_last_seen_write_a_minute() -> None:
    store = MemoryStudioAuthStore()
    service = StudioAuthService(
        store, allowlist=EmailAllowlist(emails=frozenset(), domains=frozenset({"example.com"}))
    )
    user = await store.upsert_user(email="painter@example.com")
    token = await service.issue_session(user, sign_in_method=SignInMethod.AUTH0)
    created = await store.get_session(hash_token(token))
    assert created is not None

    for _ in range(3):
        assert await service.resolve_session(token) is not None
    unchanged = await store.get_session(hash_token(token))
    assert unchanged is not None
    assert unchanged.last_seen_at == created.last_seen_at

    stale = created.model_copy(
        update={"last_seen_at": created.last_seen_at - timedelta(minutes=2)}
    )
    store._sessions[created.session_id] = stale
    await service.resolve_session(token)
    renewed = await store.get_session(hash_token(token))

    assert renewed is not None
    assert renewed.last_seen_at > stale.last_seen_at
    assert renewed.expires_at > created.expires_at


async def test_a_session_can_be_renewed_and_revoked() -> None:
    store = MemoryStudioAuthStore()
    user = await store.upsert_user(email="painter@example.com")
    expires_at = datetime.now(UTC) + timedelta(days=30)
    await store.create_session(
        session_id="hash",
        user_id=user.user_id,
        sign_in_method=SignInMethod.AUTH0,
        expires_at=expires_at,
        user_agent="pytest",
    )
    renewed_at = datetime.now(UTC) + timedelta(minutes=5)

    await store.touch_session(
        "hash", seen_at=renewed_at, expires_at=renewed_at + timedelta(days=30)
    )
    session = await store.get_session("hash")

    assert session is not None
    assert session.last_seen_at == renewed_at
    assert session.expires_at > expires_at

    await store.delete_session("hash")
    assert await store.get_session("hash") is None


async def test_a_session_ends_once_its_user_is_no_longer_allowed_in() -> None:
    """Taking an address off the allowlist signs it out on its next request, even if
    the session was issued a moment before."""
    store = MemoryStudioAuthStore()
    listed = EmailAllowlist(emails=frozenset({"ada@example.com"}), domains=frozenset())
    before = StudioAuthService(store, allowlist=listed)
    user = await store.upsert_user(email="Ada@Example.com")
    token = await before.issue_session(user, sign_in_method=SignInMethod.AUTH0)
    assert await before.resolve_session(token) is not None

    # The same sessions, served after the address came off the list.
    after = StudioAuthService(store, allowlist=EmailAllowlist(emails=frozenset(), domains=frozenset()))

    assert await after.resolve_session(token) is None
    assert await store.get_session(hash_token(token)) is None

