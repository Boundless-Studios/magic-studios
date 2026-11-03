"""Build the exact worker dependency shipped with Studio for protected download.

Run before packaging the application (the Docker build does this automatically).
Build infrastructure needs access to the pinned source; attached machines do not.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def build_worker_package(project_file: Path, destination: Path) -> None:
    project = tomllib.loads(project_file.read_text())
    requirements = [
        dependency for dependency in project["project"]["dependencies"]
        if dependency.startswith("outbound-gpu-worker-pool[")
    ]
    if len(requirements) != 1:
        raise ValueError("expected exactly one pinned worker dependency")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run([
            sys.executable, "-m", "pip", "wheel", "--no-deps",
            "--wheel-dir", temporary, requirements[0],
        ], check=True)
        wheels = list(Path(temporary).glob("*.whl"))
        if len(wheels) != 1 or not re.fullmatch(
            r"outbound_gpu_worker_pool-[A-Za-z0-9_.+]+-py3-none-any\.whl", wheels[0].name
        ):
            raise ValueError("the worker must build one universal Python wheel for Windows and Linux")
        wheel = wheels[0]
        manifest = {
            "filename": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            # Every build of the pool wheel is version 0.1.0, so the pinned source
            # commit and the digest are what tell one build from another.
            "commit": requirements[0].rsplit("@", 1)[-1],
        }
        for stale in destination.glob("*.whl"):
            stale.unlink()
        shutil.copyfile(wheel, destination / wheel.name)
        (destination / "manifest.json").write_text(json.dumps(manifest) + "\n")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    build_worker_package(root / "pyproject.toml", root / "src/magic_video_editor/worker_packages")
