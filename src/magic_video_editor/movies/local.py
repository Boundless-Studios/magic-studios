"""Deterministic local-only pool fixtures; never contacts a GPU/provider."""

import asyncio
import tempfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from outbound_gpu_worker_pool.contracts import JobSubmission

from magic_video_editor.contracts import AssetStore
from magic_video_editor.movies.media import _run


class LocalMoviePool:
    def __init__(self, assets: AssetStore) -> None:
        self.assets = assets
        self.jobs = {}
        self.lock = asyncio.Lock()

    async def submit(self, request: JobSubmission) -> SimpleNamespace:
        async with self.lock:
            if request.job_id in self.jobs:
                return self.jobs[request.job_id]
            images = request.payload.get("images") or {}
            if "last_frame" in images:
                first = await self.assets.read(images["first_frame"])
                last = await self.assets.read(images["last_frame"])
                content = await asyncio.to_thread(_crossfade, request.payload, first, last)
            else:
                content = await asyncio.to_thread(_video, request.payload)
            await self.assets.write_once(request.output_key, content, "video/mp4")
            self.jobs[request.job_id] = SimpleNamespace(
                status="queued", output_key=request.output_key, polls=0
            )
            return self.jobs[request.job_id]

    async def get(self, id: str) -> SimpleNamespace | None:
        job = self.jobs.get(id)
        if job and job.status != "cancelled":
            job.polls += 1
            job.status = "completed" if job.polls > 1 else "processing"
        return job

    async def cancel(self, id: str) -> bool:
        if id in self.jobs:
            self.jobs[id].status = "cancelled"
            return True
        return False


def _video(payload: dict[str, Any]) -> bytes:
    color = sha256(payload["prompt"].encode()).hexdigest()[:6]
    with tempfile.TemporaryDirectory(prefix="movie-fixture-") as tmp:
        out = Path(tmp) / "take.mp4"
        _run(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x{color}:s=768x432:r=16:d={payload['length'] / payload['fps']}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ]
        )
        return out.read_bytes()


def _crossfade(payload: dict[str, Any], first: bytes, last: bytes) -> bytes:
    """A frame-pinned fixture: starts on the first image, ends on the last."""
    seconds = payload["length"] / payload["fps"]
    size = f"{payload['width']}x{payload['height']}"
    with tempfile.TemporaryDirectory(prefix="flight-fixture-") as tmp:
        root = Path(tmp)
        (root / "first.png").write_bytes(first)
        (root / "last.png").write_bytes(last)
        out = root / "take.mp4"
        _run(
            [
                "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(root / "first.png"),
                "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(root / "last.png"),
                "-filter_complex",
                f"[0][1]xfade=transition=fade:duration={seconds * 0.9:.3f}:offset=0,"
                f"scale={size},fps={payload['fps']},format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "ultrafast", str(out),
            ]
        )
        return out.read_bytes()
