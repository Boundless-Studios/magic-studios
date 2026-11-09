"""The stdio MCP server: thin, typed wrappers over `tools.py`, returning compact JSON.

Configured entirely by environment: `MAGIC_STUDIOS_URL` (default
`https://api.magicstudios.ai`) and `MAGIC_STUDIOS_API_KEY`.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from magic_video_studio_mcp import tools
from magic_video_studio_mcp.client import StudioClient

ProjectId = Annotated[str, Field(description="The project's id.")]
MotionId = Annotated[str, Field(description="The motion's id, e.g. 'idle'.")]

mcp = FastMCP(
    "magic-video-studio",
    host=os.environ.get("MAGIC_STUDIOS_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MAGIC_STUDIOS_MCP_PORT", "8766")),
    stateless_http=True,
)
_client: StudioClient | None = None


def client() -> StudioClient:
    """One client for the process, built on first use from the environment."""
    global _client
    if _client is None:
        _client = StudioClient.from_env()
    return _client


def _json(payload: Any) -> str:
    public_url = os.environ.get("MAGIC_STUDIOS_PUBLIC_URL", "").rstrip("/")
    def public_links(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: public_links(item) for key, item in value.items()}
        if isinstance(value, list):
            return [public_links(item) for item in value]
        if public_url and isinstance(value, str) and value.startswith("/local-assets/"):
            return public_url + value
        return value
    return json.dumps(public_links(payload), separators=(",", ":"), default=str)


@mcp.tool()
async def get_generation_config(worker_id: str | None = None) -> str:
    """Discover authorized machines, actual default settings and workload bounds.

    Call before generating. Pass returned settings unchanged to generate_motions
    to bind the worker and recipe. Older servers may return 404; do not infer
    GPU capabilities from that absence.

    `providers.higgsfield` reports whether the owner saved a Higgsfield API key.
    Its `settings` (present only once a key is saved) run generation on, and
    bill, the owner's own Higgsfield account; pass them unchanged, changing only
    `provider_parameters.higgsfield_model` to another listed model. Higgsfield is
    never used unless the request carries those settings.
    """
    return _json(await tools.get_generation_config(client(), worker_id))


@mcp.tool()
async def list_projects() -> str:
    """Every studio project the API key's owner has, newest activity first."""
    return _json(await tools.list_projects(client()))


@mcp.tool()
async def create_project(
    name: Annotated[str, Field(description="Display name for the new project.")],
) -> str:
    """Create an empty studio project."""
    return _json(await tools.create_project(client(), name))


@mcp.tool()
async def get_project(project_id: ProjectId) -> str:
    """A project's current state: revision, approvals, motions, and what to do next."""
    return _json(await tools.get_project(client(), project_id))


@mcp.tool()
async def upload_reference(
    project_id: ProjectId,
    path: Annotated[str, Field(description="Local path to the reference image.")],
) -> str:
    """Upload a reference image and return the asset key to generate a subject from."""
    return _json(await tools.upload_reference(client(), project_id, path))


@mcp.tool()
async def attach_movie_voice(
    movie_id: Annotated[str, Field(description="The movie's id.")],
    character_id: Annotated[str, Field(description="An existing cast character's id.")],
    path: Annotated[str, Field(description="Local path to an authorized 2–15s WAV/MP3.")],
) -> str:
    """Upload and attach a character voice reference, preserving the remaining cast."""
    return _json(await tools.attach_movie_voice(client(), movie_id, character_id, path))


MovieId = Annotated[str, Field(description="The movie's id.")]
ShotId = Annotated[str, Field(description="A shot's id within the movie.")]
RequestId = Annotated[str | None, Field(
    description="Omit for a new request. Reuse an id only to retry the exact same call; "
                "a repeated id is treated as already done.")]


@mcp.tool()
async def get_movie_capabilities() -> str:
    """What this account can do in movies: whether takes, opening images, planning and
    dialogue are available, and the shot length and batch limits. Check
    `dialogue_available` before planning dialogue or attaching voices."""
    return _json(await tools.get_movie_capabilities(client()))


@mcp.tool()
async def create_movie(name: Annotated[str, Field(description="Display name for the movie.")]) -> str:
    """Create an empty movie. Next: set_movie_cast, attach_movie_voice, set_movie_story."""
    return _json(await tools.create_movie(client(), name))


@mcp.tool()
async def get_movie(movie_id: MovieId) -> str:
    """A movie's full state: revision, cast, shots with takes, cut, cues, assembly, releases."""
    return _json(await tools.get_movie(client(), movie_id))


@mcp.tool()
async def set_movie_cast(
    movie_id: MovieId,
    characters: Annotated[list[dict[str, Any]], Field(
        description="Up to 8 characters: character_id, name, appearance, voice (a spoken "
                    "voice description), possessive ('their'|'her'|'his'), audition_line. "
                    "Extra fields from get_movie are ignored.")],
) -> str:
    """Replace the movie's cast. Attach voices with attach_movie_voice; a character that
    already has a voice keeps it."""
    return _json(await tools.set_movie_cast(client(), movie_id, characters))


