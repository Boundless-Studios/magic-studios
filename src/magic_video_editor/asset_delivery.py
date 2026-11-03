"""Stream a stored object to the caller who was just authorized to read it.

Files are served through the API rather than by handing out signed storage URLs:
a signed URL works for whoever holds it, while this response exists only for the
authenticated request that asked for it, so a copied link is useless to anyone
else. Browser range requests (video seeking) are answered with 206 responses.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from magic_video_editor.contracts import AssetNotFound, AssetStore

CHUNK_BYTES = 8 * 1024 * 1024
# Owned objects are immutable, so the owner's browser may keep them; shared
# caches may not.
CACHE_CONTROL = "private, max-age=3600"
# Types a browser may render inline. Anything else is a download, so stored
# bytes can never run as a page on the Studio origin.
INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
    }
)
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_UNSAFE_FILENAME = re.compile(r'[^A-Za-z0-9._ -]')


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The single range a request asks for, or None for the whole object.
    Raises 416 when the range lies outside the object."""
    if not header or "," in header:
        return None
    match = _RANGE.match(header.strip())
    if match is None or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first == "":
        length = int(last)
        if length == 0:
            raise _unsatisfiable(size)
        start, end = max(size - length, 0), size - 1
    else:
        start = int(first)
        if start >= size:
            raise _unsatisfiable(size)
        if last and int(last) < start:
            return None
        end = min(int(last), size - 1) if last else size - 1
    return start, end


def _unsatisfiable(size: int) -> HTTPException:
    return HTTPException(
        status_code=416, detail="range not satisfiable", headers={"Content-Range": f"bytes */{size}"}
    )


def _disposition(filename: str | None) -> str:
    if filename is None:
        return "inline"
    safe = _UNSAFE_FILENAME.sub("_", filename).strip() or "download"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def asset_response(
    assets: AssetStore, key: str, request: Request, *, filename: str | None = None
) -> Response:
    """Call only after the caller is authorized for exactly this key."""
    descriptor = await assets.describe(key)
    if descriptor is None:
        raise HTTPException(status_code=404, detail="file not found")
    size = descriptor.size
    content_type = (descriptor.content_type or "").split(";")[0].strip().lower()
    if content_type not in INLINE_TYPES:
        content_type = "application/zip" if key.endswith(".zip") else "application/octet-stream"
        filename = filename or key.rsplit("/", 1)[-1]
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": CACHE_CONTROL,
        "Content-Disposition": _disposition(filename),
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    if size == 0:
        return Response(b"", media_type=content_type, headers=headers)
    selected = _byte_range(request.headers.get("range"), size)
    start, end = selected or (0, size - 1)
    headers["Content-Length"] = str(end - start + 1)
    if selected is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    # Read the first chunk before answering, so a missing object is a 404
    # rather than a broken 200.
    first_end = min(start + CHUNK_BYTES - 1, end)
    try:
        first = await assets.read_range(key, start, first_end)
    except AssetNotFound as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc

    async def body() -> AsyncIterator[bytes]:
        yield first
        position = first_end + 1
        while position <= end:
            chunk_end = min(position + CHUNK_BYTES - 1, end)
            yield await assets.read_range(key, position, chunk_end)
            position = chunk_end + 1

    return StreamingResponse(
        body(), status_code=206 if selected else 200, media_type=content_type, headers=headers
    )
