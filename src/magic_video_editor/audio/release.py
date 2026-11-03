"""The release pack: selected audio, a manifest with every file's licence and history,
and generated credits. Built once and stored create-once, so it never changes.

Layout:
  audio/<cue>/<cue>_NN.wav      the loudness rendition, or the original when none
  originals/<cue>/<cue>_NN.<ext> the untouched original, beside a rendition
  manifest.json                 machine-readable: licence, author, source, hashes
  CREDITS.md                    required attributions first, then courtesy credits
"""

from __future__ import annotations

import io
import json
import zipfile
from hashlib import sha256
from typing import Any

from magic_video_editor.audio.models import AudioSet, Candidate

SCHEMA_VERSION = 1


class PackTooLarge(ValueError):
    """Every entry together, manifest and credits included, exceeds the limit."""


# Fixed zip timestamps keep the bytes a function of the content.
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
EXTENSIONS = {"audio/mpeg": "mp3", "audio/wav": "wav"}
PROCESSING = (
    "EBU R128 loudness to -16 LUFS, -1.5 dBTP target, limiter at 0.89, "
    "resampled to 44.1 kHz, 16-bit PCM WAV"
)


def _credit(candidate: Candidate) -> str:
    licence = candidate.licence
    if licence.source == "upload":
        return "your own upload"
    name = candidate.provenance.get("name") or f"Freesound #{licence.source_id}"
    variant = " (Freesound preview)" if licence.variant == "preview" else ""
    return (
        f'"{name}" by {licence.author or "unknown"}, {licence.source_url}, '
        f"{licence.spdx} ({licence.licence_url}){variant}"
    )


def build_pack(
    audio_set: AudioSet,
    release_id: str,
    name: str,
    created_at: str,
    files: dict[str, bytes],
    max_bytes: int | None = None,
) -> tuple[bytes, dict[str, Any], str]:
    """The zip, its manifest and the manifest's SHA-256. `files` maps every asset key the selection names
    (originals and renditions) to its bytes."""
    by_id = {c.candidate_id: c for c in audio_set.candidates}
    entries: list[dict[str, Any]] = []
    blobs: list[tuple[str, bytes]] = []
    required: list[str] = []
    courtesy: list[str] = []
    for cue in audio_set.cues:
        for index, candidate_id in enumerate(cue.selected, start=1):
            candidate = by_id[candidate_id]
            stem = f"{cue.cue_id}_{index:02d}"
            original = files[candidate.asset_key]
            ext = EXTENSIONS[candidate.media_type]
            if candidate.rendition_key:
                rendition = files[candidate.rendition_key]
                path = f"audio/{cue.cue_id}/{stem}.wav"
                original_path = f"originals/{cue.cue_id}/{stem}.{ext}"
                blobs += [(path, rendition), (original_path, original)]
                processing = PROCESSING
                shipped = rendition
            else:
                path = f"audio/{cue.cue_id}/{stem}.{ext}"
                original_path = None
                blobs.append((path, original))
                processing = "none: the original as retrieved or uploaded"
                shipped = original
            review = candidate.review
            entries.append({
                "path": path,
                "original_path": original_path,
                "cue_id": cue.cue_id,
                "cue_name": cue.name,
                "kind": cue.kind,
                "loop": cue.loop,
                "variant": index,
                "candidate_id": candidate.candidate_id,
                "sha256": sha256(shipped).hexdigest(),
                "bytes": len(shipped),
                "original_sha256": candidate.sha256,
                "duration_seconds": candidate.duration_seconds,
                "measured": candidate.measured.model_dump() if candidate.measured else None,
                "origin": candidate.origin,
                "licence": candidate.licence.model_dump(),
                "provenance": candidate.provenance,
                "processing": processing,
                "review": None if review is None else {
                    "outcome": review.outcome,
                    "note": review.note,
                    "reviewer": "person" if review.actor_kind == "session" else "api key (agent)",
                    "at": review.at,
                },
            })
            line = f"- `{path}`: {_credit(candidate)}"
            (required if candidate.licence.attribution_required else courtesy).append(line)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "set": {"set_id": audio_set.set_id, "name": audio_set.name},
        "release": {"release_id": release_id, "name": name, "created_at": created_at},
        "source_revision": audio_set.revision,
        "licence_policy": audio_set.licence_policy,
        "attribution_required": bool(required),
        "files": entries,
    }
    credits = [f"# Credits: {audio_set.name}, {name}", ""]
    if required:
        credits += [
            "## Attribution required",
            "",
            "These files are licensed CC-BY. Credit them as below wherever you use them.",
            "",
            *required,
            "",
        ]
    credits += [
        "## Other sources",
        "",
        "No attribution is required for these (CC0 or your own uploads); listed for your records.",
        "",
        *(courtesy or ["- none"]),
        "",
        "Free sounds were found through Freesound (https://freesound.org). Licences are as "
        "Freesound reported them when each sound was imported; see manifest.json.",
        "",
    ]
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    entries = [("manifest.json", manifest_bytes), ("CREDITS.md", "\n".join(credits).encode()), *blobs]
    if max_bytes is not None and sum(len(content) for _, content in entries) > max_bytes:
        raise PackTooLarge(max_bytes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries:
            info = zipfile.ZipInfo(path, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue(), manifest, sha256(manifest_bytes).hexdigest()
