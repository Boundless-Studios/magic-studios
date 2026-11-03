"""Freesound search and preview download, filtered to the licences a set allows.

Ported from CINDER's `ArtSource/scripts/fetch_freesound.py`: the licence is
filtered in the query and checked again on every result, so a sound whose
licence the API reports as anything but CC0 (or CC-BY, where the set allows it)
never reaches a candidate. NC and Sampling+ are never offered.

An app token covers search and the high-quality MP3 preview; originals need
each user's Freesound OAuth, which phase 1 does not do. The token is the
service's (`MAGIC_VIDEO_EDITOR_FREESOUND_API_KEY`), sent only in the
Authorization header and never logged or returned. Freesound limits a token to
about 60 requests a minute and 2,000 a day, so each account gets a bounded share
and identical searches are answered from a short cache.
"""

from __future__ import annotations

import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from magic_video_editor.audio.models import LicencePolicy

API = "https://freesound.org/apiv2"
KEY_VARIABLE = "MAGIC_VIDEO_EDITOR_FREESOUND_API_KEY"
PREVIEW_FIELD = "preview-hq-mp3"
PREVIEW_HOST = "cdn.freesound.org"
MAX_PREVIEW_BYTES = 50 * 1024 * 1024
FIELDS = "id,name,username,license,duration,previews,url,tags"
PAGE_SIZE = 15
# Licence URLs as Freesound reports them, by path; anything else is refused.
LICENCES: dict[str, tuple[str, bool]] = {
    "/publicdomain/zero/1.0/": ("CC0-1.0", False),
    "/licenses/by/4.0/": ("CC-BY-4.0", True),
    "/licenses/by/3.0/": ("CC-BY-3.0", True),
}
QUERY_FILTER: dict[LicencePolicy, str] = {
    "cc0": 'license:"Creative Commons 0"',
    "cc0_cc_by": 'license:("Creative Commons 0" OR "Attribution")',
}
# Searches and imports share one budget per account.
SEARCHES_PER_MINUTE = 20
SEARCHES_PER_DAY = 200
CACHE_SECONDS = 600
CACHE_ENTRIES = 256


class FreesoundUnavailable(Exception):
    """No token configured, or Freesound could not answer."""


class FreesoundRateLimited(Exception):
    pass


class FreesoundRefused(Exception):
    """The sound is missing or its licence is not one the set allows."""


class FreeSound(BaseModel):
    source_id: str
    name: str
    author: str
    spdx: str
    licence_url: str
    attribution_required: bool
    duration_seconds: float
    preview_url: str
    source_url: str
    tags: list[str]


def licence_for(url: str, policy: LicencePolicy) -> tuple[str, bool] | None:
    """The SPDX id and attribution duty of a licence the policy allows, else None."""
    parts = urlsplit(url or "")
    if parts.hostname not in {"creativecommons.org", "www.creativecommons.org"}:
        return None
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    found = LICENCES.get(path)
    if found is None or (found[1] and policy != "cc0_cc_by"):
        return None
    return found


def _preview_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != PREVIEW_HOST or not parts.path.endswith(".mp3"):
        return None
    return value


def parse_sound(item: Mapping[str, Any], policy: LicencePolicy) -> FreeSound | None:
    """A search or sound result as a candidate source, or None when it must not be used."""
    try:
        licence = licence_for(str(item["license"]), policy)
        preview = _preview_url((item.get("previews") or {}).get(PREVIEW_FIELD))
        duration = float(item["duration"])
        if licence is None or preview is None or not 0 < duration <= 3600:
            return None
        return FreeSound(
            source_id=str(int(item["id"])),
            name=str(item.get("name") or "")[:200],
            author=str(item.get("username") or "")[:120],
            spdx=licence[0],
            licence_url=str(item["license"]),
            attribution_required=licence[1],
            duration_seconds=round(duration, 3),
            preview_url=preview,
            source_url=str(item.get("url") or f"https://freesound.org/s/{int(item['id'])}/"),
            tags=[str(t)[:40] for t in (item.get("tags") or [])[:12]],
        )
    except (KeyError, TypeError, ValueError):
        return None