@mcp.tool()
async def set_movie_story(
    movie_id: MovieId,
    shots: Annotated[list[dict[str, Any]], Field(
        description="Ordered shots: shot_id, name, prompt, opening_prompt, duration_seconds "
                    "(1.5-10, 0.5 steps), continuation_of, dialogue [{character_id, text, "
                    "delivery, pause_before, reaction}], soundscape. Extra fields from "
                    "get_movie are ignored.")],
    brief: Annotated[str | None, Field(description="Omit to keep the current brief.")] = None,
    style: Annotated[str | None, Field(description="Omit to keep the current style.")] = None,
    batch_size: Annotated[int | None, Field(ge=1, le=6)] = None,
    compute_target: Annotated[str | None, Field(
        description="'shared' or 'my_machines'; omit to keep the current one.")] = None,
) -> str:
    """Replace the shot list. Shots with dialogue become voiced H3 takes. Changing a shot
    makes its takes stale, and changing style makes every take stale, so omit style unless
    you mean to. The brief is only planning text and stales nothing."""
    return _json(await tools.set_movie_story(
        client(), movie_id, shots, brief, style, batch_size, compute_target))


@mcp.tool()
async def upload_movie_asset(
    movie_id: MovieId,
    path: Annotated[str, Field(description="Local png/jpeg/webp (openings) or wav/mp3 (cues).")],
) -> str:
    """Upload a local file into the movie and return its asset_key: images for
    set_shot_opening, WAV or MP3 for set_movie_soundtracks."""
    return _json(await tools.upload_movie_asset(client(), movie_id, path))


@mcp.tool()
async def set_shot_opening(movie_id: MovieId, shot_id: ShotId, asset_key: str) -> str:
    """Use an uploaded image as a shot's first frame."""
    return _json(await tools.set_shot_opening(client(), movie_id, shot_id, asset_key))


@mcp.tool()
async def build_movie(movie_id: MovieId, request_id: RequestId = None) -> str:
    """Generate every shot's take in order and assemble the cut. Follow with wait_for_movie."""
    return _json(await tools.build_movie(client(), movie_id, request_id))


@mcp.tool()
async def generate_takes(
    movie_id: MovieId,
    shot_ids: Annotated[list[str], Field(description="Up to 6 distinct shots.")],
    request_id: RequestId = None,
    select_when_ready: bool = True,
) -> str:
    """Generate one new take for each listed shot, then wait_for_movie. With select_when_ready,
    the new take is picked when done, which makes later continuation shots stale."""
    return _json(await tools.generate_takes(
        client(), movie_id, shot_ids, request_id, select_when_ready))


@mcp.tool()
async def select_movie_take(movie_id: MovieId, shot_id: ShotId, take_id: str) -> str:
    """Use a succeeded take for its shot. Later continuation shots become stale."""
    return _json(await tools.select_movie_take(client(), movie_id, shot_id, take_id))


@mcp.tool()
async def wait_for_movie(
    movie_id: MovieId,
    timeout_seconds: Annotated[float, Field(ge=1, le=7200)] = 600,
    interval_seconds: Annotated[float, Field(ge=1, le=300)] = 15,
) -> str:
    """Advance a movie with reconcile until nothing is running, something fails, or the
    timeout passes. Work only progresses while this (or the web app) is reconciling."""
    return _json(await tools.wait_for_movie(client(), movie_id, timeout_seconds, interval_seconds))


@mcp.tool()
async def set_movie_cut(
    movie_id: MovieId,
    clips: Annotated[list[dict[str, Any]], Field(
        description="Ordered clips: shot_id, take_id, in_seconds, out_seconds, "
                    "transition_seconds, mute.")],
) -> str:
    """Replace the whole cut. Clips are not muted unless mute is true (only build_movie
    mutes no-dialogue shots automatically)."""
    return _json(await tools.set_movie_cut(client(), movie_id, clips))


@mcp.tool()
async def set_movie_soundtracks(
    movie_id: MovieId,
    tracks: Annotated[list[dict[str, Any]], Field(
        description="Up to 8 cues: asset_key, offset_seconds, trim_start_seconds, gain_db, "
                    "end_seconds, fade_in_seconds, fade_out_seconds.")],
) -> str:
    """Replace the music/ambience cues mixed under the cut."""
    return _json(await tools.set_movie_soundtracks(client(), movie_id, tracks))


@mcp.tool()
async def assemble_movie(movie_id: MovieId, request_id: RequestId = None) -> str:
    """Render the current cut and cues. Follow with wait_for_movie."""
    return _json(await tools.assemble_movie(client(), movie_id, request_id))


@mcp.tool()
async def release_movie(movie_id: MovieId, name: str) -> str:
    """Publish the latest successful assembly as a named, immutable release."""
    return _json(await tools.release_movie(client(), movie_id, name))


@mcp.tool()
async def download_movie_release(
    movie_id: MovieId, release_id: str,
    destination: Annotated[str, Field(description="Local path to write the MP4 to.")],
) -> str:
    """Download a release MP4 and report its size and sha256."""
    return _json(await tools.download_movie_release(client(), movie_id, release_id, destination))


AudioSetId = Annotated[str, Field(description="The audio set's id.")]
CueId = Annotated[str, Field(description="A cue's id within the audio set.")]
CandidateId = Annotated[str, Field(description="A candidate's id within the audio set.")]


