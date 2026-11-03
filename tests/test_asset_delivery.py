"""Owner-authorized streaming: storage bytes never run as a page on our origin."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.memory import MemoryAssetStore


def _client(assets: MemoryAssetStore, filename: str | None = None) -> TestClient:
    app = FastAPI()

    @app.get("/file/{key:path}")
    async def file(key: str, request: Request):
        return await asset_response(assets, key, request, filename=filename)

    return TestClient(app)


def test_unrenderable_types_are_always_downloads_and_never_sniffed() -> None:
    assets = MemoryAssetStore()
    asyncio.run(assets.write_once("k/page.html", b"<script>alert(1)</script>", "text/html"))
    asyncio.run(assets.write_once("k/export.zip", b"PK", "application/zip"))

    page = _client(assets).get("/file/k/page.html")
    export = _client(assets).get("/file/k/export.zip")

    assert page.headers["content-type"] == "application/octet-stream"
    assert page.headers["content-disposition"].startswith('attachment; filename="page.html"')
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in page.headers["content-security-policy"]
    assert export.headers["content-type"] == "application/zip"


def test_download_names_cannot_inject_headers() -> None:
    assets = MemoryAssetStore()
    asyncio.run(assets.write_once("k/a.png", b"png", "image/png"))

    response = _client(assets, filename='evil"\r\nX-Injected: 1.png').get("/file/k/a.png")

    assert "x-injected" not in response.headers
    assert response.headers["content-disposition"].startswith('attachment; filename="evil___X-Injected_ 1.png"')


def test_a_missing_file_is_a_404() -> None:
    assert _client(MemoryAssetStore()).get("/file/k/missing.png").status_code == 404