class FreesoundClient:
    def __init__(
        self,
        token: str | None,
        *,
        api: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        self._token = (token or "").strip() or None
        self.api = api.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._cache: OrderedDict[tuple, tuple[float, list[FreeSound]]] = OrderedDict()
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    @classmethod
    def from_env(cls, values: Mapping[str, str]) -> FreesoundClient:
        return cls(values.get(KEY_VARIABLE))

    @property
    def available(self) -> bool:
        return self._token is not None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=20, follow_redirects=False
        )

    def _admit(self, owner: str) -> None:
        """At most SEARCHES_PER_MINUTE and SEARCHES_PER_DAY per account, per instance."""
        now = self._clock()
        calls = self._calls[owner]
        while calls and now - calls[0] > 86400:
            calls.popleft()
        recent = sum(1 for t in calls if now - t <= 60)
        if len(calls) >= SEARCHES_PER_DAY or recent >= SEARCHES_PER_MINUTE:
            raise FreesoundRateLimited("Too many free-sound requests; wait a little and retry.")
        calls.append(now)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self._token is None:
            raise FreesoundUnavailable("Free-sound search is not configured.")
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{self.api}{path}",
                    params=params,
                    headers={"Authorization": f"Token {self._token}"},
                )
        except httpx.HTTPError as exc:
            raise FreesoundUnavailable("Freesound did not answer.") from exc
        if response.status_code == 404:
            raise FreesoundRefused("That sound is not on Freesound.")
        if response.status_code == 429:
            raise FreesoundRateLimited("Freesound is busy; retry in a minute.")
        if response.status_code != 200:
            raise FreesoundUnavailable(f"Freesound answered {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise FreesoundUnavailable("Freesound sent an unreadable answer.") from exc

    async def search(
        self,
        owner: str,
        query: str,
        policy: LicencePolicy,
        *,
        min_seconds: float = 0,
        max_seconds: float = 120,
        page: int = 1,
        sort: Literal["rating_desc", "score", "downloads_desc"] = "rating_desc",
    ) -> list[FreeSound]:
        filters = f"{QUERY_FILTER[policy]} duration:[{min_seconds:g} TO {max_seconds:g}]"
        key = (query.strip().lower(), filters, page, sort)
        now = self._clock()
        cached = self._cache.get(key)
        if cached and now - cached[0] <= CACHE_SECONDS:
            self._cache.move_to_end(key)
            return cached[1]
        if self._token is None:
            raise FreesoundUnavailable("Free-sound search is not configured.")
        self._admit(owner)
        data = await self._get(
            "/search/text/",
            {
                "query": query,
                "filter": filters,
                "fields": FIELDS,
                "page_size": PAGE_SIZE,
                "page": page,
                "sort": sort,
            },
        )
        results = [
            sound
            for item in (data.get("results") or [] if isinstance(data, dict) else [])
            if isinstance(item, dict) and (sound := parse_sound(item, policy)) is not None
        ]
        self._cache[key] = (now, results)
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return results

    async def sound(self, owner: str, source_id: str, policy: LicencePolicy) -> FreeSound:
        """The sound as Freesound reports it now; its licence is checked again here.
        It spends the account's request budget, like a search."""
        if not source_id.isdigit() or len(source_id) > 12:
            raise FreesoundRefused("Give a Freesound sound number.")
        if self._token is None:
            raise FreesoundUnavailable("Free-sound search is not configured.")
        self._admit(owner)
        data = await self._get(f"/sounds/{int(source_id)}/", {"fields": FIELDS})
        sound = parse_sound(data, policy) if isinstance(data, dict) else None
        if sound is None:
            raise FreesoundRefused(
                "That sound's licence is not one this set allows."
                if policy == "cc0"
                else "That sound's licence is not CC0 or CC-BY."
            )
        return sound

    async def download_preview(self, sound: FreeSound) -> bytes:
        url = _preview_url(sound.preview_url)
        if url is None:
            raise FreesoundRefused("That preview is not on Freesound's CDN.")
        try:
            async with self._client() as client, client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise FreesoundUnavailable(f"Freesound's CDN answered {response.status_code}.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_PREVIEW_BYTES:
                        raise FreesoundRefused("That preview is larger than 50 MB.")
        except httpx.HTTPError as exc:
            raise FreesoundUnavailable("Freesound's CDN did not answer.") from exc
        return bytes(content)
