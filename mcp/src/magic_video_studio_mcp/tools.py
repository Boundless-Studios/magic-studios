"""The studio tools, as plain async functions over `StudioClient`.

Kept separate from the MCP wiring in `server.py` so each one can be tested against
a mock transport without standing up a server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from magic_video_studio_mcp.client import (
    StudioApiError,
    StudioClient,
    _revision_from_conflict,
    recommended_motions,
)

DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 3.0


async def list_projects(client: StudioClient) -> list[dict[str, Any]]:
    projects = await client.get("/studio/projects")
    return [
        {
            "project_id": item["project_id"],
            "name": item["name"],
            "revision": item["revision"],
            "subject_state": item["subject_state"],
            "motion_count": item["motion_count"],
            "release_count": item["release_count"],
            "updated_at": item["updated_at"],
        }
        for item in projects
    ]


async def create_project(client: StudioClient, name: str) -> dict[str, Any]:
    project = await client.request("POST", "/studio/projects", json={"name": name})
    return _project_summary(project)


async def get_project(client: StudioClient, project_id: str) -> dict[str, Any]:
    return _project_summary(await client.get(f"/studio/projects/{project_id}"))


async def upload_reference(
    client: StudioClient, project_id: str, path: str
) -> dict[str, Any]:
    source = Path(path).expanduser()
    data = source.read_bytes()
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    target = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/subject/references/uploads",
        project_id=project_id,
        body={"content_type": content_type},
    )
    await client.put_file(target["upload_url"], data, content_type, target.get("upload_headers"))
    return {"key": target["key"], "bytes": len(data), "content_type": content_type}


async def attach_movie_voice(
    client: StudioClient, movie_id: str, character_id: str, path: str
) -> dict[str, Any]:
    """Upload and attach a local WAV/MP3; the server validates its 2–15s duration."""
    source = Path(path).expanduser()
    content_type = {".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(source.suffix.lower())
    if content_type is None:
        raise ValueError("Choose a WAV or MP3 voice clip.")
    with source.open("rb") as stream:
        data = stream.read(50 * 1024 * 1024 + 1)
    if not data or len(data) > 50 * 1024 * 1024:
        raise ValueError("Choose a nonempty voice clip under 50 MB.")
    base = f"/studio/movies/{quote(movie_id, safe='')}"
    movie = await client.get(base)
    if not any(c["character_id"] == character_id for c in movie["characters"]):
        raise ValueError("Movie character not found.")
    target = await client.request(
        "POST", f"{base}/uploads", json={"content_type": content_type}
    )
    await client.put_file(target["upload_url"], data, content_type, target.get("upload_headers"))
    # Use the grant's revision: concurrent edits must conflict, never be overwritten.
    movie = await client.request(
        "PUT", f"{base}/characters/{quote(character_id, safe='')}/voice",
        json={"expected_revision": target["revision"], "asset_key": target["asset_key"]},
    )
    character = next(c for c in movie["characters"] if c["character_id"] == character_id)
    return {
        "movie_id": movie_id, "character_id": character_id,
        "revision": movie["revision"], "voice_asset_key": character["voice_asset_key"],
        "voice_seconds": character["voice_seconds"],
    }


async def generate_subject(
    client: StudioClient,
    project_id: str,
    reference_keys: list[str],
    guidance: str | None = None,
) -> dict[str, Any]:
    operation = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/subject/generate",
        project_id=project_id,
        body={
            "reference_keys": list(reference_keys),
            "direction": guidance,
            "request_id": _new_studio_request_id("subject"),
        },
    )
    return _operation_summary(operation)


# Operation stages (`OperationStage`) that are still work in flight, and the ones
# the operations list keeps afterwards as recent history.
_ACTIVE_STAGES = frozenset({"queued", "generating", "awaiting_settlement"})
_FAILED_STAGES = frozenset({"failed", "cancelled"})


async def wait_for_operations(
    client: StudioClient,
    project_id: str,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    *,
    poll_interval_s: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Poll until nothing is in flight, reconciling finished work into the manifest.

    Reconcile runs after the queue drains as well as on the way, because an operation
    is not visible in the manifest until it is settled. The operations list also keeps
    up to ten recent failed or cancelled operations, so the wait ends when no stage is
    still active rather than when the list is empty, and reports those as `failed`.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        operations = await client.get(f"/studio/projects/{project_id}/operations")
        # Reconciliation polls the provider and advances queued/processing jobs.
        # Waiting for a terminal status before calling it deadlocks hosted jobs:
        # without a browser open, nothing else observes the worker completion.
        page = await client.request(
            "POST", f"/studio/projects/{project_id}/activity/reconcile"
        )
        operations = await client.get(f"/studio/projects/{project_id}/operations")
        in_flight = [item for item in operations if item["stage"] in _ACTIVE_STAGES]
        failed = [
            _operation_summary(item)
            for item in operations
            if item["stage"] in _FAILED_STAGES
        ]
        if not in_flight:
            return {
                "settled": True,
                "in_flight": 0,
                "failed": failed,
                "recent": [
                    _operation_summary(item) for item in page["operations"][:10]
                ],
            }
        if time.monotonic() >= deadline:
            return {
                "settled": False,
                "in_flight": len(in_flight),
                "failed": failed,
                "timed_out_after_s": timeout_s,
                "recent": [_operation_summary(item) for item in operations[:10]],
            }
        await asyncio.sleep(poll_interval_s)


async def approve_subject(client: StudioClient, project_id: str) -> dict[str, Any]:
    project = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/subject/approve",
        project_id=project_id,
    )
    return _project_summary(project)


async def add_recommended_motions(
    client: StudioClient, project_id: str
) -> dict[str, Any]:
    project = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/motions/batch",
        project_id=project_id,
        body={"motions": recommended_motions()},
    )
    return _project_summary(project)


SpriteLayout = Literal["unassigned", "side_view", "isometric"]
ViewId = Literal["left", "right", "N", "NE", "E", "SE", "S", "SW", "W", "NW"]
MotionPreset = Literal["idle", "walk", "run", "jump", "attack", "guard", "cast", "hurt", "death", "trip"]


class ViewReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ViewMotionInput(BaseModel):
    """A durable identity and concise action; the server owns view constraints."""
    model_config = ConfigDict(extra="forbid")
    motion_id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["preset", "custom"] = "preset"
    preset: MotionPreset | None = None
    action: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)
    semantic_slot: str | None = None
    loop_policy: Literal["loop", "once", "ping_pong"] = "loop"
    required: bool = True

    @model_validator(mode="after")
    def validate_kind(self) -> ViewMotionInput:
        if self.kind == "preset" and self.preset is None:
            raise ValueError("preset motions require a preset")
        if self.kind == "custom" and (self.preset is not None or not self.prompt or not self.prompt.strip()):
            raise ValueError("custom motions require a prompt and no preset")
        return self


class ViewMotionChanges(BaseModel):
    """Only explicitly supplied fields are edited; omitted definition fields survive."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Literal["preset", "custom"] | None = None
    preset: MotionPreset | None = None
    action: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)
    prompt_source: Literal["preset", "custom"] | None = None
    semantic_slot: str | None = None
    loop_policy: Literal["loop", "once", "ping_pong"] | None = None
    required: bool | None = None
    style: str | None = None
    script: str | None = Field(default=None, max_length=2000)


