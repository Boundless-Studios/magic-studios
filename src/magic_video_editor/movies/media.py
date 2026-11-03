"""Bounded CPU processing of immutable movie artifacts using FFmpeg."""

import asyncio
from typing import TYPE_CHECKING

from magic_video_editor.contracts import AssetStore
from magic_video_editor.movies.soundtracks import MAX_AUDIO_BYTES, AudioTrack

if TYPE_CHECKING:
    from magic_video_editor.movies.models import Clip

from magic_video_editor.cpu_worker import (
    MAX_VIDEO_BYTES,
    MAX_CUT_BYTES,
    _duration,
    _audio_duration,
    _extract_audio,
    _join_diffs,
    _last_frame,
    _assemble,
    _run,
)


class MovieMedia:
    def __init__(self, assets: AssetStore) -> None:
        self.assets = assets
        self.slots = asyncio.Semaphore(2)

    async def duration(self, content: bytes) -> float:
        async with self.slots:
            return await asyncio.to_thread(_duration, content)

    async def duration_for_key(self, key: str) -> float:
        return await self.duration(await self.assets.read_limited(key, MAX_VIDEO_BYTES))

    async def audio_duration(self, content: bytes) -> float:
        async with self.slots:
            return await asyncio.to_thread(_audio_duration, content)

    async def join_report(self, key: str, times: list[float]) -> list[float]:
        content = await self.assets.read_limited(key, MAX_CUT_BYTES)
        async with self.slots:
            return await asyncio.to_thread(_join_diffs, content, times)

    async def extract_audio(self, content: bytes) -> bytes:
        async with self.slots:
            return await asyncio.to_thread(_extract_audio, content)

    async def final_frame(self, key: str) -> str:
        target = key + ".last.png"
        if await self.assets.describe(target):
            return target
        content = await self.assets.read_limited(key, MAX_VIDEO_BYTES)
        async with self.slots:
            result = await asyncio.to_thread(_last_frame, content)
        await self.assets.write_once(target, result, "image/png")
        return target

    async def assemble(
        self,
        sources: list[tuple[str, "Clip"]],
        key: str,
        soundtracks: list[AudioTrack] | None = None,
    ) -> str:
        if await self.assets.describe(key):
            return key
        loaded = []
        total = 0
        for source, clip in sources:
            content = await self.assets.read_limited(source, MAX_VIDEO_BYTES)
            total += len(content)
            if total > MAX_CUT_BYTES:
                raise ValueError("Cut sources exceed 512 MiB")
            loaded.append((content, clip))
        audio = []
        for track in soundtracks or []:
            content = await self.assets.read_limited(track.asset_key, MAX_AUDIO_BYTES)
            total += len(content)
            if total > MAX_CUT_BYTES:
                raise ValueError("Cut sources exceed 512 MiB")
            audio.append((content, track))
        async with self.slots:
            result = await asyncio.to_thread(_assemble, loaded, audio)
        await self.assets.write_once(key, result, "video/mp4")
        return key
