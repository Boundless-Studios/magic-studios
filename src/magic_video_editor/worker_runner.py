"""Run an attached machine's agent, keeping its own credential alive.

Nobody handles a credential after the first attach. The agent holds one that
expires, renews it well before it does, and writes the replacement to disk
atomically so a crash mid-rotation cannot leave the machine with neither.

The pool's `WorkerAgent` takes its credential as a callable, which is the whole
integration point: this module supplies one that rotates when it needs to.

This file is published as `/worker.py` and runs on the attached machine, which
cannot install this (private) package -- so it depends on nothing but the
standard library and the public pool agent, and the studio serves it
at /studio/worker.py. Keep it self-contained.
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Renew with a week to spare. A machine asleep over a long weekend, or offline
# while someone is on holiday, still wakes up inside its window.
RENEW_WHEN_REMAINING = timedelta(days=7)
# Never hammer the studio: a rotation that fails is retried on this cadence, not
# on every single lease poll.
MIN_SECONDS_BETWEEN_ATTEMPTS = 300.0


class CredentialLost(RuntimeError):
    """The stored credential no longer works and cannot be renewed.

    Terminal by design. It means the machine was removed in the studio, or its
    credential was rotated by something else -- and in the second case the right
    response is a person looking, not a silent retry.
    """


class RotatingCredential:
    """The machine's credential, renewed before it lapses.

    Callable, so it can be handed straight to `WorkerAgent(credential=...)`.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        studio_url: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        rotate: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self._config_path = config_path
        self._studio_url = studio_url.rstrip("/")
        self._now = now
        self._monotonic = monotonic
        self._rotate = rotate or _rotate_over_http
        self._last_attempt: float | None = None

    def __call__(self) -> str:
        config = _read_config(self._config_path)
        credential = config.get("OGWP_WORKER_TOKEN")
        if not credential:
            raise CredentialLost(f"no credential in {self._config_path}")
        if self._should_renew(config) and self._attempt_allowed():
            renewed = self._renew(credential, config)
            if renewed is not None:
                return renewed
        return credential

    def _should_renew(self, config: dict[str, str]) -> bool:
        raw = config.get("MVS_CREDENTIAL_EXPIRES_AT")
        if not raw:
            # Attached before rotation existed: renew at the next opportunity so
            # the machine gains an expiry rather than staying on a static secret.
            return True
        try:
            expires_at = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return expires_at - self._now() <= RENEW_WHEN_REMAINING

    def _attempt_allowed(self) -> bool:
        moment = self._monotonic()
        if (
            self._last_attempt is not None
            and moment - self._last_attempt < MIN_SECONDS_BETWEEN_ATTEMPTS
        ):
            return False
        self._last_attempt = moment
        return True

    def _renew(self, credential: str, config: dict[str, str]) -> str | None:
        try:
            payload = self._rotate(self._studio_url, credential)
        except PermissionError as exc:
            raise CredentialLost(str(exc)) from exc
        except Exception:  # noqa: BLE001 - every transport failure means "try later"
            # A network blip must not stop the machine working: the current
            # credential is still valid for days, so carry on and try later.
            return None
        replacement = payload["credential"]
        config["OGWP_WORKER_TOKEN"] = replacement
        expires_at = payload.get("credential_expires_at")
        if expires_at:
            config["MVS_CREDENTIAL_EXPIRES_AT"] = str(expires_at)
        _write_config(self._config_path, config)
        return replacement


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "magic-video-studio" / "worker.env"


def default_config_path() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "magic-video-studio" / "worker.env"
    return DEFAULT_CONFIG_PATH


def _profile_plugin(profile: str, versions: dict[str, str], environment: dict[str, str]):
    """The verified profile's plugin. It restarts a stopped ComfyUI with the
    configured start command, as the generic plugin does; pool packages from
    before that option keep running without it."""
    import httpx
    from outbound_gpu_worker_pool.machine_profiles import ProfileComfyPlugin
    try:
        from outbound_gpu_worker_pool.agent_main import _comfy_runtime
    except ImportError:
        runtime = {}
    else:
        runtime = _comfy_runtime(environment)
    return ProfileComfyPlugin(profile, versions, client=httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3)),
                              base_url=environment["OGWP_COMFY_URL"], **runtime)


