#!/usr/bin/env python3
"""Redeem a pairing code on this machine and write the agent's configuration.

Run this on the machine with the GPU, not on the one where you got the code:

    python3 scripts/enrol_machine.py --code mvsp_xxxxxxxx --name "Desk rig"

The code is single-use and expires an hour after it was issued. If it fails,
ask for another rather than retrying the same one -- a code that has been
redeemed is refused exactly like one that never existed.

Writes `~/.config/magic-video-studio/worker.env`, readable only by you, holding
the machine's durable credential. That credential is shown once by the server
and is unrecoverable afterwards, so the file is the only copy.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_STUDIO_URL = "https://magicstudios.ai"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "magic-video-studio" / "worker.env"


def _redeem(studio_url: str, code: str, name: str | None) -> dict[str, object]:
    request = urllib.request.Request(
        f"{studio_url.rstrip('/')}/studio/workers/redeem",
        data=json.dumps({"token": code, "label": name}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise SystemExit(
            f"The studio refused the code ({error.code}). {detail}\n"
            "Codes are single-use and last an hour; ask for another."
        ) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"Could not reach {studio_url}: {error.reason}") from error


def _write_config(path: Path, redeemed: dict[str, object], coordinator: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Written by scripts/enrol_machine.py. Holds this machine's credential:",
        "# the studio showed it once and cannot show it again.",
        f"OGWP_WORKER_COORDINATOR_URL={coordinator}",
        f"OGWP_WORKER_ID={redeemed['worker_id']}",
        f"OGWP_WORKER_TOKEN={redeemed['credential']}",
        f"OGWP_WORKER_TENANT={redeemed['tenant_id']}",
        "OGWP_WORKER_AUTH=static",
        "",
    ]
    # Written 0600 from the start rather than chmod-ed afterwards, so the
    # credential is never briefly world-readable on a shared machine.
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="the pairing code from the studio")
    parser.add_argument("--name", default=None, help="a name for this machine")
    parser.add_argument("--studio-url", default=DEFAULT_STUDIO_URL)
    parser.add_argument("--coordinator-url", default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    arguments = parser.parse_args(argv)

    redeemed = _redeem(arguments.studio_url, arguments.code, arguments.name)
    coordinator = arguments.coordinator_url or f"{arguments.studio_url.rstrip('/')}/pool"
    _write_config(arguments.config, redeemed, coordinator)

    print(f"Enrolled '{redeemed['label']}' as {redeemed['worker_id']}.")
    print(f"Configuration written to {arguments.config} (readable only by you).")
    print("\nStart the agent with:\n")
    print(f"  set -a; . {arguments.config}; set +a")
    print("  python3 -m outbound_gpu_worker_pool.agent_main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