_MOTION_DEFINITION_FIELDS = ("motion_id", "name", "kind", "preset", "action", "prompt", "prompt_source",
                             "semantic_slot", "loop_policy", "required", "references", "style", "script", "view_id")
_VIEW_IDS = {"side_view": {"left", "right"}, "isometric": {"N", "NE", "E", "SE", "S", "SW", "W", "NW"},
             "unassigned": set()}


def _project_path(project_id: str) -> str:
    return f"/studio/projects/{quote(project_id, safe='')}"


def _validate_view(project: dict[str, Any], view_id: str, *, must_exist: bool = False) -> None:
    if view_id not in _VIEW_IDS.get(project["manifest"].get("sprite_layout", "unassigned"), set()):
        raise ValueError("view does not match the project's sprite layout")
    if must_exist and not any(v["view_id"] == view_id for v in project["manifest"].get("views", [])):
        raise ValueError("add the declared character view before adding motions")


async def configure_sprite_layout(client: StudioClient, project_id: str, sprite_layout: SpriteLayout) -> dict[str, Any]:
    if sprite_layout not in _VIEW_IDS:
        raise ValueError("unknown sprite layout")
    base = _project_path(project_id)
    project = await client.get(base)
    return _project_summary(await client.request("PUT", f"{base}/sprite-layout",
        json={"expected_revision": project["revision"], "sprite_layout": sprite_layout}))


async def set_character_view(client: StudioClient, project_id: str, view_id: ViewId,
                              name: str | None = None, reference: ViewReferenceInput | dict[str, Any] | None = None,
                              clear_reference: bool = False) -> dict[str, Any]:
    if reference is not None and clear_reference:
        raise ValueError("reference and clear_reference are mutually exclusive")
    parsed = ViewReferenceInput.model_validate(reference) if reference is not None else None
    base = _project_path(project_id)
    project = await client.get(base)
    _validate_view(project, view_id)
    body: dict[str, Any] = {"expected_revision": project["revision"]}
    if name is not None:
        body["name"] = name
    if parsed is not None:
        body["reference"] = parsed.model_dump()
    elif clear_reference:
        body["reference"] = None
    return _project_summary(await client.request("PUT", f"{base}/views/{quote(view_id, safe='')}", json=body))


async def upload_view_reference_base64(client: StudioClient, project_id: str, view_id: ViewId,
                                        content_base64: str, content_type: str = "image/png") -> dict[str, Any]:
    content = _decode_image_base64(content_base64, content_type)
    base = _project_path(project_id)
    project = await client.get(base)
    _validate_view(project, view_id, must_exist=True)
    view_path = f"{base}/views/{quote(view_id, safe='')}"
    target = await client.request("POST", f"{view_path}/reference/uploads",
        json={"expected_revision": project["revision"], "content_type": content_type})
    await client.put_file(target["upload_url"], content, content_type, target.get("upload_headers"))
    # The upload grant does not edit the manifest. Preserve the revision read before
    # the upload so a concurrent reference replacement cannot be overwritten.
    return _project_summary(await client.request("PUT", view_path, json={
        "expected_revision": project["revision"],
        "reference": {"key": target["key"], "sha256": hashlib.sha256(content).hexdigest()}}))


async def add_view_motions(client: StudioClient, project_id: str, view_id: ViewId,
                            motions: list[ViewMotionInput | dict[str, Any]] | None = None) -> dict[str, Any]:
    if motions is None:
        motions = [ViewMotionInput(motion_id=f"{view_id.lower()}-{preset}", name=preset.capitalize(), preset=preset,
                    semantic_slot=preset, loop_policy="once" if preset == "attack" else "loop")
                   for preset in ("idle", "walk", "attack")]
    parsed = [ViewMotionInput.model_validate(m) for m in motions]
    if not 1 <= len(parsed) <= 32 or len({m.motion_id for m in parsed}) != len(parsed):
        raise ValueError("supply 1 to 32 unique durable motion IDs")
    base = _project_path(project_id)
    project = await client.get(base)
    _validate_view(project, view_id, must_exist=True)
    existing = {m["motion_id"]: m for m in project["manifest"]["motions"]}
    definitions, skipped = [], []
    for motion in parsed:
        definition = motion.model_dump()
        definition.update(view_id=view_id, prompt_source="custom" if motion.kind == "custom" else "preset")
        if motion.motion_id in existing:
            current = existing[motion.motion_id]
            comparable = dict(definition)
            if motion.kind == "preset":
                # Preset compilation fills prompt/action/slot/loop. Compare authored
                # intent, not the server-generated prompt or omitted defaults.
                comparable.pop("prompt")
                comparable.pop("loop_policy")
                for key in ("action", "semantic_slot"):
                    if comparable[key] is None:
                        comparable.pop(key)
            if any(current.get(key) != value for key, value in comparable.items()):
                raise ValueError(f"motion ID {motion.motion_id!r} already has a conflicting definition")
            skipped.append(motion.motion_id)
        else:
            definitions.append(definition)
    if definitions:
        project = await client.request("POST", f"{base}/motions/batch",
            json={"expected_revision": project["revision"], "motions": definitions})
    return {**_project_summary(project), "added_motion_ids": [m["motion_id"] for m in definitions], "skipped_motion_ids": skipped}


async def update_view_motion(client: StudioClient, project_id: str, motion_id: str,
                              changes: ViewMotionChanges | dict[str, Any]) -> dict[str, Any]:
    parsed = ViewMotionChanges.model_validate(changes)
    edits = parsed.model_dump(exclude_unset=True)
    if not edits:
        raise ValueError("supply at least one motion field to update")
    base = _project_path(project_id)
    project = await client.get(base)
    motion = next((m for m in project["manifest"]["motions"] if m["motion_id"] == motion_id), None)
    if motion is None or not motion.get("view_id"):
        raise ValueError("motion must exist and belong to a declared character view")
    _validate_view(project, motion["view_id"], must_exist=True)
    definition = {key: motion[key] for key in _MOTION_DEFINITION_FIELDS if key in motion}
    definition.update(edits)
    return _project_summary(await client.request("PATCH", f"{base}/motions/{quote(motion_id, safe='')}",
        json={**definition, "expected_revision": project["revision"]}))


async def generate_motions(
    client: StudioClient, project_id: str, motion_ids: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    actions: dict[str, str] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    targets = motion_ids
    if not targets:
        project = await client.get(f"/studio/projects/{project_id}")
        targets = [motion["motion_id"] for motion in project["manifest"]["motions"]]
    if not targets:
        return {"queued": [], "failed": [], "note": "the project has no motions yet"}
    body: dict[str, Any] = {"motion_ids": targets, "request_id": request_id if request_id is not None else _new_studio_request_id("motions")}
    if settings is not None:
        body["settings"] = settings
    if actions is not None:
        body["actions"] = actions
    result = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/motions/generate-batch",
        project_id=project_id,
        body=body,
    )
    return {
        "request_id": body["request_id"],
        "queued": [
            {
                "motion_id": item["motion_id"],
                "job_id": item["operation"]["job_id"],
                "status": item["operation"]["status"],
                **_generation_evidence(item["operation"]),
            }
            for item in result["queued"]
        ],
        "failed": result["failed"],
    }


