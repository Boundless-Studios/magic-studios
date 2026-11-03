"""Download and install a protected worker wheel. Uses only Python's stdlib.

Both attach scripts download this file before the worker is installed. The code
arrives through MVS_CODE, never a URL or command argument, and is not redeemed
here: a failed installation can be retried before the pairing code expires.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_PACKAGE_BYTES = 64 * 1024 * 1024
WHEEL_NAME = re.compile(r"outbound_gpu_worker_pool-[A-Za-z0-9_.+]+-py3-none-any\.whl")


class InstallFailed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A pairing code must never be forwarded to a different host or path.
        return None


def install(studio_url: str, root: Path, code: str) -> None:
    parsed = urllib.parse.urlsplit(studio_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise InstallFailed("the studio address must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstallFailed("the studio address must not contain credentials, a query, or a fragment")
    if not code:
        raise InstallFailed("no pairing code; copy the command from your machines page")
    request = urllib.request.Request(
        f"{studio_url.rstrip('/')}/studio/workers/package",
        data=json.dumps({"token": code}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            filename = response.headers.get("X-Worker-Package-Filename", "")
            digest = response.headers.get("X-Worker-Package-SHA256", "")
            commit = response.headers.get("X-Worker-Package-Commit", "")
            if not WHEEL_NAME.fullmatch(filename) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise InstallFailed("the studio returned invalid worker package metadata")
            data = response.read(MAX_PACKAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise InstallFailed("the studio refused this pairing code; get a new code on your machines page") from None
        if exc.code == 503:
            raise InstallFailed("the studio's worker package is unavailable; ask the operator to check the deployment") from None
        raise InstallFailed(f"worker package download failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise InstallFailed("could not download the worker package; check the connection to the studio and retry") from None
    if len(data) > MAX_PACKAGE_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise InstallFailed("worker package verification failed; retry the download")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = root / "install.log"
    with tempfile.TemporaryDirectory(prefix="package-", dir=root) as temporary:
        wheel = Path(temporary) / filename
        wheel.write_bytes(data)
        # Never pass the code into uv, build hooks or installed dependencies.
        environment = dict(os.environ)
        environment.pop("MVS_CODE", None)
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(log_path, 0o600)
        with os.fdopen(descriptor, "w") as log:
            try:
                result = subprocess.run(
                    ["uv", "pip", "install", "--python", sys.executable, "--reinstall-package", "outbound-gpu-worker-pool", f"{wheel}[agent,comfy]"],
                    stdout=log, stderr=subprocess.STDOUT, env=environment, check=False,
                )
            except OSError:
                raise InstallFailed("could not start uv; rerun the attach command to prepare the installer") from None
        if result.returncode:
            raise InstallFailed(f"worker dependency installation failed; details are in {log_path}")
    # Every build is version 0.1.0, so worker-update.py tells builds apart by this record.
    (root / "pool-package.json").write_text(json.dumps({"filename": filename, "sha256": digest, "commit": commit}))


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: worker-install.py <studio-url> <worker-directory>", file=sys.stderr)
        return 2
    code = os.environ.pop("MVS_CODE", "")
    try:
        install(sys.argv[1], Path(sys.argv[2]), code)
    except (InstallFailed, OSError) as exc:
        print(f"Attaching failed: {exc}", file=sys.stderr)
        print("This attempt has not spent your pairing code. Retry before it expires (one hour after creation).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