@mcp.tool()
async def get_audio_capabilities() -> str:
    """What this account can do with audio sets: free-sound search, uploads, loudness
    processing (not for public accounts), sending to movies, and the limits."""
    return _json(await tools.get_audio_capabilities(client()))


@mcp.tool()
async def create_audio_set(name: Annotated[str, Field(description="Display name for the set.")]) -> str:
    """Create an empty audio set (CC0-only). Next: set_audio_set to add cues."""
    return _json(await tools.create_audio_set(client(), name))


@mcp.tool()
async def get_audio_set(set_id: AudioSetId) -> str:
    """An audio set's full state: revision, cues with selections, candidates with
    licences, measurements and reviews, and releases."""
    return _json(await tools.get_audio_set(client(), set_id))


@mcp.tool()
async def set_audio_set(
    set_id: AudioSetId,
    cues: Annotated[list[dict[str, Any]], Field(
        description="Up to 64 cues: cue_id, name, kind ('line'|'sfx'|'ambience'|'music'), "
                    "prompt, text, target_seconds, loop, keep (variants to ship). Extra "
                    "fields from get_audio_set are ignored.")],
    name: Annotated[str | None, Field(description="Omit to keep the current name.")] = None,
    brief: Annotated[str | None, Field(description="Omit to keep the current brief.")] = None,
    licence_policy: Annotated[str | None, Field(
        description="'cc0' (no attribution duty) or 'cc0_cc_by' (CC-BY allowed, credited "
                    "in releases). Omit to keep the current policy.")] = None,
) -> str:
    """Replace the cues. Removing a cue removes its candidates; narrowing the licence
    policy drops CC-BY files from selections."""
    return _json(await tools.set_audio_set(client(), set_id, cues, name, brief, licence_policy))


@mcp.tool()
async def search_free_sounds(
    set_id: AudioSetId,
    query: Annotated[str, Field(description="What the sound is, e.g. 'glass crack'.")],
    min_seconds: Annotated[float, Field(ge=0, le=600)] = 0,
    max_seconds: Annotated[float, Field(gt=0, le=600)] = 120,
    page: Annotated[int, Field(ge=1, le=10)] = 1,
) -> str:
    """Search Freesound, filtered to the set's licence policy (CC0, plus CC-BY if allowed;
    never non-commercial). Nothing is stored; import a result with import_free_sound."""
    return _json(await tools.search_free_sounds(client(), set_id, query, min_seconds, max_seconds, page))


@mcp.tool()
async def import_free_sound(
    set_id: AudioSetId, cue_id: CueId,
    source_id: Annotated[str, Field(description="The Freesound sound number from search_free_sounds.")],
) -> str:
    """Download a free sound's preview into a cue as a candidate, with its licence recorded."""
    return _json(await tools.import_free_sound(client(), set_id, cue_id, source_id))


@mcp.tool()
async def upload_audio_candidate(
    set_id: AudioSetId, cue_id: CueId,
    path: Annotated[str, Field(description="Local WAV or MP3 the owner has the rights to.")],
) -> str:
    """Upload a local file into a cue as a candidate."""
    return _json(await tools.upload_audio_candidate(client(), set_id, cue_id, path))


@mcp.tool()
async def replace_audio_original(
    set_id: AudioSetId, candidate_id: CandidateId,
    path: Annotated[str, Field(description="Local WAV/MP3 original downloaded from Freesound.")],
) -> str:
    """Replace a Freesound preview candidate with its original (same length). It needs a
    new review afterwards."""
    return _json(await tools.replace_audio_original(client(), set_id, candidate_id, path))


@mcp.tool()
async def review_audio_candidate(
    set_id: AudioSetId, candidate_id: CandidateId,
    outcome: Literal["keep", "reject"],
    note: Annotated[str, Field(max_length=500)] = "",
    reason: Annotated[str, Field(max_length=80)] = "",
) -> str:
    """Keep or reject a candidate. The review records that an API key (not a person)
    gave it, and releases say so; leave listening verdicts to the owner when you can."""
    return _json(await tools.review_audio_candidate(client(), set_id, candidate_id, outcome, note, reason))


@mcp.tool()
async def select_audio_candidates(
    set_id: AudioSetId, cue_id: CueId,
    candidate_ids: Annotated[list[str], Field(description="Kept candidates, in order, at most the cue's keep.")],
) -> str:
    """Set the variants a cue ships."""
    return _json(await tools.select_audio_candidates(client(), set_id, cue_id, candidate_ids))


@mcp.tool()
async def get_audio_readiness(set_id: AudioSetId) -> str:
    """What blocks a release, whether it would need attribution, and how many files are previews."""
    return _json(await tools.get_audio_readiness(client(), set_id))


@mcp.tool()
async def release_audio_set(set_id: AudioSetId, name: str) -> str:
    """Freeze the selection into a named, immutable zip with manifest.json and CREDITS.md."""
    return _json(await tools.release_audio_set(client(), set_id, name))


@mcp.tool()
async def download_audio_release(
    set_id: AudioSetId, release_id: str,
    destination: Annotated[str, Field(description="Local path to write the zip to.")],
) -> str:
    """Download a release zip and report its size and sha256."""
    return _json(await tools.download_audio_release(client(), set_id, release_id, destination))