async def list_takes(
    client: StudioClient, project_id: str, motion_id: str
) -> dict[str, Any]:
    project = await client.get(f"/studio/projects/{project_id}")
    for motion in project["manifest"]["motions"]:
        if motion["motion_id"] == motion_id:
            return {
                "motion_id": motion_id,
                **_motion_view_summary(motion),
                "selected_candidate_id": motion["selected_candidate_id"],
                "takes": [_take_summary(item) for item in motion["candidates"]],
            }
    raise KeyError(f"project {project_id} has no motion {motion_id!r}")


async def select_take(
    client: StudioClient, project_id: str, motion_id: str, candidate_id: str
) -> dict[str, Any]:
    project = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/motions/{motion_id}"
        f"/candidates/{candidate_id}/select",
        project_id=project_id,
    )
    return _project_summary(project)


async def run_quality(client: StudioClient, project_id: str) -> dict[str, Any]:
    project = await client.mutate(
        "POST", f"/studio/projects/{project_id}/quality/set", project_id=project_id
    )
    assessment = project["manifest"]["set_assessment"]
    return {
        "project_id": project["project_id"],
        "revision": project["revision"],
        "fresh": project["manifest"]["set_assessment_fresh"],
        "policy_version": None if assessment is None else assessment["policy_version"],
        "findings": [] if assessment is None else assessment["findings"],
    }


async def approve_set(client: StudioClient, project_id: str) -> dict[str, Any]:
    # Findings are warnings, never blockers: an empty waiver list acknowledges them all.
    project = await client.mutate(
        "POST", f"/studio/projects/{project_id}/set/approve", project_id=project_id
    )
    return _project_summary(project)


async def create_release(
    client: StudioClient, project_id: str, name: str
) -> dict[str, Any]:
    release = await client.mutate(
        "POST",
        f"/studio/projects/{project_id}/releases",
        project_id=project_id,
        body={"name": name},
    )
    return {
        "release_id": release["release_id"],
        "project_id": release["project_id"],
        "name": release["name"],
        "source_revision": release["source_revision"],
        "release_digest": release["release_digest"],
        "created_at": release["created_at"],
    }


async def export_release(
    client: StudioClient, release_id: str, profile: str = "png-json"
) -> dict[str, Any]:
    """Export a release. `expected_revision` here means the release's own
    `source_revision`, which is not readable without the project, so the first
    attempt guesses and the 409 body supplies the real one."""
    body: dict[str, Any] = {"profile": profile, "expected_revision": 1}
    path = f"/studio/releases/{release_id}/exports"
    try:
        record = await client.request("POST", path, json=body)
    except StudioApiError as error:
        current = (
            error.detail.get("current_revision")
            if error.status_code == 409 and isinstance(error.detail, dict)
            else None
        )
        if not isinstance(current, int):
            raise
        body["expected_revision"] = current
        record = await client.request("POST", path, json=body)
    return _export_summary(record)


async def download_export(
    client: StudioClient, export_id: str, dest_path: str
) -> dict[str, Any]:
    target = await client.get(f"/studio/exports/{export_id}/download")
    payload = await client.download(target["upload_url"])
    destination = Path(dest_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {
        "path": str(destination),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _new_studio_request_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    manifest = project["manifest"]
    return {
        "project_id": project["project_id"],
        "revision": project["revision"],
        "name": manifest["name"],
        "primary_action": project["primary_action"],
        "subject_approval": manifest["subject"]["approval"]["state"],
        "set_approval": manifest["set_approval"]["state"],
        "sprite_layout": manifest.get("sprite_layout", "unassigned"),
        "views": manifest.get("views", []),
        "motions": [
            {
                "motion_id": motion["motion_id"],
                "name": motion["name"],
                **_motion_view_summary(motion),
                "takes": len(motion["candidates"]),
                "selected_candidate_id": motion["selected_candidate_id"],
            }
            for motion in manifest["motions"]
        ],
        "active_operations": [
            _operation_summary(item) for item in project["active_operations"]
        ],
    }


def _motion_view_summary(motion: dict[str, Any]) -> dict[str, Any]:
    return {key: motion.get(key) for key in ("view_id", "view_heading", "setup_version", "view_reference", "kind", "preset", "action", "spec_digest")}


def _generation_evidence(operation: dict[str, Any]) -> dict[str, Any]:
    return {key: operation.get(key) for key in (
        "prompt", "generation_settings", "requested_settings", "resolved_recipe", "execution_provenance",
        "generation_evidence", "source", "spec_digest", "view_id", "view_reference", "reference_source_sha256",
        "prepared_frame_sha256",
    )}


def _operation_summary(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": operation["job_id"],
        "operation": operation["operation"],
        "motion_id": operation["motion_id"],
        "status": operation["status"],
        "stage": operation["stage"],
        "failure_code": operation["failure_code"],
        "failure_explanation": operation["failure_explanation"],
        **_generation_evidence(operation),
    }


def _take_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    assessment = candidate["assessment"]
    return {
        "candidate_id": candidate["candidate_id"],
        "artifact_key": candidate["artifact_key"],
        "duration_ms": candidate["duration_ms"],
        "facing": candidate["facing"],
        "source": candidate["source"],
        "findings": [] if assessment is None else assessment["findings"],
        **{key: candidate.get(key) for key in (
            "sha256", "metadata", "generation_settings", "recipe", "execution_provenance",
            "ordered_frames", "frame_duration_ms", "loop_policy",
        )},
    }


def _export_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_id": record["export_id"],
        "release_id": record["release_id"],
        "profile": f"{record['profile_name']}-{record['profile_version']}",
        "artifact_key": record["artifact_key"],
        "sha256": record["sha256"],
        "size_bytes": record["size_bytes"],
    }


# ---------------------------------------------------------------- movies

# Exactly the server's movie upload types (service.upload).
_MOVIE_UPLOAD_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
}
_ACTIVE_TAKE = frozenset({"waiting", "submitting", "running"})


def _movie_path(movie_id: str, suffix: str = "") -> str:
    return f"/studio/movies/{quote(movie_id, safe='')}{suffix}"


def _shot_path(movie_id: str, shot_id: str, suffix: str) -> str:
    return _movie_path(movie_id, f"/shots/{quote(shot_id, safe='')}{suffix}")


async def get_movie_capabilities(client: StudioClient) -> dict[str, Any]:
    return await client.get("/studio/movies/capabilities")


async def create_movie(client: StudioClient, name: str) -> dict[str, Any]:
    return await client.request("POST", "/studio/movies", json={"name": name})


async def get_movie(client: StudioClient, movie_id: str) -> dict[str, Any]:
    return await client.get(_movie_path(movie_id))


# The route models forbid extra fields; these are what each accepts. Output fields
# from get_movie (takes, voice_seconds, opening_asset_key...) are dropped, so an agent
# can edit what it read and send it back.
_CHARACTER_FIELDS = ("character_id", "name", "appearance", "voice", "possessive",
                     "voice_asset_key", "audition_line")