async def run(config_path: Path | None = None) -> int:
    """Poll for work forever, renewing the credential as needed."""
    import asyncio
    import logging

    from outbound_gpu_worker_pool.agent_main import build_agent_from_env

    logger = logging.getLogger("magic-video-worker")
    path = config_path or Path(
        os.environ.get("MVS_WORKER_CONFIG", str(default_config_path()))
    )
    for name, value in _read_config(path).items():
        os.environ.setdefault(name, value)
    # Older installations may update the runner without the new helper.
    try:
        from worker_runtime import configure_runtime
    except ImportError:
        try:
            from magic_video_editor.worker_runtime import configure_runtime
        except ImportError:
            configure_runtime = None
    if configure_runtime is not None:
        configure_runtime(os.environ)
    import shutil
    from outbound_gpu_worker_pool.agent_main import KNOWN_PLUGINS
    try:
        from cpu_worker import MovieCpuPlugin
    except ImportError:
        try:
            from magic_video_editor.cpu_worker import MovieCpuPlugin
        except ImportError:
            MovieCpuPlugin = None
    environment = dict(os.environ)
    if MovieCpuPlugin is not None and shutil.which("ffmpeg") and shutil.which("ffprobe"):
        KNOWN_PLUGINS["movie-cpu"] = lambda _: MovieCpuPlugin()
        configured = environment.get("OGWP_WORKER_PLUGINS", "deterministic-echo")
        environment["OGWP_WORKER_PLUGINS"] = ",".join(dict.fromkeys([*configured.split(","), "movie-cpu"]))
        logger.info("CPU movie processing enabled (FFmpeg); no GPU required")
    else:
        logger.info("CPU movie processing unavailable: install FFmpeg and update the worker")
    profile = environment.get("MVS_WORKER_PROFILE")
    if profile:
        import httpx
        from outbound_gpu_worker_pool.machine_profiles import prepare_environment
        prepared = await prepare_environment(environment)
        environment.update(prepared)
        versions = json.loads(prepared["OGWP_WORKER_RUNTIME_VERSIONS"])
        KNOWN_PLUGINS["magic-comfy-profile"] = lambda _: _profile_plugin(profile, versions, environment)
        configured = environment.get("OGWP_WORKER_PLUGINS", "")
        # An explicit profile serves only its verified graph, never the generic
        # comfy plugin's unrelated model capabilities.
        names = [n for n in configured.split(",") if n and n != "comfy-workflow"]
        environment["OGWP_WORKER_PLUGINS"] = ",".join(dict.fromkeys([*names, "magic-comfy-profile"]))
    agent = build_agent_from_env(environment)
    # The agent asks for its credential on every request, so replacing this one
    # callable is the whole of the integration: a renewal needs no restart.
    agent.credential = RotatingCredential(
        path, studio_url=os.environ.get("MVS_STUDIO_URL", "https://magicstudios.ai")
    )

    # The agent's own loop, not a hand-rolled one: it backs off from 2s to 60s
    # with jitter when there is no work. Calling run_once() in a loop skips all
    # of that and polls as fast as the network allows, which is what this did at
    # first -- heartbeating every 1.5 seconds and drawing 429s from an API that
    # also serves the studio to people.
    import signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(number, stop.set)
            installed_signals.append(number)
        except (NotImplementedError, RuntimeError):
            # Windows' event loop does not implement POSIX signal handlers.
            pass
    try:
        await agent.run_forever(stop)
    except CredentialLost:
        # Removed in the studio, or rotated by something that is not this
        # machine. Retrying cannot fix either, and looping quietly would hide
        # the second case, which is the one worth noticing.
        logger.exception("this machine is no longer attached")
        return 1
    finally:
        for number in installed_signals:
            loop.remove_signal_handler(number)
    return 0


def main() -> int:
    import asyncio
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return asyncio.run(run())


def _rotate_over_http(studio_url: str, credential: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{studio_url}/studio/workers/rotate",
        data=json.dumps({"credential": credential}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        # 400 is the studio saying this credential may not rotate: removed, or
        # already rotated by something else. Retrying cannot help.
        if error.code == 400:
            raise PermissionError(
                "this machine's credential is no longer accepted; "
                "remove it in the studio and attach the machine again"
            ) from error
        raise


def _read_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def _write_config(path: Path, values: dict[str, str]) -> None:
    """Replace the file atomically and 0600 from the start, so a crash mid-write
    cannot leave a machine with a truncated credential and a reader can never see
    a world-readable one."""
    body = "\n".join(
        [
            "# Written by the Magic Video Studio worker. The credential renews",
            "# itself; there is nothing here to maintain by hand.",
            *(f"{name}={value}" for name, value in sorted(values.items())),
            "",
        ]
    )
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
