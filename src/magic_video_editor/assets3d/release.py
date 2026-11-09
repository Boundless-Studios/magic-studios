"""The release pack: the selected model, the source pictures, a manifest of the whole
candidate chain, and generated credits. Built once and stored create-once, so it
never changes.

Layout:
  model/<asset>.glb       the selected `clean` candidate, else the selected `mesh`
  sources/<role>.<ext>    the source pictures, one per role
  manifest.json           the chain with hashes, provenance, gates and reviews
  CREDITS.md              what made each file, generated from the manifest

Rig, clip, export and render files join the pack when those stages exist.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from hashlib import sha256
from typing import Any

from magic_video_editor.assets3d.models import ROLES, Asset3D, Asset3DCandidate, Stage

SCHEMA_VERSION = 1
# The stages a pack ships today, in chain order. The model is the last one selected.
RELEASED_STAGES: tuple[Stage, ...] = ("mesh", "clean")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Fixed zip timestamps keep the bytes a function of the content.
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
# Already-compressed files are stored as they are: deflating a 200 MB GLB of
# embedded textures costs seconds and saves little.
STORED_SUFFIXES = (".glb", ".png", ".jpg", ".webp")
OWN_FILE = "the owner's own file"


class PackTooLarge(ValueError):
    """Every entry together, manifest and credits included, exceeds the limit."""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "asset"


def chain(asset: Asset3D) -> list[Asset3DCandidate]:
    """The selected candidates a pack ships, in stage order."""
    by_id = {c.candidate_id: c for c in asset.candidates}
    return [
        by_id[asset.selections[stage]]
        for stage in RELEASED_STAGES
        if asset.selections.get(stage) in by_id
    ]


def _review(candidate: Asset3DCandidate) -> dict[str, Any] | None:
    review = candidate.review
    return None if review is None else {
        "outcome": review.outcome,
        "note": review.note,
        "sha256": review.sha256,
        "reviewer": "person" if review.actor_kind == "session" else "api key (agent)",
        "at": review.at,
    }


def _credit(entry: dict[str, Any]) -> str:
    """One line of CREDITS.md for a chain entry or a source.

    Uploads are the owner's own files, whose rights are theirs to vouch for. Engine
    output names every model that made it, with its licence, from the provenance
    the worker attested (`models: [{id, licence}]`); that arrives with the engine
    stages, and a missing licence is said rather than guessed."""
    if entry.get("origin") != "engine":
        return OWN_FILE
    provenance = entry.get("provenance") or {}
    models = [
        f"{m.get('id', 'unnamed model')} ({m.get('licence') or 'licence not recorded'})"
        for m in provenance.get("models", [])
        if isinstance(m, dict)
    ]
    made_by = provenance.get("capability_id") or "the 3D engine"
    return f"made by {made_by}" + (f" with {', '.join(models)}" if models else "")


def build_pack(
    asset: Asset3D,
    release_id: str,
    name: str,
    created_at: str,
    files: dict[str, bytes],
    max_bytes: int | None = None,
) -> tuple[bytes, dict[str, Any], str]:
    """The zip, its manifest and the manifest's SHA-256. `files` maps every asset key
    the pack ships (the model and the sources) to its bytes."""
    selected = chain(asset)
    model = selected[-1]
    blobs: list[tuple[str, bytes]] = []
    entries: list[dict[str, Any]] = []

    def add(path: str, content: bytes) -> None:
        blobs.append((path, content))
        entries.append({"path": path, "sha256": sha256(content).hexdigest(), "bytes": len(content)})

    model_path = f"model/{slug(asset.name)}.glb"
    add(model_path, files[model.asset_key])
    sources = []
    for role in ROLES:
        source = next((s for s in asset.sources if s.role == role), None)
        if source is None:
            continue
        path = f"sources/{role}.{EXTENSIONS[source.media_type]}"
        add(path, files[source.asset_key])
        sources.append({
            "role": role,
            "path": path,
            "source_id": source.source_id,
            "sha256": source.sha256,
            "origin": source.origin,
            "provenance": source.provenance,
        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset.asset_id,
        "name": asset.name,
        "kind": asset.kind,
        "release_id": release_id,
        "release_name": name,
        "created_at": created_at,
        "source_revision": asset.revision,
        "model": {"path": model_path, "stage": model.stage, "candidate_id": model.candidate_id},
        "chain": [
            {
                "stage": c.stage,
                "candidate_id": c.candidate_id,
                "parent_id": c.parent_id,
                "origin": c.origin,
                "sha256": c.sha256,
                "bytes": c.byte_length,
                "measured": c.measured.model_dump() if c.measured else None,
                "provenance": c.provenance,
                "gate": c.gate.model_dump() if c.gate else None,
                "review": _review(c),
            }
            for c in selected
        ],
        "sources": sources,
        "files": entries,
    }
    credits = [
        f"# Credits: {asset.name}, {name}",
        "",
        "## Model",
        "",
        *(
            f"- {entry['stage']} `{entry['candidate_id']}`"
            f"{' (shipped as `' + model_path + '`)' if entry['candidate_id'] == model.candidate_id else ''}: "
            f"{_credit(entry)}"
            for entry in manifest["chain"]
        ),
        "",
        "## Source pictures",
        "",
        *([f"- `{s['path']}`: {_credit(s)}" for s in sources] or ["- none"]),
        "",
        "Uploads are the owner's own files; their rights are the owner's to vouch for. "
        "Every file's hash and history is in manifest.json.",
        "",
    ]
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    everything = [("manifest.json", manifest_bytes), ("CREDITS.md", "\n".join(credits).encode()), *blobs]
    if max_bytes is not None and sum(len(content) for _, content in everything) > max_bytes:
        raise PackTooLarge(max_bytes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in everything:
            info = zipfile.ZipInfo(path, ZIP_TIME)
            info.compress_type = (
                zipfile.ZIP_STORED if path.endswith(STORED_SUFFIXES) else zipfile.ZIP_DEFLATED
            )
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue(), manifest, sha256(manifest_bytes).hexdigest()