_SHOT_FIELDS = ("shot_id", "name", "prompt", "opening_prompt", "duration_seconds",
                "continuation_of", "dialogue", "soundscape", "camera", "still", "beats")
_LINE_FIELDS = ("character_id", "text", "delivery", "pause_before", "reaction")
_BEAT_FIELDS = ("at_seconds", "action")


def _only(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: item[key] for key in fields if key in item}


def _shot_input(shot: dict[str, Any]) -> dict[str, Any]:
    kept = _only(shot, _SHOT_FIELDS)
    if "dialogue" in kept:
        kept["dialogue"] = [_only(line, _LINE_FIELDS) for line in kept["dialogue"]]
    if "beats" in kept:
        kept["beats"] = [_only(beat, _BEAT_FIELDS) for beat in kept["beats"]]
    return kept


async def set_movie_cast(
    client: StudioClient, movie_id: str, characters: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replace the cast. A character that already has an attached voice keeps it unless
    the caller supplies another `voice_asset_key` (dropping it would stale its lines)."""
    current = await get_movie(client, movie_id)
    voices = {c["character_id"]: c.get("voice_asset_key") for c in current.get("characters", [])}
    cast = []
    for character in characters:
        kept = _only(character, _CHARACTER_FIELDS)
        if "voice_asset_key" not in kept and voices.get(kept.get("character_id")):
            kept["voice_asset_key"] = voices[kept["character_id"]]
        cast.append(kept)
    return await client.mutate_movie(
        "PUT", _movie_path(movie_id, "/characters"), movie_id=movie_id,
        body={"characters": cast})


async def set_movie_story(
    client: StudioClient, movie_id: str, shots: list[dict[str, Any]],
    brief: str | None = None, style: str | None = None, batch_size: int | None = None,
    compute_target: str | None = None,
) -> dict[str, Any]:
    """Replace the shot list. Settings left as None keep their current values: a changed
    style or brief makes every take stale, and compute_target must not flip silently."""
    current = await get_movie(client, movie_id)
    settings = {"brief": brief, "style": style, "batch_size": batch_size,
                "compute_target": compute_target}
    body = {key: current.get(key) if value is None else value for key, value in settings.items()}
    body = {key: value for key, value in body.items() if value is not None}
    body["shots"] = [_shot_input(shot) for shot in shots]
    return await client.mutate_movie(
        "PUT", _movie_path(movie_id, "/story"), movie_id=movie_id, body=body)


async def upload_movie_asset(client: StudioClient, movie_id: str, path: str) -> dict[str, Any]:
    """Upload a local png/jpeg/webp/mp4/mpeg/wav and return its asset key."""
    source = Path(path).expanduser()
    content_type = _MOVIE_UPLOAD_TYPES.get(source.suffix.lower())
    if content_type is None:
        raise ValueError("Choose a png, jpeg, webp, mp4, mp3 or wav file.")
    data = source.read_bytes()
    if not data:
        raise ValueError("Choose a nonempty file.")
    target = await client.request(
        "POST", _movie_path(movie_id, "/uploads"), json={"content_type": content_type})
    await client.put_file(target["upload_url"], data, content_type, target.get("upload_headers"))
    return {"asset_key": target["asset_key"], "revision": target["revision"]}


async def set_shot_opening(
    client: StudioClient, movie_id: str, shot_id: str, asset_key: str
) -> dict[str, Any]:
    return await client.mutate_movie(
        "POST", _shot_path(movie_id, shot_id, "/opening"), movie_id=movie_id,
        body={"asset_key": asset_key})


def _request_id(request_id: str | None) -> str:
    # The server treats a repeated request_id as the same request and does nothing, so a
    # fresh id is the default; reuse one only to retry the exact same call.
    return request_id or str(uuid4())


async def build_movie(
    client: StudioClient, movie_id: str, request_id: str | None = None
) -> dict[str, Any]:
    return await client.mutate_movie(
        "POST", _movie_path(movie_id, "/build"), movie_id=movie_id,
        body={"request_id": _request_id(request_id)})


async def generate_takes(
    client: StudioClient, movie_id: str, shot_ids: list[str], request_id: str | None = None,
    select_when_ready: bool = True,
) -> dict[str, Any]:
    return await client.mutate_movie("POST", _movie_path(movie_id, "/generate"), movie_id=movie_id, body={
        "shot_ids": shot_ids, "request_id": _request_id(request_id),
        "select_when_ready": select_when_ready,
    })


async def select_movie_take(
    client: StudioClient, movie_id: str, shot_id: str, take_id: str
) -> dict[str, Any]:
    return await client.mutate_movie(
        "POST", _shot_path(movie_id, shot_id, "/select"), movie_id=movie_id,
        body={"take_id": take_id})


_ACTIVE_ASSEMBLY = frozenset({"waiting", "pending", "running", "submitting"})
_RETRYABLE_RECONCILE = frozenset({409, 429, 502, 503, 504})


def _composition_active(composition: dict[str, Any]) -> bool:
    # Same rule as the web app's `movieActive`: compositions are a permanent history,
    # so only pending ones (or a superseded pool job still settling) are in flight.
    status = composition.get("status")
    return status == "pending" or (
        status == "superseded" and composition.get("provider") == "pool"
        and not composition.get("settled"))


def _movie_state(movie: dict[str, Any]) -> tuple[bool, list[str]]:
    """(still working, current failures) for a reconciled movie.

    A shot's failure counts only if its newest take failed: older failed takes are
    history once another take exists, and must not end a later wait.
    """
    busy, failed = False, []
    for shot in movie.get("shots", []):
        takes = shot.get("takes", [])
        if any(take.get("status") in _ACTIVE_TAKE for take in takes):
            busy = True
        if takes and takes[-1].get("status") == "failed":
            failed.append(f"take {shot['shot_id']}/{takes[-1]['take_id']}")
    build = movie.get("build") or {}
    if build.get("status") in ("generating", "assembling"):
        busy = True
    elif build.get("status") == "failed":
        failed.append("build")
    assembly = movie.get("assembly") or {}
    if assembly.get("status") in _ACTIVE_ASSEMBLY:
        busy = True
    elif assembly.get("status") == "failed":
        failed.append("assembly")
    if any(_composition_active(c) for c in (movie.get("compositions") or {}).values()):
        busy = True
    return busy, failed


async def wait_for_movie(
    client: StudioClient, movie_id: str, timeout_seconds: float = 600,
    interval_seconds: float = 15, *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Drive the movie with reconcile until nothing is in flight or the timeout passes,
    then report the current failures. Reconcile is what advances jobs; nothing runs
    server-side. A busy or briefly unavailable reconcile is retried, not fatal."""
    deadline = clock() + timeout_seconds
    movie: dict[str, Any] | None = None
    while True:
        try:
            movie = await client.request("POST", _movie_path(movie_id, "/reconcile"))
        except StudioApiError as error:
            if error.status_code not in _RETRYABLE_RECONCILE:
                raise
        except httpx.TransportError:
            pass
        else:
            busy, failed = _movie_state(movie)
            if not busy:
                return {"settled": True, "failed": failed, "movie": movie}
        if clock() >= deadline:
            failed = _movie_state(movie)[1] if movie is not None else []
            return {"settled": False, "failed": failed, "movie": movie}
        await sleep(interval_seconds)


async def set_movie_cut(
    client: StudioClient, movie_id: str, clips: list[dict[str, Any]]
) -> dict[str, Any]:
    return await client.mutate_movie(
        "PUT", _movie_path(movie_id, "/cut"), movie_id=movie_id, body={"clips": clips})


async def set_movie_soundtracks(
    client: StudioClient, movie_id: str, tracks: list[dict[str, Any]]
) -> dict[str, Any]:
    return await client.mutate_movie(
        "PUT", _movie_path(movie_id, "/soundtracks"), movie_id=movie_id,
        body={"tracks": tracks})


async def assemble_movie(
    client: StudioClient, movie_id: str, request_id: str | None = None
) -> dict[str, Any]:
    return await client.mutate_movie(
        "POST", _movie_path(movie_id, "/assemble"), movie_id=movie_id,
        body={"request_id": _request_id(request_id)})


async def release_movie(client: StudioClient, movie_id: str, name: str) -> dict[str, Any]:
    return await client.mutate_movie(
        "POST", _movie_path(movie_id, "/release"), movie_id=movie_id, body={"name": name})


async def download_movie_release(
    client: StudioClient, movie_id: str, release_id: str, destination: str
) -> dict[str, Any]:
    data = await client.download(
        _movie_path(movie_id, f"/releases/{quote(release_id, safe='')}/download"))
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


# Audio sets: free sounds and uploads per cue, review, selection and licensed releases.
_AUDIO_UPLOAD_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}
_CUE_FIELDS = ("cue_id", "name", "kind", "prompt", "text", "target_seconds", "loop", "keep")


