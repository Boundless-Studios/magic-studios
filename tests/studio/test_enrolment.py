"""Machine enrolment: pairing tokens, machine credentials, and their limits."""

from datetime import UTC, datetime, timedelta

import pytest
from outbound_gpu_worker_pool import MemoryWorkerRegistry, WorkerRegistration
from outbound_gpu_worker_pool.contracts import WorkerAuthError

from magic_video_editor.studio.enrolment import (
    MAX_WORKERS_PER_OWNER,
    PAIRING_TOKEN_TTL,
    EnrolledWorkerAuthenticator,
    MemoryStudioEnrolmentStore,
    PairingTokenUnusable,
    WorkerLimitReached,
    enrolled_worker_subject,
    generate_pairing_token,
    generate_worker_credential,
    hash_token,
    looks_like_pairing_token,
    looks_like_worker_credential,
    normalize_label,
    pairing_token_expiry,
)

OWNER = "iap:accounts.google.com:12345"
OTHER_OWNER = "iap:accounts.google.com:99999"


def _store() -> MemoryStudioEnrolmentStore:
    return MemoryStudioEnrolmentStore()


async def _mint(store: MemoryStudioEnrolmentStore, owner: str = OWNER) -> str:
    plaintext = generate_pairing_token()
    await store.mint_pairing_token(
        owner_id=owner,
        token_hash=hash_token(plaintext),
        expires_at=pairing_token_expiry(),
    )
    return plaintext


async def _redeem(
    store: MemoryStudioEnrolmentStore,
    token: str,
    *,
    worker_id: str = "machine-1",
    now: datetime | None = None,
) -> tuple[str, object]:
    credential = generate_worker_credential()
    worker = await store.redeem_pairing_token(
        token_hash=hash_token(token),
        worker_id=worker_id,
        label=normalize_label("Studio box"),
        credential_hash=hash_token(credential),
        now=now or datetime.now(UTC),
    )
    return credential, worker


def test_the_two_credentials_are_distinguishable_by_shape() -> None:
    """Shape decides which authenticator owns a credential, so a pairing token
    must never be mistaken for a machine credential or the reverse."""
    pairing = generate_pairing_token()
    credential = generate_worker_credential()

    assert looks_like_pairing_token(pairing)
    assert not looks_like_worker_credential(pairing)
    assert looks_like_worker_credential(credential)
    assert not looks_like_pairing_token(credential)
    assert pairing != credential


def test_a_pairing_token_lasts_an_hour() -> None:
    assert PAIRING_TOKEN_TTL == timedelta(hours=1)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert pairing_token_expiry(now) == now + timedelta(hours=1)


async def test_redeeming_binds_the_machine_to_whoever_minted_the_token() -> None:
    store = _store()
    token = await _mint(store)

    _, worker = await _redeem(store, token)

    assert worker.owner_id == OWNER
    assert worker.label == "Studio box"
    assert worker.revoked_at is None


async def test_a_token_can_be_redeemed_only_once() -> None:
    """The second attempt is what a stolen-and-replayed token looks like."""
    store = _store()
    token = await _mint(store)
    await _redeem(store, token)

    with pytest.raises(PairingTokenUnusable):
        await _redeem(store, token, worker_id="machine-2")


async def test_an_expired_token_is_refused() -> None:
    store = _store()
    token = await _mint(store)

    with pytest.raises(PairingTokenUnusable):
        await _redeem(store, token, now=datetime.now(UTC) + timedelta(hours=2))


async def test_an_unknown_token_is_refused_like_a_spent_one() -> None:
    """Same exception for unknown, spent and expired: otherwise a wrong token
    becomes an oracle telling an attacker which guesses exist."""
    store = _store()

    with pytest.raises(PairingTokenUnusable):
        await _redeem(store, generate_pairing_token())


async def test_a_machine_credential_authenticates_as_that_machine() -> None:
    store = _store()
    token = await _mint(store)
    credential, worker = await _redeem(store, token)

    identity = await EnrolledWorkerAuthenticator(store).authenticate(
        f"Bearer {credential}"
    )

    assert identity.worker_id == worker.worker_id
    # The pool keeps one worker per subject, so the subject names the machine;
    # the owner binds its leases as the tenant the studio enrolled it under.
    assert identity.subject == enrolled_worker_subject(worker.worker_id)
    assert identity.subject != OWNER
    assert identity.method == "enrolment"


async def test_a_machine_bound_under_its_owner_keeps_that_binding() -> None:
    """A machine that heartbeated before server-approved enrollment has a
    registry row whose subject is its owner. That row stays authoritative."""
    store = _store()
    credential, worker = await _redeem(store, await _mint(store))
    registry = MemoryWorkerRegistry()
    await registry.upsert(
        WorkerRegistration(worker_id=worker.worker_id, capabilities=(), tenant_id=OWNER),
        identity_subject=OWNER,
    )

    identity = await EnrolledWorkerAuthenticator(
        store, registry=registry
    ).authenticate(f"Bearer {credential}")

    assert identity.subject == OWNER


