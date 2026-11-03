"""The MCP server wheel and the public skill: what they contain, that they rebuild to
the same bytes, and that only what the manifest names is served."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import tomllib
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_video_editor.public_downloads import create_download_router

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "mcp/pyproject.toml").read_text())["project"]["version"]
WHEEL = f"magic_studios_mcp-{VERSION}-py3-none-any.whl"
INFO = f"magic_studios_mcp-{VERSION}.dist-info"
# Operator material that must never reach a public download.
OPERATOR_MARKERS = re.compile(
    r"ssh|tailscale|wsl|127\.0\.0\.1:5173|/home/|haroon|4090|16 ?GB|LightX|W4A8|gaia|server-maintenance|view-production|connection\.md",
    re.IGNORECASE,
)


def _script():
    spec = importlib.util.spec_from_file_location("build_downloads", ROOT / "scripts/build_downloads.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, dict]:
    destination = tmp_path_factory.mktemp("downloads")
    return destination, _script().build_downloads(ROOT, destination)


def _file(built, suffix: str) -> Path:
    destination, manifest = built
    (name,) = [item["name"] for item in manifest["files"] if item["name"].endswith(suffix)]
    return destination / name


def test_a_rebuild_gives_the_same_bytes_and_checksums(built, tmp_path) -> None:
    _, first = built
    again = _script().build_downloads(ROOT, tmp_path)
    assert again == first
    for item in first["files"]:
        assert hashlib.sha256((tmp_path / item["name"]).read_bytes()).hexdigest() == item["sha256"]


def test_the_wheel_backend_is_pinned_exactly(built) -> None:
    """The backend writes its version into the wheel, so a floating requirement would
    change the published bytes and checksum whenever it released."""
    requires = tomllib.loads((ROOT / "mcp/pyproject.toml").read_text())["build-system"]["requires"]
    assert requires and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*", requirement) for requirement in requires), requires
    with zipfile.ZipFile(_file(built, ".whl")) as archive:
        generator = archive.read(f"{INFO}/WHEEL").decode()
    pinned = requires[0].split("==")[1]
    assert f"Generator: setuptools ({pinned})" in generator


def test_the_wheel_holds_only_the_server_with_the_public_defaults(built) -> None:
    wheel = _file(built, ".whl")
    assert wheel.name == WHEEL
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(name.startswith(("magic_video_studio_mcp/", f"{INFO}/")) for name in names), names
        assert not [name for name in names if "test" in name or name.endswith(("OPERATORS.md", ".pyc"))]
        client = archive.read("magic_video_studio_mcp/client.py").decode()
        assert 'DEFAULT_BASE_URL = "https://api.magicstudios.ai"' in client
        entry_points = archive.read(f"{INFO}/entry_points.txt").decode()
        assert "magic-studios-mcp = magic_video_studio_mcp.server:main" in entry_points
        assert "magic-video-studio-mcp = magic_video_studio_mcp.server:main" in entry_points
        metadata = archive.read(f"{INFO}/METADATA").decode()
        assert "Name: magic-studios-mcp" in metadata and f"Version: {VERSION}" in metadata and "License-Expression: MIT" in metadata
        assert archive.read(f"{INFO}/licenses/LICENSE").decode().startswith("MIT License\n")
        for name in names:
            text = archive.read(name).decode(errors="replace")
            assert not OPERATOR_MARKERS.search(text.replace("Claude Desktop", "")), name


def test_the_skill_zip_holds_only_the_public_skill(built) -> None:
    skill = _file(built, ".zip")
    assert skill.name == f"magic-studios-skill-{VERSION}.zip"
    with zipfile.ZipFile(skill) as archive:
        assert archive.namelist() == [
            "magic-studios/SKILL.md",
            "magic-studios/agents/openai.yaml",
            "magic-studios/references/workflow.md",
        ]
        for name in archive.namelist():
            assert not OPERATOR_MARKERS.search(archive.read(name).decode()), name
        assert archive.read("magic-studios/SKILL.md").decode().startswith("---\nname: magic-studios\n")


def test_only_the_manifest_files_are_served_with_their_checksums(built) -> None:
    destination, manifest = built
    application = FastAPI()
    application.include_router(create_download_router(destination))
    client = TestClient(application)
    listed = client.get("/studio/downloads").json()["files"]
    assert [item["name"] for item in listed] == [item["name"] for item in manifest["files"]]
    for item in listed:
        served = client.get(item["url"])
        assert served.status_code == 200
        assert hashlib.sha256(served.content).hexdigest() == item["sha256"]
        assert served.headers["content-disposition"] == f'attachment; filename="{item["name"]}"'
        # Short-lived, and labelled with the checksum the list reports.
        assert served.headers["cache-control"] == "public, max-age=300"
        assert served.headers["etag"] == f'"{item["sha256"]}"'
    assert client.get("/studio/downloads/manifest.json").status_code == 404
    assert client.get("/studio/downloads/..%2Fpublic_downloads.py").status_code == 404


def test_nothing_is_listed_before_the_downloads_are_built(tmp_path) -> None:
    application = FastAPI()
    application.include_router(create_download_router(tmp_path))
    assert TestClient(application).get("/studio/downloads").json() == {"files": []}


@pytest.mark.parametrize("document", [
    "web/src/features/guide/DownloadChecksums.tsx",
    "mcp/README.md",
    "src/magic_video_editor/api.md",
])
def test_the_docs_name_the_files_the_build_makes(built, document) -> None:
    _, manifest = built
    text = (ROOT / document).read_text()
    for item in manifest["files"]:
        assert f"https://magicstudios.ai/studio/downloads/{item['name']}" in text, (document, item["name"])
    assert not re.findall(r"/studio/downloads/(?!" + "|".join(re.escape(item["name"]) for item in manifest["files"]) + r")[\w.-]+\.(?:whl|zip)", text)


def test_the_operator_skill_is_not_what_ships(built) -> None:
    """The internal skill stays in the repository, unchanged and unpublished."""
    with zipfile.ZipFile(_file(built, ".zip")) as archive:
        shipped = archive.read("magic-studios/SKILL.md")
    assert shipped != (ROOT / ".agents/skills/magic-studios/SKILL.md").read_bytes()
    assert json.loads((built[0] / "manifest.json").read_text()) == built[1]