def _audio_path(set_id: str, suffix: str = "") -> str:
    return f"/studio/audio/{quote(set_id, safe='')}{suffix}"


async def get_audio_capabilities(client: StudioClient) -> dict[str, Any]:
    return await client.get("/studio/audio/capabilities")


async def create_audio_set(client: StudioClient, name: str) -> dict[str, Any]:
    return await client.request("POST", "/studio/audio", json={"name": name})


async def get_audio_set(client: StudioClient, set_id: str) -> dict[str, Any]:
    return await client.get(_audio_path(set_id))


async def set_audio_set(
    client: StudioClient, set_id: str, cues: list[dict[str, Any]],
    name: str | None = None, brief: str | None = None, licence_policy: str | None = None,
) -> dict[str, Any]:
    """Replace the cues. Settings left as None keep their current values, so the licence
    policy never changes unless asked; cue output fields (`selected`) are dropped."""
    # The kept settings and expected_revision come from the same read, so a change
    # made elsewhere in between is a conflict, never silently undone; on a conflict
    # the body is rebuilt from a fresh read and sent once more.
    for attempt in range(2):
        current = await get_audio_set(client, set_id)
        body = {
            "expected_revision": current["revision"],
            "name": current["name"] if name is None else name,
            "brief": current.get("brief", "") if brief is None else brief,
            "licence_policy": current.get("licence_policy", "cc0") if licence_policy is None else licence_policy,
            "cues": [_only(cue, _CUE_FIELDS) for cue in cues],
        }
        try:
            return await client.request("PUT", _audio_path(set_id), json=body)
        except StudioApiError as error:
            stale = error.status_code == 409 and _revision_from_conflict(error.detail) is not None
            if not stale or attempt:
                raise
    raise AssertionError("unreachable")


async def search_free_sounds(
    client: StudioClient, set_id: str, query: str, min_seconds: float = 0,
    max_seconds: float = 120, page: int = 1,
) -> list[dict[str, Any]]:
    suffix = (f"/free-sounds?q={quote(query, safe='')}&min_seconds={min_seconds:g}"
              f"&max_seconds={max_seconds:g}&page={page}")
    return await client.get(_audio_path(set_id, suffix))


async def import_free_sound(
    client: StudioClient, set_id: str, cue_id: str, source_id: str
) -> dict[str, Any]:
    return await client.mutate_audio(
        "POST", _audio_path(set_id, f"/cues/{quote(cue_id, safe='')}/import"), set_id=set_id,
        body={"source": "freesound", "source_id": source_id})


async def _upload_audio(client: StudioClient, set_id: str, path: str) -> tuple[str, str]:
    source = Path(path).expanduser()
    content_type = _AUDIO_UPLOAD_TYPES.get(source.suffix.lower())
    if content_type is None:
        raise ValueError("Choose a WAV or MP3 file.")
    data = source.read_bytes()
    if not data:
        raise ValueError("Choose a nonempty file.")
    target = await client.request(
        "POST", _audio_path(set_id, "/uploads"), json={"content_type": content_type})
    await client.put_file(target["upload_url"], data, content_type, target.get("upload_headers"))
    return target["asset_key"], source.name


async def upload_audio_candidate(
    client: StudioClient, set_id: str, cue_id: str, path: str
) -> dict[str, Any]:
    asset_key, filename = await _upload_audio(client, set_id, path)
    return await client.mutate_audio(
        "POST", _audio_path(set_id, f"/cues/{quote(cue_id, safe='')}/upload"), set_id=set_id,
        body={"asset_key": asset_key, "filename": filename[:200]})


async def replace_audio_original(
    client: StudioClient, set_id: str, candidate_id: str, path: str
) -> dict[str, Any]:
    asset_key, _ = await _upload_audio(client, set_id, path)
    return await client.mutate_audio(
        "POST", _audio_path(set_id, f"/candidates/{quote(candidate_id, safe='')}/original"),
        set_id=set_id, body={"asset_key": asset_key})