@mcp.tool()
async def send_audio_to_movie(
    set_id: AudioSetId, candidate_id: CandidateId,
    movie_id: Annotated[str, Field(description="One of your movies.")],
) -> str:
    """Add a kept candidate to a movie as a soundtrack cue at the start; shape it with
    set_movie_soundtracks. CC-BY sounds are refused: movie releases carry no credits yet."""
    return _json(await tools.send_audio_to_movie(client(), set_id, candidate_id, movie_id))


Asset3DId = Annotated[str, Field(description="The 3D asset's id.")]
RequestId = Annotated[str | None, Field(
    description="Reuse only to retry the exact same call; a repeated id returns the same run.")]


@mcp.tool()
async def get_3d_capabilities() -> str:
    """What 3D assets can do for this account. Each engine stage (`mesh`, `clean`)
    says whether it can run and, when not, why: its reason starts "not available yet"."""
    return _json(await tools.get_3d_capabilities(client()))


@mcp.tool()
async def generate_3d_mesh(
    asset_id: Asset3DId,
    resolution: Annotated[int | None, Field(description="512 or 1024 (default 1024).")] = None,
    seed: Annotated[int | None, Field(description="0 to 2147483647; random when omitted, and recorded.")] = None,
    request_id: RequestId = None,
) -> str:
    """Generate a mesh candidate from the asset's source pictures (a front picture is
    required) on the 3D engine. When the stage is not available this returns
    `available: false` with the server's reason and submits nothing. A run lands as a
    candidate after the asset is reconciled; inspect its gate, then keep or reject it."""
    return _json(await tools.generate_3d_mesh(client(), asset_id, resolution, seed, request_id))


@mcp.tool()
async def clean_3d_mesh(
    asset_id: Asset3DId,
    parent_id: Annotated[str, Field(description="A mesh candidate you kept or selected.")],
    quality_tier: Annotated[str | None, Field(
        description="'draft', 'standard', 'high' or 'ultra' (default 'standard'). Not with target_triangles.")] = None,
    target_triangles: Annotated[int | None, Field(
        description="1,000 to 500,000. Not with quality_tier.")] = None,
    request_id: RequestId = None,
) -> str:
    """Clean a kept mesh candidate on the 3D engine: one watertight, UV-mapped,
    floor-aligned body at a triangle budget. When the stage is not available this
    returns `available: false` with the server's reason and submits nothing."""
    return _json(await tools.clean_3d_mesh(
        client(), asset_id, parent_id, quality_tier, target_triangles, request_id))


@mcp.tool()
async def generate_subject(
    project_id: ProjectId,
    reference_keys: Annotated[
        list[str], Field(description="Asset keys from upload_reference.", min_length=1)
    ],
    guidance: Annotated[
        str | None, Field(description="Optional art direction for the subject.")
    ] = None,
) -> str:
    """Start subject generation. Returns the queued operation; poll with
    wait_for_operations."""
    return _json(
        await tools.generate_subject(client(), project_id, reference_keys, guidance)
    )


