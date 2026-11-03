"""Build the public downloads Studio serves under /studio/downloads: the MCP server's
wheel and the public agent skill.

Run before packaging the application (the Docker build does this automatically). The
wheel is built from a clean copy of only what ships, so tests, operator notes or a
local virtual environment can never end up in it. Both builds are reproducible: the
same source and build tools give the same bytes, so the published checksums stay put.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

# 1980-01-01, the earliest time a zip entry can record.
SOURCE_DATE_EPOCH = 315532800
SHIPPED = ("pyproject.toml", "README.md", "LICENSE", "src")


def build_mcp_wheel(project: Path, destination: Path) -> Path:
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        for name in SHIPPED:
            origin = project / name
            if origin.is_dir():
                shutil.copytree(origin, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
            else:
                shutil.copyfile(origin, source / name)
        wheels = Path(temporary) / "wheels"
        environment = {**os.environ, "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH)}
        # The image build has pip; a uv-managed environment has uv instead. Both call
        # the same setuptools backend.
        if importlib.util.find_spec("pip") is not None:
            command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(wheels), str(source)]
        else:
            command = ["uv", "build", "--wheel", "--quiet", "--out-dir", str(wheels), str(source)]
        subprocess.run(command, check=True, env=environment)
        built = list(wheels.glob("*.whl"))
        if len(built) != 1 or not re.fullmatch(r"magic_studios_mcp-[0-9][A-Za-z0-9_.+]*-py3-none-any\.whl", built[0].name):
            raise ValueError("the MCP server must build one universal wheel named magic_studios_mcp")
        target = destination / built[0].name
        shutil.copyfile(built[0], target)
        return target


def build_skill_zip(skill: Path, destination: Path, version: str) -> Path:
    """One `magic-studios/` folder, entries sorted, every timestamp and mode fixed."""
    target = destination / f"magic-studios-skill-{version}.zip"
    files = sorted(path for path in skill.rglob("*") if path.is_file() and path.name != ".DS_Store")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            entry = zipfile.ZipInfo(f"{skill.name}/{path.relative_to(skill).as_posix()}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    return target


def build_downloads(root: Path, destination: Path) -> dict:
    project = root / "mcp"
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.iterdir():
        stale.unlink()
    built = [
        (build_mcp_wheel(project, destination), "The Magic Studios MCP server. Install it with uv or pipx."),
        (build_skill_zip(project / "skill" / "magic-studios", destination, version), "The Magic Studios agent skill."),
    ]
    manifest = {"files": [
        {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "size": path.stat().st_size, "summary": summary}
        for path, summary in built
    ]}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    repository = Path(__file__).resolve().parents[1]
    print(json.dumps(build_downloads(repository, repository / "src/magic_video_editor/download_files"), indent=2))