async def review_audio_candidate(
    client: StudioClient, set_id: str, candidate_id: str, outcome: Literal["keep", "reject"],
    note: str = "", reason: str = "",
) -> dict[str, Any]:
    """The verdict names the file it is about: the candidate's hash and the revision
    come from one read, and a retry after a stale revision keeps that hash, so the
    server refuses it if the file was replaced in between."""
    current = await get_audio_set(client, set_id)
    candidate = next((c for c in current.get("candidates", []) if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError(f"No candidate {candidate_id} in this audio set.")
    path = _audio_path(set_id, f"/candidates/{quote(candidate_id, safe='')}/review")
    body = {"expected_revision": current["revision"], "outcome": outcome, "note": note,
            "reason": reason, "sha256": candidate["sha256"]}
    try:
        return await client.request("POST", path, json=body)
    except StudioApiError as error:
        refreshed = _revision_from_conflict(error.detail) if error.status_code == 409 else None
        if refreshed is None:
            raise
        return await client.request("POST", path, json={**body, "expected_revision": refreshed})


async def select_audio_candidates(
    client: StudioClient, set_id: str, cue_id: str, candidate_ids: list[str]
) -> dict[str, Any]:
    return await client.mutate_audio(
        "PUT", _audio_path(set_id, f"/cues/{quote(cue_id, safe='')}/selection"), set_id=set_id,
        body={"candidate_ids": candidate_ids})


async def get_audio_readiness(client: StudioClient, set_id: str) -> dict[str, Any]:
    return await client.get(_audio_path(set_id, "/readiness"))


async def release_audio_set(client: StudioClient, set_id: str, name: str) -> dict[str, Any]:
    return await client.mutate_audio(
        "POST", _audio_path(set_id, "/release"), set_id=set_id, body={"name": name})


async def download_audio_release(
    client: StudioClient, set_id: str, release_id: str, destination: str
) -> dict[str, Any]:
    data = await client.download(
        _audio_path(set_id, f"/releases/{quote(release_id, safe='')}/download"))
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


async def send_audio_to_movie(
    client: StudioClient, set_id: str, candidate_id: str, movie_id: str
) -> dict[str, Any]:
    path = _audio_path(set_id, f"/candidates/{quote(candidate_id, safe='')}/send-to-movie")
    body = {"movie_id": movie_id, "movie_revision": await client.movie_revision(movie_id)}
    try:
        return await client.request("POST", path, json=body)
    except StudioApiError as error:
        refreshed = _revision_from_conflict(error.detail) if error.status_code == 409 else None
        if refreshed is None:
            raise
        return await client.request("POST", path, json={**body, "movie_revision": refreshed})


async def get_generation_config(client: StudioClient, worker_id: str | None = None) -> dict[str, Any]:
    """Read defaults; pass returned settings unchanged to bind the chosen recipe."""
    suffix = "?worker_id=" + quote(worker_id, safe="") if worker_id else ""
    return await client.get("/studio/generation-config" + suffix)


async def inspect_candidate_frames(client: StudioClient, project_id: str, motion_id: str,
                                   candidate_id: str, start: int = 0, limit: int = 8,
                                   stride: int = 1) -> dict[str, Any]:
    if not 0 <= start or not 1 <= limit <= 12 or not 1 <= stride <= 512:
        raise ValueError("choose start >= 0, limit 1–12, and stride 1–512")
    return await client.get(
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}"
        f"/candidates/{quote(candidate_id, safe='')}/frames?start={start}&limit={limit}&stride={stride}")


async def suggest_motion_loop(client: StudioClient, project_id: str, motion_id: str,
                              candidate_id: str, mode: Literal["idle", "walk", "attack"], target_frame_count: int = 12) -> dict[str, Any]:
    if mode not in {"idle", "walk", "attack"} or target_frame_count not in {8, 12, 16}:
        raise ValueError("choose idle, walk, or attack with 8, 12, or 16 frames")
    return await client.request("POST",
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}"
        f"/candidates/{quote(candidate_id, safe='')}/loop-suggestion",
        json={"mode": mode, "target_frame_count": target_frame_count})


async def create_frame_revision(client: StudioClient, project_id: str, motion_id: str,
                                 source_candidate_id: str, ordered_frames: list[int],
                                 frame_duration_ms: int, loop_policy: str = "loop",
                                 target_frame_count: int | None = None, cell_size: int | None = None,
                                 normalization: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(source_candidate_id=source_candidate_id, ordered_frames=ordered_frames,
                frame_duration_ms=frame_duration_ms, loop_policy=loop_policy)
    if target_frame_count is not None:
        body["target_frame_count"] = target_frame_count
    if cell_size is not None:
        body["cell_size"] = cell_size
    if normalization is not None:
        body["normalization"] = normalization
    project = await client.mutate("POST",
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}/revisions",
        project_id=project_id, body=body)
    motion = next(m for m in project["manifest"]["motions"] if m["motion_id"] == motion_id)
    return {"project_id": project_id, "revision": project["revision"],
            "selected_candidate_id": motion["selected_candidate_id"],
            "candidate": next(c for c in motion["candidates"] if c["candidate_id"] == motion["selected_candidate_id"])}


async def get_candidate_download(client: StudioClient, project_id: str, motion_id: str,
                                  candidate_id: str, format: str = "png") -> dict[str, Any]:
    suffix = {"png": "sheet.png", "webm": "preview.webm", "gif": "preview.gif"}.get(format)
    if suffix is None:
        raise ValueError("format must be png, webm, or gif")
    target = await client.get(
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}"
        f"/candidates/{quote(candidate_id, safe='')}/{suffix}")

    project = await client.get(f"/studio/projects/{quote(project_id, safe='')}")
    motions = project["manifest"]["motions"]
    motion = next(m for m in motions if m["motion_id"] == motion_id)
    candidate = next(c for c in motion["candidates"] if c["candidate_id"] == candidate_id)
    metadata = candidate.get("metadata") or {}
    pivot_x, pivot_y = metadata.get("pivot_x"), metadata.get("pivot_y")
    for item in motions:
        selected = next((c for c in item["candidates"] if c["candidate_id"] == item["selected_candidate_id"]), None)
        normalization = ((selected or {}).get("recipe") or {}).get("normalization") or {}
        if (normalization.get("reference_candidate_id") == candidate_id
                and normalization.get("reference_sha256") == candidate["sha256"]):
            registered = (round(normalization["target_anchor_x"]), round(normalization["target_anchor_y"]))
            if pivot_x is not None and pivot_y is not None and registered != (pivot_x, pivot_y):
                raise ValueError("selected calibrations disagree on reference pivot")
            pivot_x, pivot_y = registered
    width = metadata.get("width")
    if width is not None and metadata.get("media_kind") == "sheet":
        width //= max(1, metadata.get("frame_count", 1))
    if candidate.get("facing") == "left" and pivot_x is not None and width is not None:
        pivot_x = width - pivot_x
    return {**target, "frame_geometry": {"frame_width": width, "frame_height": metadata.get("height"),
                                        "pivot_x": pivot_x, "pivot_y": pivot_y},
            "placement": "Position each frame so its exported pivot coincides with the shared actor origin."}


async def upload_subject_base64(client: StudioClient, project_id: str, image_base64: str,
                                 content_type: str = "image/png") -> dict[str, Any]:
    content = _decode_image_base64(image_base64, content_type)
    target = await client.mutate("POST", f"/studio/projects/{quote(project_id, safe='')}/subject/uploads",
                                 project_id=project_id, body={"content_type": content_type})
    await client.put_file(target["upload_url"], content, content_type, target.get("upload_headers"))
    project = await client.mutate("POST", f"/studio/projects/{quote(project_id, safe='')}/subject/settle",
                                  project_id=project_id, body={"asset_key": target["key"]})
    return _project_summary(project)


def _decode_image_base64(image_base64: str, content_type: str) -> bytes:
    import base64
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("choose PNG, JPEG, or WebP")
    if len(image_base64) > 14_000_000:
        raise ValueError("image must be at most 10 MB")
    content = base64.b64decode(image_base64, validate=True)
    if not content or len(content) > 10_000_000:
        raise ValueError("image must contain 1 to 10 MB of image data")
    return content


async def derive_candidate_alpha(client: StudioClient, project_id: str, motion_id: str,
                                  candidate_id: str) -> dict[str, Any]:
    project = await client.mutate("POST",
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}"
        f"/candidates/{quote(candidate_id, safe='')}/derive-alpha",
        project_id=project_id)
    return _project_summary(project)


