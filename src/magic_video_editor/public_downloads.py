"""The public downloads: the MCP server's wheel and the agent skill, with their SHA-256.

`scripts/build_downloads.py` builds them into `download_files/` before the application
is packaged. Only the files its manifest names are served. The build is pinned and
reproducible, but a cached copy is kept only briefly and carries the file's SHA-256 as
its ETag, so it can never disagree with the list for long.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

DOWNLOADS = Path(__file__).parent / "download_files"
MEDIA_TYPES = {".whl": "application/zip", ".zip": "application/zip"}


def create_download_router(directory: Path = DOWNLOADS) -> APIRouter:
    router = APIRouter(prefix="/studio/downloads")

    def manifest() -> list[dict[str, Any]]:
        path = directory / "manifest.json"
        return json.loads(path.read_text())["files"] if path.is_file() else []

    @router.get("")
    async def list_downloads() -> dict[str, Any]:
        return {"files": [{**item, "url": f"/studio/downloads/{item['name']}"} for item in manifest()]}

    @router.get("/{name}")
    async def download(name: str) -> FileResponse:
        item = next((item for item in manifest() if item["name"] == name), None)
        if item is None:
            raise HTTPException(status_code=404, detail="download not found")
        return FileResponse(
            directory / name,
            media_type=MEDIA_TYPES[Path(name).suffix],
            filename=name,
            headers={"Cache-Control": "public, max-age=300", "ETag": f'"{item["sha256"]}"'},
        )

    return router