@mcp.tool()
async def wait_for_operations(
    project_id: ProjectId,
    timeout_s: Annotated[
        float, Field(description="Give up after this many seconds.", gt=0)
    ] = tools.DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> str:
    """Poll until nothing is in flight, settling finished work into the manifest.
    Recent failed or cancelled operations come back under `failed`; they do not
    keep the wait open."""
    return _json(await tools.wait_for_operations(client(), project_id, timeout_s))


@mcp.tool()
async def approve_subject(project_id: ProjectId) -> str:
    """Approve the current subject, acknowledging any findings."""
    return _json(await tools.approve_subject(client(), project_id))


@mcp.tool()
async def add_recommended_motions(project_id: ProjectId) -> str:
    """Add the recommended set: idle, walk, attack, guard, cast, hurt."""
    return _json(await tools.add_recommended_motions(client(), project_id))


@mcp.tool()
async def configure_sprite_layout(project_id: ProjectId, sprite_layout: tools.SpriteLayout) -> str:
    """Explicitly choose unassigned, side_view, or isometric organization. Existing art is
    never assigned, rotated, mirrored, or discarded automatically. Set up views next.
    """
    return _json(await tools.configure_sprite_layout(client(), project_id, sprite_layout))


@mcp.tool()
async def set_character_view(project_id: ProjectId, view_id: tools.ViewId,
                             name: str | None = None, reference: tools.ViewReferenceInput | None = None,
                             clear_reference: bool = False) -> str:
    """Add/update a declared supplied view. Omit reference to keep it; clear_reference
    explicitly clears it. Upload actual matching pixels with upload_view_reference_base64.
    Replacing a reference stales prior takes while retaining selections and history.
    """
    return _json(await tools.set_character_view(client(), project_id, view_id, name, reference, clear_reference))


@mcp.tool()
async def upload_view_reference_base64(project_id: ProjectId, view_id: tools.ViewId,
                                        content_base64: str, content_type: str = "image/png") -> str:
    """Upload actual client image bytes (PNG/JPEG/WebP, at most 10 MB) for a declared view.
    No remote filesystem path, inferred view, automatic rotation, or mirror. Returns the
    persisted reference revision, key, hash and dimensions in views. Next: add_view_motions.
    """
    return _json(await tools.upload_view_reference_base64(client(), project_id, view_id, content_base64, content_type))


@mcp.tool()
async def add_view_motions(project_id: ProjectId, view_id: tools.ViewId,
                            motions: Annotated[list[tools.ViewMotionInput] | None, Field(min_length=1, max_length=32)] = None) -> str:
    """Add preset/custom actions within a declared view using explicit durable motion IDs.
    Omit motions for {view}-idle/walk/attack. Existing exact definitions are skipped;
    conflicting IDs fail rather than overwrite. Supply concise action intent and actual
    view references; Studio compiles constraints. Walk is forward in place. Generate with
    the returned added_motion_ids or existing skipped_motion_ids. Completion is not visual acceptance.
    """
    return _json(await tools.add_view_motions(client(), project_id, view_id, motions))


@mcp.tool()
async def update_view_motion(project_id: ProjectId, motion_id: MotionId,
                              changes: tools.ViewMotionChanges) -> str:
    """Edit explicit fields of one view motion, preserving its remaining definition,
    view/reference, candidates and selection. View references are edited through the view.
    """
    return _json(await tools.update_view_motion(client(), project_id, motion_id, changes))


@mcp.tool()
async def generate_motions(
    project_id: ProjectId,
    motion_ids: Annotated[
        list[str] | None,
        Field(description="Motions to generate; omit for every motion on the project."),
    ] = None,
    settings: Annotated[dict[str, Any] | None, Field(description="Studio generation settings, including width, height, steps, seed, duration_seconds and provider_parameters such as framing_fill.")] = None,
    actions: Annotated[dict[str, str] | None, Field(description="Optional motion_id to short action text; uses the same preset compiler as the UI.")] = None,
    request_id: RequestId = None,
) -> str:
    """Queue motion generation; preserve request_id to resume the exact call without
    duplicate jobs. Partial success is normal: read failed too. Dispatched prompt/settings
    and immutable source/reference evidence are returned; completion is not visual acceptance.
    """
    return _json(await tools.generate_motions(client(), project_id, motion_ids, settings, actions, request_id))


@mcp.tool()
async def list_takes(project_id: ProjectId, motion_id: MotionId) -> str:
    """The generated takes (candidates) for one motion, and which is selected."""
    return _json(await tools.list_takes(client(), project_id, motion_id))


@mcp.tool()
async def select_take(
    project_id: ProjectId,
    motion_id: MotionId,
    candidate_id: Annotated[str, Field(description="The take to select.")],
) -> str:
    """Select a take for a motion. Every required motion needs one before approval."""
    return _json(await tools.select_take(client(), project_id, motion_id, candidate_id))


@mcp.tool()
async def inspect_candidate_frames(project_id: ProjectId, motion_id: MotionId, candidate_id: str,
                                   start: int = 0, limit: int = 8, stride: int = 1) -> list:
    """View up to 12 decoded native frames as images; inspect phase and edge contact remotely."""
    import base64
    from mcp.server.fastmcp.utilities.types import Image
    result = await tools.inspect_candidate_frames(client(), project_id, motion_id, candidate_id, start, limit, stride)
    frames = result.pop("frames")
    content = [_json({**result, "frames": [{k: v for k, v in f.items() if k not in {"image", "pose"}} for f in frames]})]
    for frame in frames:
        content.append(f"Source frame {frame['index']}")
        content.append(Image(data=base64.b64decode(frame["image"].split(",", 1)[1]), format="png"))
    return content


@mcp.tool()
async def suggest_motion_loop(project_id: ProjectId, motion_id: MotionId, candidate_id: str,
                               mode: Literal["idle", "walk", "attack"], target_frame_count: int = 12) -> str:
    """Propose a subtle idle loop, one walk stride, or complete attack. Returns source
    FPS/timebase, exact indices/timing and limitations without saving or selecting frames.
    Inspect the proposal, then explicitly save with create_frame_revision. A successful
    proposal does not establish visual acceptance; a frozen idle is not a valid loop.
    """
    return _json(await tools.suggest_motion_loop(client(), project_id, motion_id, candidate_id, mode, target_frame_count))


@mcp.tool()
async def create_frame_revision(project_id: ProjectId, motion_id: MotionId, source_candidate_id: str,
                                ordered_frames: list[int], frame_duration_ms: int,
                                loop_policy: str = "loop", target_frame_count: int | None = None,
                                cell_size: int | None = None, normalization: dict[str, Any] | None = None) -> str:
    """Save/select chosen source indices. Optional fixed scale+source/target anchor normalization requires count+cell size."""
    return _json(await tools.create_frame_revision(client(), project_id, motion_id, source_candidate_id,
        ordered_frames, frame_duration_ms, loop_policy, target_frame_count, cell_size, normalization))


@mcp.tool()
async def calibrate_motion_body(project_id: ProjectId, motion_id: MotionId, candidate_id: str,
                                source_frame: int, reference_candidate_id: str,
                                reference_motion_id: str | None = None, reference_frame: int = 0) -> str:
    """Measure one fixed body scale and anchor against a deliberately chosen same-view take.
    View motions require explicit reference_motion_id; legacy unassigned defaults to walk.
    Review the measured transform before saving.
    """
    return _json(await tools.calibrate_motion_body(client(), project_id, motion_id, candidate_id,
        source_frame, reference_candidate_id, reference_motion_id, reference_frame))


@mcp.tool()
async def derive_candidate_alpha(project_id: ProjectId, motion_id: MotionId, candidate_id: str) -> str:
    """Remove a green backdrop into a new transparent candidate before normalization/export."""
    return _json(await tools.derive_candidate_alpha(client(), project_id, motion_id, candidate_id))


@mcp.tool()
async def get_candidate_download(project_id: ProjectId, motion_id: MotionId, candidate_id: str,
                                 format: str = "png") -> str:
    """Get a PNG sheet, transparent WebM, or GIF download URL without a server-local file."""
    return _json(await tools.get_candidate_download(client(), project_id, motion_id, candidate_id, format))


@mcp.tool()
async def upload_subject_base64(project_id: ProjectId, image_base64: str, content_type: str = "image/png") -> str:
    """Upload and settle a prepared subject from remote image bytes (up to 10 MB)."""
    return _json(await tools.upload_subject_base64(client(), project_id, image_base64, content_type))


@mcp.tool()
async def run_quality(project_id: ProjectId) -> str:
    """Assess the selected set. Findings are warnings, never blockers."""
    return _json(await tools.run_quality(client(), project_id))


@mcp.tool()
async def approve_set(project_id: ProjectId) -> str:
    """Approve the motion set, acknowledging every finding present."""
    return _json(await tools.approve_set(client(), project_id))


@mcp.tool()
async def create_release(
    project_id: ProjectId,
    name: Annotated[str, Field(description="Release name, unique within the project.")],
) -> str:
    """Freeze the approved set into an immutable release."""
    return _json(await tools.create_release(client(), project_id, name))


@mcp.tool()
async def export_release(
    release_id: Annotated[str, Field(description="The release to export.")],
    profile: Annotated[
        str, Field(description="Export profile, e.g. 'png-json'.")
    ] = "png-json",
) -> str:
    """Build an export package for a release."""
    return _json(await tools.export_release(client(), release_id, profile))


@mcp.tool()
async def download_export(
    export_id: Annotated[str, Field(description="The export to download.")],
    dest_path: Annotated[str, Field(description="Local path to write the file to.")],
) -> str:
    """Download an export to a local file and report its sha256."""
    return _json(await tools.download_export(client(), export_id, dest_path))



TurnaroundId = Annotated[str, Field(description="The turnaround's id, 'turnaround-…'.")]
DraftId = Annotated[str, Field(description="The draft's id, 'draft-…'.")]
Heading = Annotated[Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW"], Field(description="A compass direction.")]


@mcp.tool()
async def start_turnaround(
    project_id: ProjectId,
    source_view_id: Annotated[str, Field(description="The view whose bound reference turns; it must face S.")],
    source_revision_id: Annotated[str, Field(description="That view's current reference revision_id.")],
    equipment: Annotated[list[dict[str, str]] | None, Field(description=(
        "Up to four one-sided items to keep attached: {item, attachment: hand|shoulder|hip|back|other, "
        "source_picture_side: left|right|unsided}. unsided only for back or other."))] = None,
    settings: Annotated[dict[str, Any] | None, Field(description=(
        "Omit to use the default machine; it must report features.last_frame on the standard profile."))] = None,
    request_id: RequestId = None,
) -> str:
    """Generate other directions from one image, step 1: turn a front (S) or front three-quarter (SE, SW) reference in place.

    Queues one closed-loop video. Check get_generation_config first: an available machine on
    h3-24gb-standard must report features.last_frame. Then reconcile and get_turnaround until
    it settles. Nothing here creates or binds a reference.
    """
    return _json(await tools.start_turnaround(client(), project_id, source_view_id, source_revision_id,
                                              equipment, settings, request_id))


@mcp.tool()
async def list_turnarounds(project_id: ProjectId) -> str:
    """Every turnaround in the project with its state, cards and saved picks."""
    return _json(await tools.list_turnarounds(client(), project_id))


@mcp.tool()
async def get_turnaround(project_id: ProjectId, turnaround_id: TurnaroundId) -> str:
    """One turnaround. Once settled: suggested_picks (a frame per heading, with its reason),
    placement_cards (what a correct picture of each heading shows), synthesized_parts, and
    the saved pick_set. Suggestions are proposals; inspect frames before trusting them."""
    return _json(await tools.get_turnaround(client(), project_id, turnaround_id))


@mcp.tool()
async def inspect_turnaround_frames(project_id: ProjectId, turnaround_id: TurnaroundId,
                                    start: int = 0, limit: int = 8, stride: int = 1) -> list:
    """View up to 12 decoded frames of a settled turnaround as images, to check the picks."""
    import base64
    from mcp.server.fastmcp.utilities.types import Image
    result = await tools.inspect_turnaround_frames(client(), project_id, turnaround_id, start, limit, stride)
    frames = result.pop("frames")
    content = [_json({**result, "frames": [{k: v for k, v in f.items() if k not in {"image", "pose"}} for f in frames]})]
    for frame in frames:
        content.append(f"Turnaround frame {frame['index']}")
        content.append(Image(data=base64.b64decode(frame["image"].split(",", 1)[1]), format="png"))
    return content


@mcp.tool()
async def save_turnaround_picks(
    project_id: ProjectId, turnaround_id: TurnaroundId,
    expected_pick_revision: Annotated[int, Field(description="The pick_revision you read; 0 before the first save.")],
    picks: Annotated[dict[str, dict[str, Any]], Field(description=(
        "One entry for each of the seven headings other than the source: {frame_index, "
        "confirm_out_of_region?, confirm_out_of_order?}."))],
) -> str:
    """Save the whole pick set. A pick outside its heading's region or out of turn order is
    refused unless that heading confirms it; an override lasts only for this revision. A
    stale revision is refused (409): read the turnaround again, never overwrite blindly."""
    return _json(await tools.save_turnaround_picks(client(), project_id, turnaround_id,
                                                   expected_pick_revision, picks))


@mcp.tool()
async def create_view_draft(project_id: ProjectId, turnaround_id: TurnaroundId,
                            pick_revision: Annotated[int, Field(description="The saved pick set's revision.")],
                            heading: Heading, request_id: RequestId = None) -> str:
    """Make a draft of one picked heading: the frame keyed and framed to 1024, with the
    placement card it will be checked against. A draft is never a reference by itself."""
    return _json(await tools.create_view_draft(client(), project_id, turnaround_id, pick_revision, heading,
                                               request_id))


@mcp.tool()
async def list_view_drafts(project_id: ProjectId) -> str:
    """Every draft with its image path, card, synthesized parts, latest check, and whether it
    is discarded, bound or bindable."""
    return _json(await tools.list_view_drafts(client(), project_id))


@mcp.tool()
async def check_view_draft(project_id: ProjectId, draft_id: DraftId, facing: bool, equipment: bool,
                           identity: bool) -> str:
    """Record the person's three-box check of a draft against its placement card: facing
    correct, equipment where the card says, same face and outfit with plausible synthesized
    parts. Only with the person's explicit go-ahead for this direction, after they have seen
    the draft; never tick boxes on your own judgement."""
    return _json(await tools.check_view_draft(client(), project_id, draft_id, facing, equipment, identity))


@mcp.tool()
async def discard_view_draft(project_id: ProjectId, draft_id: DraftId) -> str:
    """Discard a draft for good; it can never be bound, even by a plan previewed earlier.

    Discarding changes the project revision. A draft that is a direction's
    reference is refused until that reference is replaced."""
    return _json(await tools.discard_view_draft(client(), project_id, draft_id))

def main() -> None:
    transport = os.environ.get("MAGIC_STUDIOS_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("MCP transport must be stdio, streamable-http or sse")
    mcp.run(transport=transport)


@mcp.tool()
async def get_setup_capabilities() -> str:
    """Discover actual server setup versions and conventions before editing setup."""
    return _json(await tools.get_setup_capabilities(client()))


@mcp.tool()
async def get_setup(project_id: ProjectId) -> str:
    """Read the current revision and declarative setup, preserving stable identities."""
    return _json(await tools.get_setup(client(), project_id))


@mcp.tool()
async def preview_setup(project_id: ProjectId, expected_revision: int, desired_setup: dict[str, Any]) -> str:
    """Preview atomic setup changes, including explicit removals and heading changes.

    Submit only requested motions. Templates never add motions or launch generation.
    Reference bindings use verified existing project revision IDs, never storage keys.
    Omit removed views/motions from desired setup. Inspect exact impacts and blockers.
    A checked direction draft binds as {"revision_id": "draft-…"} on the view with the
    draft's own heading, only with the person's explicit go-ahead for that direction.
    """
    return _json(await tools.preview_setup(client(), project_id, expected_revision, desired_setup))


@mcp.tool()
async def apply_setup(project_id: ProjectId, expected_revision: int, plan_token: str,
                      confirmed_impacts: list[str] | None = None) -> str:
    """Apply the reviewed plan with explicitly confirmed impact IDs; never generates.

    A stale plan requires a fresh preview. Settle or explicitly cancel blocked jobs first.
    """
    return _json(await tools.apply_setup(client(), project_id, expected_revision, plan_token, confirmed_impacts))


@mcp.tool()
async def upload_setup_reference_base64(project_id: ProjectId, image_base64: str,
                                        content_type: str = "image/png") -> str:
    """Stage artwork (up to 10 MB) without changing current setup or generating.

    Use the returned verified revision ID in a preview_setup reference binding.
    One staged image may explicitly bind identity and original view on first setup.
    """
    return _json(await tools.upload_setup_reference_base64(client(), project_id, image_base64, content_type))


@mcp.tool()
async def abandon_setup_dispatch(project_id: ProjectId, operation_id: str,
        expected_revision: int, confirm_dispatch_stopped: bool = False) -> str:
    """Fence a stranded dispatch only after explicit confirmation that dispatch stopped.

    Missing or failed/cancelled operations only; active/completed jobs are blocked.
    This never resubmits generation. Refresh setup and preview any subsequent edit.
    """
    return _json(await tools.abandon_setup_dispatch(client(), project_id, operation_id,
        expected_revision, confirm_dispatch_stopped))


FlightId = Annotated[str, Field(description="The scene flight's id.")]
LegId = Annotated[str, Field(description="A leg's id within the flight.")]


@mcp.tool()
async def get_scene_flight_capabilities() -> str:
    """Whether this server can make scene flights at all (`generation_available`), the
    image types it accepts and the recipe: size, frame rate, steps and leg lengths.
    Check it, and `generation_available` from get_movie_capabilities, before a flight."""
    return _json(await tools.get_scene_flight_capabilities(client()))


@mcp.tool()
async def create_scene_flight(name: Annotated[str, Field(description="Display name.")]) -> str:
    """Create an empty scene flight: an ordered set of camera legs, each pinned to start
    on an origin image and end on a destination image. Next: add_scene_flight_image."""
    return _json(await tools.create_scene_flight(client(), name))


@mcp.tool()
async def get_scene_flight(flight_id: FlightId) -> str:
    """A flight's legs and takes (status, seed, seam diffs, video keys). Reading it also
    settles finished GPU jobs."""
    return _json(await tools.get_scene_flight(client(), flight_id))


@mcp.tool()
async def add_scene_flight_image(
    flight_id: FlightId,
    path: Annotated[str | None, Field(description="A png/jpeg/webp on the server's filesystem.")] = None,
    content_base64: Annotated[str | None, Field(description="Image bytes, base64; use from a remote client.")] = None,
    content_type: Annotated[Literal["image/png", "image/jpeg", "image/webp"], Field(
        description="Type of content_base64.")] = "image/png",
) -> str:
    """Add an origin or destination image (at most 20 MiB). Returns its image_key."""
    return _json(await tools.add_scene_flight_image(client(), flight_id, path, content_base64, content_type))


@mcp.tool()
async def set_scene_flight_legs(
    flight_id: FlightId,
    legs: Annotated[list[dict[str, Any]], Field(description=(
        "Ordered legs: leg_id, origin_image_key, destination_image_key, and either prompt "
        "or origin_name + destination_name (+ optional descriptions) to compose the default "
        "camera wording; optional negative_prompt, name, duration_seconds (5-15, 0.5 steps). "
        "Extra fields from get_scene_flight are ignored."))],
) -> str:
    """Replace the leg list. A changed leg's earlier takes become stale."""
    return _json(await tools.set_scene_flight_legs(client(), flight_id, legs))


@mcp.tool()
async def generate_scene_flight_take(
    flight_id: FlightId, leg_id: LegId, request_id: RequestId = None,
    seed: Annotated[int | None, Field(ge=0, description="Fixed seed; omit for a fresh one.")] = None,
) -> str:
    """Start a take of one leg on the machine's default recipe. Calling again re-rolls the
    leg; the newest successful take is selected. Follow with wait_for_scene_flight."""
    return _json(await tools.generate_scene_flight_take(client(), flight_id, leg_id, request_id, seed))


@mcp.tool()
async def generate_scene_flight_leg(
    origin_path: Annotated[str, Field(description="Origin image on the server's filesystem.")],
    destination_path: Annotated[str, Field(description="Destination image on the server's filesystem.")],
    prompt: str = "", origin_name: str = "", destination_name: str = "",
    origin_description: str = "", destination_description: str = "", negative_prompt: str = "",
    duration_seconds: Annotated[float, Field(ge=5, le=15, multiple_of=0.5)] = 5.0,
    seed: Annotated[int | None, Field(ge=0)] = None,
    flight_id: Annotated[str | None, Field(description="Append to this flight instead of a new one.")] = None,
) -> str:
    """One frame-pinned leg in one call: creates the flight (unless given), adds both images,
    appends the leg and starts a take. Give prompt, or both place names."""
    return _json(await tools.generate_scene_flight_leg(
        client(), origin_path, destination_path, prompt=prompt, origin_name=origin_name,
        destination_name=destination_name, origin_description=origin_description,
        destination_description=destination_description, negative_prompt=negative_prompt,
        duration_seconds=duration_seconds, seed=seed, flight_id=flight_id))


@mcp.tool()
async def select_scene_flight_take(flight_id: FlightId, leg_id: LegId, take_id: str) -> str:
    """Use a succeeded, current take for its leg."""
    return _json(await tools.select_scene_flight_take(client(), flight_id, leg_id, take_id))


@mcp.tool()
async def wait_for_scene_flight(
    flight_id: FlightId,
    timeout_seconds: Annotated[float, Field(ge=1, le=7200)] = 1800,
    interval_seconds: Annotated[float, Field(ge=1, le=300)] = 15,
) -> str:
    """Poll the flight until no take or chain is running, or the timeout passes."""
    return _json(await tools.wait_for_scene_flight(client(), flight_id, timeout_seconds, interval_seconds))


@mcp.tool()
async def chain_scene_flight(flight_id: FlightId, request_id: RequestId = None) -> str:
    """Join every leg's selected take in order with hard cuts (768x432, 24 fps). Adjacent
    legs must share their boundary image."""
    return _json(await tools.chain_scene_flight(client(), flight_id, request_id))


@mcp.tool()
async def download_scene_flight_video(
    flight_id: FlightId,
    destination: Annotated[str, Field(description="Local path to write the MP4 to.")],
    leg_id: Annotated[str | None, Field(description="A leg; omit for the chained flight.")] = None,
    take_id: Annotated[str | None, Field(description="A take; omit for the leg's selected take.")] = None,
) -> str:
    """Download a leg take or the chain and report its size and sha256."""
    return _json(await tools.download_scene_flight_video(client(), flight_id, destination, leg_id, take_id))


if __name__ == "__main__":
    main()