async def calibrate_motion_body(client: StudioClient, project_id: str, motion_id: str,
                                 candidate_id: str, source_frame: int, reference_candidate_id: str,
                                 reference_motion_id: str | None = None, reference_frame: int = 0) -> dict[str, Any]:
    project = await client.get(_project_path(project_id))
    motions = {motion["motion_id"]: motion for motion in project["manifest"]["motions"]}
    source = motions.get(motion_id)
    if source is None:
        raise ValueError("source motion not found")
    if source.get("view_id") is not None and reference_motion_id is None:
        raise ValueError("view-scoped calibration requires an explicit same-view reference_motion_id")
    resolved_reference = reference_motion_id if reference_motion_id is not None else "walk"
    reference = motions.get(resolved_reference)
    if reference is None or reference.get("view_id") != source.get("view_id"):
        raise ValueError("calibration reference must exist in the same view as the source")
    return await client.request("POST",
        f"/studio/projects/{quote(project_id, safe='')}/motions/{quote(motion_id, safe='')}"
        f"/candidates/{quote(candidate_id, safe='')}/body-calibration",
        json=dict(source_frame=source_frame, reference_candidate_id=reference_candidate_id,
                  reference_motion_id=resolved_reference, reference_frame=reference_frame))


async def get_setup_capabilities(client: StudioClient) -> dict[str, Any]:
    return await client.get("/studio/setup-capabilities")


async def get_setup(client: StudioClient, project_id: str) -> dict[str, Any]:
    return await client.get(f"{_project_path(project_id)}/setup")


async def preview_setup(client: StudioClient, project_id: str, expected_revision: int,
                        desired_setup: dict[str, Any]) -> dict[str, Any]:
    # Never retry revision-bound previews or silently add template suggestions.
    return await client.request("POST", f"{_project_path(project_id)}/setup/preview",
        json={"expected_revision": expected_revision, "desired_setup": desired_setup})


async def apply_setup(client: StudioClient, project_id: str, expected_revision: int,
                      plan_token: str, confirmed_impacts: list[str] | None = None) -> dict[str, Any]:
    return await client.request("POST", f"{_project_path(project_id)}/setup/apply",
        json={"expected_revision": expected_revision, "plan_token": plan_token,
              "confirmed_impacts": confirmed_impacts or []})


async def upload_setup_reference_base64(client: StudioClient, project_id: str,
        image_base64: str, content_type: str = "image/png") -> dict[str, Any]:
    """Stage verified artwork without changing active identity or view bindings."""
    return await client.request("POST", f"{_project_path(project_id)}/setup/references",
        json={"content_base64": image_base64, "content_type": content_type})


async def abandon_setup_dispatch(client: StudioClient, project_id: str, operation_id: str,
        expected_revision: int, confirm_dispatch_stopped: bool = False) -> dict[str, Any]:
    return await client.request("POST", f"{_project_path(project_id)}/setup/dispatches/{quote(operation_id, safe='')}/abandon",
        json={"expected_revision": expected_revision, "confirm_dispatch_stopped": confirm_dispatch_stopped})


# ---------------------------------------------------------------- scene flights

_FLIGHT_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_LEG_FIELDS = ("leg_id", "origin_image_key", "destination_image_key", "prompt", "origin_name",
               "destination_name", "origin_description", "destination_description",
               "negative_prompt", "name", "duration_seconds")


def _flight_path(flight_id: str, suffix: str = "") -> str:
    return f"/studio/scene-flights/{quote(flight_id, safe='')}{suffix}"


async def _flight_mutation(
    client: StudioClient, method: str, flight_id: str, suffix: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Send with the current revision; retry once only on a stale-revision 409."""
    payload = {**body, "expected_revision": (await get_scene_flight(client, flight_id))["revision"]}
    try:
        return await client.request(method, _flight_path(flight_id, suffix), json=payload)
    except StudioApiError as error:
        refreshed = _revision_from_conflict(error.detail) if error.status_code == 409 else None
        if refreshed is None:
            raise
        payload["expected_revision"] = refreshed
        return await client.request(method, _flight_path(flight_id, suffix), json=payload)


async def get_scene_flight_capabilities(client: StudioClient) -> dict[str, Any]:
    return await client.get("/studio/scene-flights/capabilities")


async def create_scene_flight(client: StudioClient, name: str) -> dict[str, Any]:
    return await client.request("POST", "/studio/scene-flights", json={"name": name})


async def get_scene_flight(client: StudioClient, flight_id: str) -> dict[str, Any]:
    """Current state; reading it also advances finished GPU jobs into takes."""
    return await client.get(_flight_path(flight_id))


async def add_scene_flight_image(
    client: StudioClient, flight_id: str, path: str | None = None,
    content_base64: str | None = None, content_type: str = "image/png",
) -> dict[str, Any]:
    """Add an origin/destination image from a local file or base64 bytes."""
    if (path is None) == (content_base64 is None):
        raise ValueError("Give exactly one of path or content_base64.")
    if path is not None:
        source = Path(path).expanduser()
        content_type = _FLIGHT_IMAGE_TYPES.get(source.suffix.lower(), "")
        if not content_type:
            raise ValueError("Choose a png, jpeg or webp image.")
        content_base64 = base64.b64encode(source.read_bytes()).decode()
    return await client.request(
        "POST", _flight_path(flight_id, "/images"),
        json={"content_base64": content_base64, "content_type": content_type})


async def set_scene_flight_legs(
    client: StudioClient, flight_id: str, legs: list[dict[str, Any]]
) -> dict[str, Any]:
    return await _flight_mutation(
        client, "PUT", flight_id, "/legs", {"legs": [_only(leg, _LEG_FIELDS) for leg in legs]})


async def generate_scene_flight_take(
    client: StudioClient, flight_id: str, leg_id: str, request_id: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"request_id": _request_id(request_id)}
    if seed is not None:
        body["seed"] = seed
    return await _flight_mutation(
        client, "POST", flight_id, f"/legs/{quote(leg_id, safe='')}/takes", body)


async def select_scene_flight_take(
    client: StudioClient, flight_id: str, leg_id: str, take_id: str
) -> dict[str, Any]:
    return await _flight_mutation(
        client, "POST", flight_id, f"/legs/{quote(leg_id, safe='')}/select", {"take_id": take_id})


async def chain_scene_flight(
    client: StudioClient, flight_id: str, request_id: str | None = None
) -> dict[str, Any]:
    return await _flight_mutation(
        client, "POST", flight_id, "/chain", {"request_id": _request_id(request_id)})


def _flight_state(flight: dict[str, Any]) -> tuple[bool, list[str]]:
    busy, failed = False, []
    for leg in flight.get("legs", []):
        takes = leg.get("takes", [])
        if any(take.get("status") in _ACTIVE_TAKE for take in takes):
            busy = True
        if takes and takes[-1].get("status") in {"failed", "cancelled"}:
            failed.append(f"take {leg['leg_id']}/{takes[-1]['take_id']}: {takes[-1].get('error')}")
    chain = flight.get("chain") or {}
    if chain.get("status") == "pending":
        busy = True
    elif chain.get("status") == "failed":
        failed.append(f"chain: {chain.get('error')}")
    return busy, failed


async def wait_for_scene_flight(
    client: StudioClient, flight_id: str, timeout_seconds: float = 1800,
    interval_seconds: float = 15, *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll the flight (which reconciles it) until no take or chain is in flight."""
    deadline = clock() + timeout_seconds
    flight: dict[str, Any] | None = None
    while True:
        try:
            flight = await get_scene_flight(client, flight_id)
        except StudioApiError as error:
            if error.status_code not in _RETRYABLE_RECONCILE:
                raise
        except httpx.TransportError:
            pass
        else:
            busy, failed = _flight_state(flight)
            if not busy:
                return {"settled": True, "failed": failed, "flight": flight}
        if clock() >= deadline:
            failed = _flight_state(flight)[1] if flight is not None else []
            return {"settled": False, "failed": failed, "flight": flight}
        await sleep(interval_seconds)


async def download_scene_flight_video(
    client: StudioClient, flight_id: str, destination: str,
    leg_id: str | None = None, take_id: str | None = None,
) -> dict[str, Any]:
    """Download one take (leg_id + take_id; the leg's selected take when take_id is
    omitted) or, with neither, the chained flight."""
    if leg_id is None and take_id is not None:
        raise ValueError("Give leg_id with take_id.")
    if leg_id is None:
        suffix = "/chain/video?download=true"
    else:
        if take_id is None:
            flight = await get_scene_flight(client, flight_id)
            leg = next((item for item in flight["legs"] if item["leg_id"] == leg_id), None)
            if leg is None or not leg.get("selected_take_id"):
                raise ValueError("That leg has no selected take yet.")
            take_id = leg["selected_take_id"]
        suffix = (f"/legs/{quote(leg_id, safe='')}/takes/{quote(take_id, safe='')}"
                  "/video?download=true")
    data = await client.download(_flight_path(flight_id, suffix))
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "take_id": take_id}


