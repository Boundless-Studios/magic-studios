"""Machine credentials renew themselves, so nobody handles one after attaching."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from outbound_gpu_worker_pool.contracts import WorkerAuthError

from magic_video_editor.studio.enrolment import (
    PREVIOUS_CREDENTIAL_GRACE,
    WORKER_CREDENTIAL_TTL,
    CredentialRotationRefused,
    EnrolledWorkerAuthenticator,
    MemoryStudioEnrolmentStore,
    generate_pairing_token,
    generate_worker_credential,
    hash_token,
    normalize_label,
    pairing_token_expiry,
)
from magic_video_editor.worker_runner import (
    RENEW_WHEN_REMAINING,
    CredentialLost,
    RotatingCredential,
)

OWNER = "iap:accounts.google.com:12345"


async def _attach(store: MemoryStudioEnrolmentStore) -> tuple[str, str]:
    token = generate_pairing_token()
    await store.mint_pairing_token(
        owner_id=OWNER, token_hash=hash_token(token), expires_at=pairing_token_expiry()
    )
    credential = generate_worker_credential()
    worker = await store.redeem_pairing_token(
        token_hash=hash_token(token),
        worker_id="machine-1",
        label=normalize_label("Desk rig"),
        credential_hash=hash_token(credential),
        now=datetime.now(UTC),
    )
    return credential, worker.worker_id


async def _rotate(store: MemoryStudioEnrolmentStore, credential: str) -> str:
    replacement = generate_worker_credential()
    await store.rotate_credential(
        credential_hash=hash_token(credential),
        new_credential_hash=hash_token(replacement),
        now=datetime.now(UTC),
    )
    return replacement


async def test_a_new_machine_gets_a_credential_that_expires() -> None:
    """A credential that never expires is one that stays useful forever once
    leaked; the whole point of rotation is that it does not."""
    store = MemoryStudioEnrolmentStore()
    credential, _ = await _attach(store)

    worker = await store.worker_by_credential(hash_token(credential))

    assert worker is not None
    assert worker.credential_expires_at is not None
    remaining = worker.credential_expires_at - datetime.now(UTC)
    assert timedelta(days=29) < remaining <= WORKER_CREDENTIAL_TTL


async def test_rotating_swaps_the_credential_and_extends_the_window() -> None:
    store = MemoryStudioEnrolmentStore()
    credential, _ = await _attach(store)

    replacement = await _rotate(store, credential)

    assert replacement != credential
    worker = await store.worker_by_credential(hash_token(replacement))
    assert worker is not None
    assert worker.rotated_at is not None
    assert worker.credential_expires_at is not None
    assert worker.credential_expires_at - datetime.now(UTC) > RENEW_WHEN_REMAINING


async def test_the_old_credential_still_works_briefly_after_rotating() -> None:
    """A reply lost in flight, or a restart mid-rotation, must not lock a machine
    out of its own account."""
    store = MemoryStudioEnrolmentStore()
    credential, _ = await _attach(store)
    await _rotate(store, credential)

    assert await store.worker_by_credential(hash_token(credential)) is not None
    assert PREVIOUS_CREDENTIAL_GRACE == timedelta(hours=24)


async def test_the_old_credential_cannot_rotate_again() -> None:
    """This is what a stolen copy looks like once the real machine has moved on:
    it can still work for a moment, but it can never take over the account."""
    store = MemoryStudioEnrolmentStore()
    credential, _ = await _attach(store)
    await _rotate(store, credential)

    with pytest.raises(CredentialRotationRefused):
        await _rotate(store, credential)


async def test_a_revoked_machine_cannot_rotate_back_into_service() -> None:
    store = MemoryStudioEnrolmentStore()
    credential, worker_id = await _attach(store)
    await store.revoke_worker(owner_id=OWNER, worker_id=worker_id)

    with pytest.raises(CredentialRotationRefused):
        await _rotate(store, credential)


async def test_an_expired_credential_stops_authenticating() -> None:
    store = MemoryStudioEnrolmentStore()
    credential, worker_id = await _attach(store)
    stored = store._workers[worker_id]
    store._workers[worker_id] = stored.model_copy(
        update={"credential_expires_at": datetime.now(UTC) - timedelta(minutes=1)}
    )

    with pytest.raises(WorkerAuthError):
        await EnrolledWorkerAuthenticator(store).authenticate(f"Bearer {credential}")


# --- the agent side ------------------------------------------------------------


def _config(tmp_path: Path, expires_at: datetime | None) -> Path:
    path = tmp_path / "worker.env"
    lines = ["OGWP_WORKER_TOKEN=mvsw_" + "a" * 32, "OGWP_WORKER_ID=machine-1"]
    if expires_at is not None:
        lines.append(f"MVS_CREDENTIAL_EXPIRES_AT={expires_at.isoformat()}")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_agent_keeps_its_credential_while_it_is_fresh(tmp_path: Path) -> None:
    calls: list[str] = []
    path = _config(tmp_path, datetime.now(UTC) + timedelta(days=25))
    credential = RotatingCredential(
        path,
        studio_url="https://studio.example",
        rotate=lambda _url, current: calls.append(current) or {},  # type: ignore[func-returns-value]
    )

    assert credential() == "mvsw_" + "a" * 32
    assert calls == []


def test_the_agent_renews_before_the_window_closes(tmp_path: Path) -> None:
    path = _config(tmp_path, datetime.now(UTC) + timedelta(days=3))
    replacement = "mvsw_" + "b" * 32
    credential = RotatingCredential(
        path,
        studio_url="https://studio.example",
        rotate=lambda _url, _current: {
            "credential": replacement,
            "credential_expires_at": (
                datetime.now(UTC) + timedelta(days=30)
            ).isoformat(),
        },
    )

    assert credential() == replacement
    # Persisted, so a restart uses the new one rather than the one just retired.
    assert replacement in path.read_text()
    assert credential() == replacement


def test_a_machine_attached_before_rotation_existed_gains_an_expiry(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path, None)
    replacement = "mvsw_" + "c" * 32
    credential = RotatingCredential(
        path,
        studio_url="https://studio.example",
        rotate=lambda _url, _current: {
            "credential": replacement,
            "credential_expires_at": (
                datetime.now(UTC) + timedelta(days=30)
            ).isoformat(),
        },
    )

    assert credential() == replacement
    assert "MVS_CREDENTIAL_EXPIRES_AT" in path.read_text()


def test_a_network_failure_leaves_the_machine_working(tmp_path: Path) -> None:
    """The current credential is good for days, so a blip must not stop work."""

    def _boom(_url: str, _current: str) -> dict[str, object]:
        raise TimeoutError("studio unreachable")

    path = _config(tmp_path, datetime.now(UTC) + timedelta(days=3))
    credential = RotatingCredential(
        path, studio_url="https://studio.example", rotate=_boom
    )

    assert credential() == "mvsw_" + "a" * 32


def test_a_refused_renewal_stops_the_machine_rather_than_retrying(
    tmp_path: Path,
) -> None:
    """Refusal means removed, or rotated by something else. Both want a person."""

    def _refused(_url: str, _current: str) -> dict[str, object]:
        raise PermissionError("no longer accepted")

    path = _config(tmp_path, datetime.now(UTC) + timedelta(days=3))
    credential = RotatingCredential(
        path, studio_url="https://studio.example", rotate=_refused
    )

    with pytest.raises(CredentialLost):
        credential()


def test_renewal_is_not_attempted_on_every_call(tmp_path: Path) -> None:
    """The credential is read on every lease poll; renewal must not be."""
    attempts: list[str] = []
    clock = iter([0.0, 1.0, 2.0, 3.0])

    def _count(_url: str, current: str) -> dict[str, object]:
        attempts.append(current)
        raise TimeoutError("still down")

    path = _config(tmp_path, datetime.now(UTC) + timedelta(days=3))
    credential = RotatingCredential(
        path,
        studio_url="https://studio.example",
        rotate=_count,
        monotonic=lambda: next(clock),
    )

    credential()
    credential()
    credential()

    assert len(attempts) == 1