async def test_a_machine_enrolled_by_the_studio_keeps_its_own_subject() -> None:
    store = _store()
    credential, worker = await _redeem(store, await _mint(store))
    registry = MemoryWorkerRegistry()
    await registry.upsert(
        WorkerRegistration(worker_id=worker.worker_id, capabilities=(), tenant_id=OWNER),
        identity_subject=enrolled_worker_subject(worker.worker_id),
    )

    identity = await EnrolledWorkerAuthenticator(
        store, registry=registry
    ).authenticate(f"Bearer {credential}")

    assert identity.subject == enrolled_worker_subject(worker.worker_id)


async def test_a_revoked_machine_stops_authenticating() -> None:
    store = _store()
    token = await _mint(store)
    credential, worker = await _redeem(store, token)
    await store.revoke_worker(owner_id=OWNER, worker_id=worker.worker_id)

    with pytest.raises(WorkerAuthError):
        await EnrolledWorkerAuthenticator(store).authenticate(f"Bearer {credential}")


async def test_a_pairing_token_is_not_a_machine_credential() -> None:
    """Redeeming is the only thing a pairing token may do; presenting one to the
    pool must not authenticate anything."""
    store = _store()
    token = await _mint(store)

    with pytest.raises(WorkerAuthError):
        await EnrolledWorkerAuthenticator(store).authenticate(f"Bearer {token}")


async def test_an_unrecognised_credential_falls_through_to_the_configured_method() -> (
    None
):
    """Enrolled machines coexist with statically-configured ones; anything not
    shaped like a machine credential belongs to the method already in place."""
    store = _store()
    seen: list[str | None] = []

    class _Fallback:
        async def authenticate(self, authorization: str | None) -> str:
            seen.append(authorization)
            return "fell-through"

    authenticator = EnrolledWorkerAuthenticator(store, fallback=_Fallback())

    assert await authenticator.authenticate("Bearer legacy-token") == "fell-through"
    assert seen == ["Bearer legacy-token"]


async def test_one_persons_machine_is_invisible_to_another() -> None:
    store = _store()
    token = await _mint(store)
    _, worker = await _redeem(store, token)

    assert await store.list_workers(OTHER_OWNER) == ()
    assert await store.revoke_worker(
        owner_id=OTHER_OWNER, worker_id=worker.worker_id
    ) is None
    assert await store.rename_worker(
        owner_id=OTHER_OWNER, worker_id=worker.worker_id, label="mine now"
    ) is None
    assert (await store.list_workers(OWNER))[0].label == "Studio box"


async def test_an_owner_cannot_enrol_past_the_limit() -> None:
    store = _store()
    for index in range(MAX_WORKERS_PER_OWNER):
        await _redeem(store, await _mint(store), worker_id=f"machine-{index}")

    with pytest.raises(WorkerLimitReached):
        await _redeem(store, await _mint(store), worker_id="one-too-many")


async def test_revoking_frees_a_slot() -> None:
    store = _store()
    for index in range(MAX_WORKERS_PER_OWNER):
        await _redeem(store, await _mint(store), worker_id=f"machine-{index}")
    await store.revoke_worker(owner_id=OWNER, worker_id="machine-0")

    _, worker = await _redeem(store, await _mint(store), worker_id="replacement")

    assert worker.worker_id == "replacement"


def test_the_attach_script_names_the_app_paths_not_the_static_ones() -> None:
    """Static assets sit behind a CDN that holds a response for an hour and no
    deploy can purge, so both files are served by the app under /studio/. The
    first version used the static paths and came back as the studio's HTML."""
    from importlib.resources import files as package_files

    script = package_files("magic_video_editor").joinpath("attach.sh").read_text()

    assert "/studio/worker.py" in script
    assert '"$STUDIO_URL/worker.py"' not in script


def test_the_enrolment_migration_is_idempotent_and_add_only() -> None:
    """A migration that adds then drops burns a catalog slot on every start;
    ~320 starts exhausted Postgres's 1600-column ceiling on 2026-09-06."""
    from importlib.resources import files

    raw = (
        files("magic_video_editor.migrations")
        .joinpath("015_worker_enrolment.sql")
        .read_text()
    )
    # Comments explain the 2026-09-06 outage and name the very statements being
    # banned, so the ban has to read the statements, not the prose.
    sql = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("--")
    ).upper()

    assert "CREATE TABLE IF NOT EXISTS STUDIO_PAIRING_TOKENS" in sql
    assert "CREATE TABLE IF NOT EXISTS STUDIO_WORKERS" in sql
    assert "DROP " not in sql
    assert "ALTER " not in sql


def test_a_blank_label_never_blocks_enrolment() -> None:
    assert normalize_label(None) == "Unnamed machine"
    assert normalize_label("   ") == "Unnamed machine"
    assert normalize_label("  Desk rig ") == "Desk rig"
    assert len(normalize_label("x" * 500)) == 200