async def generate_scene_flight_leg(
    client: StudioClient, origin_path: str, destination_path: str, *,
    prompt: str = "", origin_name: str = "", destination_name: str = "",
    origin_description: str = "", destination_description: str = "",
    negative_prompt: str = "", duration_seconds: float = 5.0, seed: int | None = None,
    name: str | None = None, flight_id: str | None = None,
) -> dict[str, Any]:
    """One leg in one call: a flight (new unless given), both images, the leg, a take.
    Follow with wait_for_scene_flight and download_scene_flight_video."""
    if flight_id is None:
        label = name or (f"{origin_name} to {destination_name}" if origin_name and destination_name
                         else "Scene flight")
        flight_id = (await create_scene_flight(client, label))["flight_id"]
    origin = await add_scene_flight_image(client, flight_id, origin_path)
    destination = await add_scene_flight_image(client, flight_id, destination_path)
    current = await get_scene_flight(client, flight_id)
    legs = [_only(leg, _LEG_FIELDS) for leg in current["legs"]]
    leg_id = f"leg-{len(legs) + 1}"
    while any(leg["leg_id"] == leg_id for leg in legs):
        leg_id += "x"
    legs.append({
        "leg_id": leg_id, "origin_image_key": origin["image_key"],
        "destination_image_key": destination["image_key"], "prompt": prompt,
        "origin_name": origin_name, "destination_name": destination_name,
        "origin_description": origin_description, "destination_description": destination_description,
        "negative_prompt": negative_prompt, "duration_seconds": duration_seconds,
    })
    await set_scene_flight_legs(client, flight_id, legs)
    flight = await generate_scene_flight_take(client, flight_id, leg_id, seed=seed)
    return {"flight_id": flight_id, "leg_id": leg_id, "flight": flight}


# Other directions from one image: a turnaround take, picks, drafts. Binding a
# checked draft uses preview_setup/apply_setup with {"revision_id": "draft-..."}.

def _turnaround_path(project_id: str, turnaround_id: str = "") -> str:
    path = f"{_project_path(project_id)}/turnarounds"
    return path + "/" + quote(turnaround_id, safe="") if turnaround_id else path


async def start_turnaround(client: StudioClient, project_id: str, source_view_id: str, source_revision_id: str,
                           equipment: list[dict[str, str]] | None = None, settings: dict[str, Any] | None = None,
                           request_id: str | None = None) -> dict[str, Any]:
    """Queue one closed-loop turn of a bound front (S) or front three-quarter (SE, SW) reference."""
    body: dict[str, Any] = {"source_view_id": source_view_id, "source_revision_id": source_revision_id,
                            "equipment": equipment or [], "request_id": _request_id(request_id)}
    if settings is not None:
        body["settings"] = settings
    return await client.mutate("POST", _turnaround_path(project_id), project_id=project_id, body=body)


async def list_turnarounds(client: StudioClient, project_id: str) -> dict[str, Any]:
    return await client.get(_turnaround_path(project_id))


async def get_turnaround(client: StudioClient, project_id: str, turnaround_id: str) -> dict[str, Any]:
    """One take with its suggested picks, placement cards and saved pick set."""
    return await client.get(_turnaround_path(project_id, turnaround_id))


async def inspect_turnaround_frames(client: StudioClient, project_id: str, turnaround_id: str, start: int = 0,
                                    limit: int = 8, stride: int = 1) -> dict[str, Any]:
    if not 0 <= start or not 1 <= limit <= 12 or not 1 <= stride <= 512:
        raise ValueError("choose start >= 0, limit 1–12, and stride 1–512")
    return await client.get(f"{_turnaround_path(project_id, turnaround_id)}/frames"
                            f"?start={start}&limit={limit}&stride={stride}")


async def save_turnaround_picks(client: StudioClient, project_id: str, turnaround_id: str,
                                expected_pick_revision: int, picks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # Never retried: a stale pick revision means someone else changed the picks,
    # and the caller must read them again rather than overwrite.
    return await client.request("PUT", f"{_turnaround_path(project_id, turnaround_id)}/picks",
                                json={"expected_pick_revision": expected_pick_revision, "picks": picks})


async def create_view_draft(client: StudioClient, project_id: str, turnaround_id: str, pick_revision: int,
                            heading: str, request_id: str | None = None) -> dict[str, Any]:
    """A draft of one picked heading; repeating the same request_id returns the same draft."""
    return await client.mutate("POST", f"{_project_path(project_id)}/view-drafts", project_id=project_id, body={
        "turnaround_id": turnaround_id, "pick_revision": pick_revision, "heading": heading,
        "standing": False, "request_id": _request_id(request_id)})


async def list_view_drafts(client: StudioClient, project_id: str) -> dict[str, Any]:
    return await client.get(f"{_project_path(project_id)}/view-drafts")


async def check_view_draft(client: StudioClient, project_id: str, draft_id: str, facing: bool, equipment: bool,
                           identity: bool) -> dict[str, Any]:
    return await client.mutate("POST", f"{_project_path(project_id)}/view-drafts/{quote(draft_id, safe='')}/check",
                               project_id=project_id,
                               body={"facing": facing, "equipment": equipment, "identity": identity})


async def discard_view_draft(client: StudioClient, project_id: str, draft_id: str) -> dict[str, Any]:
    return await client.mutate("POST", f"{_project_path(project_id)}/view-drafts/{quote(draft_id, safe='')}/discard",
                               project_id=project_id)
