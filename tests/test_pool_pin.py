"""The pool package pin, the lockfile and the installed package agree on one commit.

A pin to a PR-branch head can vanish once the branch is deleted (see #232); the
pin must be a commit on the pool's main, which reviewers confirm when it moves.
This test catches the pin, lock and environment drifting apart.
"""

import json
import re
import tomllib
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POOL = "outbound-gpu-worker-pool"


def _pinned_commit() -> str:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    pin = next(d for d in dependencies if d.startswith(POOL))
    return re.fullmatch(r".*@ git\+https://github\.com/Boundless-Studios/outbound-gpu-worker-pool@([0-9a-f]{40})", pin).group(1)


def test_pool_pin_lock_and_installed_package_name_the_same_commit():
    commit = _pinned_commit()
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = next(p for p in lock["package"] if p["name"] == POOL)
    assert locked["source"]["git"].endswith(f"?rev={commit}#{commit}")
    direct_url = json.loads(distribution(POOL).read_text("direct_url.json"))
    assert direct_url["vcs_info"]["commit_id"] == commit
