"""Durable CPU processing using the same signed transfers and fenced pool jobs."""

import json
from hashlib import sha256
from uuid import UUID, uuid5

from outbound_gpu_worker_pool.contracts import JobSubmission
from magic_video_editor.cpu_worker import CPU_CAPABILITY
from magic_video_editor.movies.media import MovieMedia


class CpuPending(Exception):
    """Intent is durable; reconciliation should revisit without blocking HTTP."""


class PoolMovieMedia(MovieMedia):
    def __init__(self, assets, pool, tenant_id=None, scope="", access=None, local_fallback=True):
        super().__init__(assets)
        self.pool, self.tenant_id, self.scope = pool, tenant_id, scope
        self.access = access
        # False for public accounts: their processing never runs on the service.
        self.local_fallback = local_fallback

    # Methods with no pool-backed path run only on the service; scoped media
    # without a fallback refuses them rather than using service compute.
    def _require_local(self):
        if not self.local_fallback:
            raise ValueError("This step needs the service's FFmpeg, which public accounts cannot use.")

    async def duration(self, content):
        self._require_local()
        return await super().duration(content)

    async def audio_duration(self, content):
        self._require_local()
        return await super().audio_duration(content)

    async def extract_audio(self, content):
        self._require_local()
        return await super().extract_audio(content)

    async def join_report(self, key, times):
        self._require_local()
        return await super().join_report(key, times)

    def scoped(self, tenant_id, scope, local_fallback=True):
        media = PoolMovieMedia(self.assets, self.pool, tenant_id, scope, self.access, local_fallback)
        media.slots = self.slots
        return media

    async def _process(self, payload, key):
        identity = sha256(
            json.dumps(
                [self.tenant_id, self.scope, key, payload], sort_keys=True
            ).encode()
        ).hexdigest()
        job_id = str(uuid5(UUID("139c1b3a-655b-4c09-a0a9-4ae90447720f"), identity))
        record = await self.pool.get(job_id)
        if record is None:
            if await self.assets.describe(key):
                return True
            # A machine advertises this capability only when FFmpeg is installed.
            # Preserve service processing when no eligible CPU executor is online.
            workers = await self.pool.worker_views()
            eligible = []
            for w in workers:
                if (
                    str(w.status) not in {"online", "busy"}
                    or CPU_CAPABILITY not in w.capability_ids
                ):
                    continue
                allowed = w.tenant_id == self.tenant_id
                if self.access is not None and self.tenant_id in self.access.shared_tenants:
                    allowed = await self.access.serves_shared(w.worker_id, w.tenant_id)
                if allowed:
                    eligible.append(w)
            if not eligible:
                if not self.local_fallback:
                    raise ValueError(
                        "Connect your own machine with FFmpeg (online) to process this movie."
                    )
                return False
            keys = tuple(
                dict.fromkeys(
                    s["key"] for s in payload["sources"] + payload.get("audio", [])
                )
            )
            await self.pool.submit(
                JobSubmission(
                    job_id=job_id,
                    idempotency_key="movie-cpu:" + identity,
                    capability_id=CPU_CAPABILITY,
                    input_keys=keys,
                    output_key=key,
                    payload=payload,
                    tenant_id=self.tenant_id,
                    execution_deadline_seconds=600,
                )
            )
            raise CpuPending()
        status = str(record.status)
        if status == "completed":
            return True
        if status in {"failed", "cancelled"}:
            raise ValueError(
                getattr(record, "failure_message", None) or "CPU processing failed"
            )
        raise CpuPending()

    async def final_frame(self, key):
        target = key + ".last.png"
        if await self._process(
            {"operation": "frame", "sources": [{"key": key}]}, target
        ):
            return target
        return await super().final_frame(key)

    async def duration_for_key(self, key):
        target = key + ".duration.json"
        if await self._process(
            {"operation": "probe", "sources": [{"key": key}]}, target
        ):
            import math

            value = json.loads(await self.assets.read_limited(target, 4096))[
                "duration_seconds"
            ]
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not 0.05 <= value <= 600
            ):
                raise ValueError("Invalid CPU duration result")
            return value
        return await super().duration_for_key(key)

    async def assemble(self, sources, key, soundtracks=None):
        payload = {
            "operation": "assemble",
            "sources": [
                {
                    "key": source,
                    "in_seconds": clip.in_seconds,
                    "out_seconds": clip.out_seconds,
                    **(
                        {"transition_seconds": clip.transition_seconds}
                        if clip.transition_seconds
                        else {}
                    ),
                    **({"mute": True} if getattr(clip, "mute", False) else {}),
                }
                for source, clip in sources
            ],
            "audio": [
                {
                    "key": t.asset_key,
                    "offset_seconds": t.offset_seconds,
                    "trim_start_seconds": t.trim_start_seconds,
                    "gain_db": t.gain_db,
                    # Shape fields ride along only when set, so an unshaped
                    # cue keeps its historical job identity.
                    **{
                        name: getattr(t, name)
                        for name in (
                            "end_seconds",
                            "fade_in_seconds",
                            "fade_out_seconds",
                            "swell_db",
                            "swell_seconds",
                        )
                        if getattr(t, name, None)
                    },
                }
                for t in soundtracks or []
            ],
        }
        if (
            len(sources) <= 16
            and len(payload["audio"]) <= 8
            and len(set(s["key"] for s in payload["sources"] + payload["audio"])) <= 16
            and await self._process(payload, key)
        ):
            return key
        return await super().assemble(sources, key, soundtracks)
