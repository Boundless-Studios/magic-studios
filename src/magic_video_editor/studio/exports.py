"""Deterministic, engine-neutral release packages."""

import asyncio
import io
import json
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image, ImageChops, ImageOps
from PIL.PngImagePlugin import PngInfo

from magic_video_editor.contracts import (
    AssetNotFound,
    AssetStorageUnavailable,
    AssetStore,
    AssetTooLarge,
)
from magic_video_editor.studio.models import (
    AnimationType,
    ExportProfile,
    ExportRecord,
    ExternalSubject,
    Facing,
    FrameSlot,
    LoopPolicy,
    MotionCandidate,
    NormalizationSpec,
    MotionSpecification,
    MotionState,
    Release,
)
from magic_video_editor.studio.presets import PRESET_DEFINITIONS
from magic_video_editor.plugins.ffmpeg import alpha_video_decoder_args
from magic_video_editor.targets import (
    NeutralExternalSubject,
    NeutralMotion,
    NeutralPackage,
    TargetRegistry,
)

PNG_JSON_V1 = ExportProfile(name="png-json", version="1")
PNG_JSON_V2 = ExportProfile(name="png-json", version="2")
GAIA_V1 = ExportProfile(name="gaia", version="1")
_GAIA_SCHEMA_VERSION = 4
MAX_EXPORT_SHEET_BYTES = 100_000_000
MAX_EXPORT_TOTAL_BYTES = 500_000_000
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ReleaseAssetInvalid(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ImmutableExportCollision(RuntimeError):
    """An immutable export key contains absent or different bytes."""


class ExportStorageUnavailable(RuntimeError):
    """Asset storage failed outside a deterministic input error."""


class ReleaseExporter:
    def __init__(
        self,
        assets: AssetStore,
        *,
        max_sheet_bytes: int = MAX_EXPORT_SHEET_BYTES,
        max_total_bytes: int = MAX_EXPORT_TOTAL_BYTES,
        registry: TargetRegistry | None = None,
    ) -> None:
        self._assets = assets
        self._max_sheet_bytes = max_sheet_bytes
        self._max_total_bytes = max_total_bytes
        self._registry = (
            registry if registry is not None else TargetRegistry.from_entry_points()
        )
        self._records: dict[str, ExportRecord] = {}

    def _target_profile(self, system: str) -> ExportProfile | None:
        adapter = self._registry.get(system)
        if adapter is None:
            return None
        return ExportProfile(
            name=adapter.descriptor.system, version=adapter.descriptor.package_version
        )

    async def export(self, release: Release, *, profile: ExportProfile) -> ExportRecord:
        target_profile = self._target_profile(profile.name)
        if (
            profile not in (PNG_JSON_V1, PNG_JSON_V2, GAIA_V1)
            and profile != target_profile
        ):
            raise ValueError("unsupported export profile")
        if profile.name != PNG_JSON_V1.name and any(motion.specification.view_id is not None for motion in release.manifest.motions):
            raise ValueError("target export does not support view-scoped sprite sets; use png-json")
        export_id = sha256(
            f"{release.release_digest}{profile.name}{profile.version}".encode()
        ).hexdigest()
        existing = self._records.get(export_id)
        if existing is not None:
            await self.verify_published(existing)
            return existing
        entries: dict[str, bytes] = {}
        geometries: dict[str, dict[str, int | None]] = {}
        neutral_motions: list[NeutralMotion] = []
        total = 0
        registered_pivots = resolve_registered_pivots(release.manifest.motions)
        for motion in sorted(release.manifest.motions, key=lambda item: item.motion_id):
            candidate = motion.selected_candidate
            if candidate is None:
                continue
            try:
                source = await self._assets.read_limited(
                    candidate.artifact_key, self._max_sheet_bytes
                )
            except AssetTooLarge as exc:
                raise ReleaseAssetInvalid(
                    "release_asset_too_large",
                    "selected motion asset exceeds export limit",
                ) from exc
            except AssetNotFound as exc:
                raise ReleaseAssetInvalid(
                    "release_asset_missing", "selected motion asset is missing"
                ) from exc
            except AssetStorageUnavailable as exc:
                raise ExportStorageUnavailable("export storage unavailable") from exc
            if sha256(source).hexdigest() != candidate.sha256:
                raise ReleaseAssetInvalid(
                    "release_asset_digest_mismatch",
                    "selected motion asset does not match signed release",
                )
            metadata = candidate.metadata
            assert metadata is not None
            if source.startswith(b"\x89PNG\r\n\x1a\n"):
                sheet = source
                sheet_width = metadata.width
                frame_width = metadata.width // metadata.frame_count
            else:
                sheet = await _render_video_sprite_sheet(source, metadata.frame_count)
                sheet_width = (
                    metadata.packing_estimate.width
                    if metadata.packing_estimate is not None
                    else metadata.width * metadata.frame_count
                )
                frame_width = metadata.width
            pivot_x, pivot_y = registered_pivots.get(
                candidate.candidate_id, (metadata.pivot_x, metadata.pivot_y))
            if pivot_x is not None and pivot_y is not None:
                sheet = register_sheet_pivot(sheet, pivot_x, pivot_y)
            ordered = candidate.effective_ordered_frames
            packed_frame_count = metadata.frame_count
            if candidate.ordered_frames is not None:
                sheet = _pack_sheet(sheet, frame_width, metadata.height, ordered)
                packed_frame_count = sum(slot.hold for slot in ordered)
                sheet_width = frame_width * packed_frame_count
            if candidate.facing is Facing.LEFT:
                sheet = _mirror_sheet(sheet, frame_width)
            geometries[motion.motion_id] = {
                "frame_count": packed_frame_count,
                "frame_width": frame_width,
                "frame_height": metadata.height,
                "sheet_width": sheet_width,
                "sheet_height": metadata.height,
                "baseline_y": metadata.baseline_y,
                "pivot_x": (
                    frame_width - pivot_x
                    if candidate.facing is Facing.LEFT and pivot_x is not None
                    else pivot_x
                ),
                "pivot_y": pivot_y,
            }
            total += len(sheet)
            if total > self._max_total_bytes:
                raise ValueError("export inputs exceed total byte limit")
            sheet_member_path = f"motions/{_safe_slot(motion.motion_id)}.png"
            entries[sheet_member_path] = sheet
            neutral_motions.append(
                _neutral_motion(
                    motion=motion,
                    candidate=candidate,
                    geometry=geometries[motion.motion_id],
                    sheet_member_path=sheet_member_path,
                    sheet_bytes=sheet,
                )
            )
        # manifest.json is always the png-json-shaped payload, embedded as-is inside
        # a gaia- or target-profile package too; only a direct png-json request
        # picks its own
        png_json_profile = profile if profile.name == PNG_JSON_V1.name else PNG_JSON_V1
        entries["manifest.json"] = _canonical_json(
            _package_manifest(release, geometries, profile=png_json_profile)
        )
        total += len(entries["manifest.json"])
        if target_profile is not None and profile == target_profile:
            adapter = self._registry.get(profile.name)
            assert adapter is not None
            package = NeutralPackage(
                release_id=release.release_id,
                project_id=release.project_id,
                manifest_digest=release.manifest_digest,
                release_digest=release.release_digest,
                motions=tuple(neutral_motions),
                external_subject=_neutral_external_subject(
                    release.manifest.external_subject
                ),
            )
            for member_name, member_bytes in adapter.export_files(package).items():
                entries[f"{profile.name}/{member_name}"] = member_bytes
                total += len(member_bytes)
        if total > self._max_total_bytes:
            raise ValueError("export inputs exceed total byte limit")
        content = _archive(entries)
        digest = sha256(content).hexdigest()
        key = f"exports/studio/{release.project_id}/{export_id}.zip"
        try:
            created = await self._assets.write_once(key, content, "application/zip")
        except AssetStorageUnavailable as exc:
            raise ExportStorageUnavailable("export storage unavailable") from exc
        if not created:
            try:
                existing_content = await self._assets.read_limited(key, len(content))
            except (AssetNotFound, AssetTooLarge) as exc:
                raise ImmutableExportCollision(
                    "immutable export object is missing or corrupt"
                ) from exc
            except AssetStorageUnavailable as exc:
                raise ExportStorageUnavailable("export storage unavailable") from exc
            if existing_content != content:
                raise ImmutableExportCollision(
                    "immutable export object contains different bytes"
                )
        completed_at = datetime.now(UTC)
        record = ExportRecord(
            export_id=export_id,
            release_id=release.release_id,
            profile_name=profile.name,
            profile_version=profile.version,
            artifact_key=key,
            sha256=digest,
            size_bytes=len(content),
            created_at=completed_at,
            completed_at=completed_at,
        )
        self._records[export_id] = record
        return record

    def get(self, export_id: str) -> ExportRecord | None:
        return self._records.get(export_id)

    async def verify_published(self, record: ExportRecord) -> None:
        try:
            content = await self._assets.read_limited(
                record.artifact_key, record.size_bytes
            )
        except (AssetNotFound, AssetTooLarge) as exc:
            raise ImmutableExportCollision(
                "cached immutable export object is missing or corrupt"
            ) from exc
        except AssetStorageUnavailable as exc:
            raise ExportStorageUnavailable("export storage unavailable") from exc
        if (
            len(content) != record.size_bytes
            or sha256(content).hexdigest() != record.sha256
        ):
            raise ImmutableExportCollision(
                "cached immutable export object failed integrity verification"
            )


def _package_manifest(
    release: Release,
    geometries: dict[str, dict[str, int | None]],
    *,
    profile: ExportProfile = PNG_JSON_V1,
) -> dict[str, object]:
    subject = release.manifest.subject.current_revision
    if subject is None:
        raise ValueError("release has no subject")
    motions: list[dict[str, object]] = []
    for motion in sorted(release.manifest.motions, key=lambda item: item.motion_id):
        candidate = motion.selected_candidate
        if candidate is None:
            continue
        if candidate.metadata is None or candidate.duration_ms is None:
            raise ValueError("selected motion lacks export metadata")
        ordered = candidate.effective_ordered_frames
        total_hold = sum(slot.hold for slot in ordered)
        motions.append(
            {
                "artifact_sha256": candidate.sha256,
                "candidate_id": candidate.candidate_id,
                "direction": motion.specification.direction,
                "duration_ms": (
                    candidate.frame_duration_ms * total_hold
                    if candidate.frame_duration_ms is not None
                    else candidate.duration_ms
                ),
                "event_markers": [],
                "facing": candidate.facing.value,
                "frame_geometry": geometries[motion.motion_id],
                "holds": [slot.hold for slot in ordered],
                "loop": (
                    candidate.loop_policy or motion.specification.loop_policy
                ).value,
                "motion_id": motion.motion_id,
                "ordered_frames": [slot.index for slot in ordered],
                "semantic_slot": motion.specification.semantic_slot,
                **({"view_id": str(motion.specification.view_id),
                    **({"view_heading": motion.specification.view_heading.value if motion.specification.view_heading else None,
                        "setup_version": motion.specification.setup_version} if motion.specification.setup_version else {}),
                    "view_reference": motion.specification.view_reference.model_dump(mode="json") if motion.specification.view_reference else None,
                    "action": motion.specification.action, "preset": motion.specification.preset.value if motion.specification.preset else None,
                    "kind": motion.specification.kind.value}
                   if motion.specification.view_id is not None else {}),
                "sheet": f"motions/{motion.motion_id}.png",
            }
        )
    return {
        **({"sprite_layout": release.manifest.sprite_layout.value,
            "views": [view.model_dump(mode="json") for view in release.manifest.views]}
           if release.manifest.views else {}),
        "format": {"name": profile.name, "version": profile.version},
        "manifest_digest": release.manifest_digest,
        "release_digest": release.release_digest,
        "project_id": release.project_id,
        "quality": {
            "policy_versions": list(release.quality_policy_versions),
            "set": release.set_approval.model_dump(mode="json", exclude_none=True),
            "subject": release.manifest.subject.approval.model_dump(
                mode="json", exclude_none=True
            ),
            "waivers": list(release.set_approval.waived_finding_ids),
        },
        "release_id": release.release_id,
        "source_revision": release.source_revision,
        "subject": {
            "artifact_sha256": subject.sha256,
            "height": subject.height,
            "revision_id": subject.revision_id,
            "width": subject.width,
        },
        "motions": motions,
    }


# T19: resample a candidate to a chosen frame count / cell size (parity with the
# reference game's video_lab_animation.py CELL_SIZE_CHOICES packing).
RESAMPLE_FRAME_COUNT_CHOICES = (4, 8, 12, 16, 32)
RESAMPLE_CELL_SIZE_CHOICES = (256, 512, 1024)


def split_sheet_into_tiles(sheet: bytes, frame_count: int) -> list[bytes]:
    """Raw per-frame PNG tiles in stored (unordered) index order."""
    with Image.open(io.BytesIO(sheet)) as opened:
        source = opened.convert("RGBA")
        frame_width = source.width // frame_count
        tiles: list[bytes] = []
        for index in range(frame_count):
            box = (index * frame_width, 0, (index + 1) * frame_width, source.height)
            buffer = io.BytesIO()
            source.crop(box).save(buffer, format="PNG")
            tiles.append(buffer.getvalue())
        return tiles


def expand_ordered_sequence(
    tiles: list[bytes], ordered_frames: tuple[FrameSlot, ...]
) -> list[bytes]:
    """Playback order after reorder/holds, as a flat list of frame tiles."""
    sequence: list[bytes] = []
    for slot in ordered_frames:
        sequence.extend([tiles[slot.index]] * slot.hold)
    return sequence


def even_sample_with_padding[T](sequence: list[T], target_count: int) -> list[T]:
    """Evenly sample `target_count` items across `sequence`. A sequence shorter than
    `target_count` pads by repeating its final item -- ported reading of the
    reference game's `_pad_frame_paths`: a source that yields fewer frames than
    requested is a held final pose, not an error."""
    if not sequence:
        raise ValueError("cannot resample an empty frame sequence")
    if target_count <= 0:
        raise ValueError("target frame count must be positive")
    if target_count <= len(sequence):
        if target_count == 1:
            return [sequence[0]]
        return [
            sequence[round(index * (len(sequence) - 1) / (target_count - 1))]
            for index in range(target_count)
        ]
    padded = list(sequence) + [sequence[-1]] * (target_count - len(sequence))
    return padded[:target_count]


def pack_cell_sheet(
    frames: list[bytes], cell_size: int, *, normalization: NormalizationSpec | None = None
) -> bytes:
    """Scale each frame to fit inside a transparent `cell_size` square (aspect
    preserved, centered) and pack them into one horizontal strip."""
    if normalization is not None:
        from magic_video_editor.studio.normalization import pack_normalized_sheet

        return pack_normalized_sheet(frames, cell_size, normalization)
    cells: list[Image.Image] = []
    for frame_bytes in frames:
        with Image.open(io.BytesIO(frame_bytes)) as opened:
            frame = opened.convert("RGBA")
        fitted = ImageOps.contain(frame, (cell_size, cell_size))
        cell = Image.new("RGBA", (cell_size, cell_size), (0, 0, 0, 0))
        cell.paste(
            fitted,
            ((cell_size - fitted.width) // 2, (cell_size - fitted.height) // 2),
        )
        cells.append(cell)
    packed = Image.new("RGBA", (cell_size * len(cells), cell_size), (0, 0, 0, 0))
    for position, cell in enumerate(cells):
        packed.paste(cell, (position * cell_size, 0))
    buffer = io.BytesIO()
    png_info = PngInfo()
    png_info.add_text("studio_frame_count", str(len(cells)))
    packed.save(buffer, format="PNG", pnginfo=png_info)
    return buffer.getvalue()


async def render_candidate_sheet(source: bytes, candidate: MotionCandidate) -> bytes:
    """Build a packed PNG strip honoring order/holds/facing (T20) -- the same code path
    `ReleaseExporter.export` uses for a released motion, callable standalone so a
    candidate's own download route does not require a release."""
    metadata = candidate.metadata
    assert metadata is not None
    if source.startswith(b"\x89PNG\r\n\x1a\n"):
        sheet = source
        frame_width = metadata.width // metadata.frame_count
    else:
        sheet = await _render_video_sprite_sheet(source, metadata.frame_count)
        frame_width = metadata.width
    if candidate.ordered_frames is not None:
        sheet = _pack_sheet(
            sheet, frame_width, metadata.height, candidate.effective_ordered_frames
        )
    if candidate.facing is Facing.LEFT:
        sheet = _mirror_sheet(sheet, frame_width)
    return sheet


def candidate_playback_frame_count(candidate: MotionCandidate) -> int:
    """Frame count of the strip `render_candidate_sheet` produces for this candidate."""
    assert candidate.metadata is not None
    if candidate.ordered_frames is not None:
        return sum(slot.hold for slot in candidate.effective_ordered_frames)
    return candidate.metadata.frame_count


_NEUTRAL_LOOP_MODE_BY_POLICY: dict[LoopPolicy, str] = {
    LoopPolicy.LOOP: "loop",
    LoopPolicy.ONCE: "once",
    # No target adapter seen so far has a native ping_pong loop mode; the
    # closest native behavior is a loop, with the studio-only detail carried
    # separately as `NeutralMotion.ping_pong` rather than lost silently.
    LoopPolicy.PING_PONG: "loop",
}


def _neutral_animation_type(specification: MotionSpecification) -> AnimationType:
    definition = (
        PRESET_DEFINITIONS.get(specification.preset) if specification.preset else None
    )
    return (
        definition.animation_type if definition is not None else AnimationType.MOVEMENT
    )


def _neutral_motion(
    *,
    motion: MotionState,
    candidate: MotionCandidate,
    geometry: dict[str, int | None],
    sheet_member_path: str,
    sheet_bytes: bytes,
) -> NeutralMotion:
    """Project one released motion into the studio's own neutral vocabulary --
    everything a target adapter needs, nothing target-specific."""
    if candidate.metadata is None:
        raise ValueError("selected motion lacks export metadata")
    ordered = candidate.effective_ordered_frames
    held_frame_indexes = tuple(slot.index for slot in ordered for _ in range(slot.hold))
    frame_width = geometry["frame_width"]
    frame_height = geometry["frame_height"]
    assert frame_width is not None and frame_height is not None
    loop_policy = candidate.loop_policy or motion.specification.loop_policy
    total_ms = (
        candidate.frame_duration_ms * len(held_frame_indexes)
        if candidate.frame_duration_ms is not None
        else candidate.duration_ms
    )
    frame_duration_ms = (
        candidate.frame_duration_ms
        if candidate.frame_duration_ms is not None
        else max(1, round((total_ms or 1) / max(1, len(held_frame_indexes))))
    )
    return NeutralMotion(
        motion_id=motion.motion_id,
        semantic_slot=motion.specification.semantic_slot,
        animation_type=_neutral_animation_type(motion.specification).value,
        candidate_id=candidate.candidate_id,
        sha256=candidate.sha256,
        frame_duration_ms=frame_duration_ms,
        frames=held_frame_indexes,
        columns=len(held_frame_indexes),
        frame_width=frame_width,
        frame_height=frame_height,
        loop_mode=_NEUTRAL_LOOP_MODE_BY_POLICY[loop_policy],
        ping_pong=loop_policy is LoopPolicy.PING_PONG,
        sheet_member_path=sheet_member_path,
        sheet_bytes=sheet_bytes,
        pivot_x=geometry.get("pivot_x"),
        pivot_y=geometry.get("pivot_y"),
    )


def _neutral_external_subject(
    external_subject: ExternalSubject | None,
) -> NeutralExternalSubject | None:
    if external_subject is None:
        return None
    return NeutralExternalSubject(
        entity_kind=external_subject.entity_kind,
        entity_id=external_subject.entity_id,
        display_name=external_subject.display_name,
    )


def _pack_sheet(
    sheet: bytes,
    frame_width: int,
    frame_height: int,
    ordered_frames: tuple[FrameSlot, ...],
) -> bytes:
    """Slice a source row-strip sheet and rebuild it in playback order, repeating each
    frame `hold` times, so a saved reorder/hold edit is visible in the exported pixels."""
    with Image.open(io.BytesIO(sheet)) as opened:
        source = opened.convert("RGBA")
        tiles: list[Image.Image] = []
        for slot in ordered_frames:
            box = (
                slot.index * frame_width,
                0,
                (slot.index + 1) * frame_width,
                frame_height,
            )
            tile = source.crop(box)
            tiles.extend([tile] * slot.hold)
        packed = Image.new(
            "RGBA", (frame_width * len(tiles), frame_height), (0, 0, 0, 0)
        )
        for position, tile in enumerate(tiles):
            packed.paste(tile, (position * frame_width, 0))
        buffer = io.BytesIO()
        info = _sheet_png_info(opened.info, len(tiles))
        packed.save(buffer, format="PNG", pnginfo=info)
        return buffer.getvalue()


# Same key/similarity/blend as plugins.ffmpeg.CHROMAKEY_FILTER (ported from the
# reference game's video_lab_animation.py:208), approximated with Pillow's C-level ops instead of a
# numpy dependency: a Chebyshev (max-channel) distance from the key color stands in
# for ffmpeg's UV-plane distance, thresholded through the same similarity/blend band.
CHROMAKEY_COLOR = (0x00, 0xB1, 0x40)
CHROMAKEY_SIMILARITY = 0.10
CHROMAKEY_BLEND = 0.08


def chromakey_sheet(
    sheet: bytes,
    *,
    key: tuple[int, int, int] = CHROMAKEY_COLOR,
    similarity: float = CHROMAKEY_SIMILARITY,
    blend: float = CHROMAKEY_BLEND,
) -> bytes:
    """Key out a flat backdrop color across every tile of a PNG strip, returning a
    transparent RGBA PNG. Applied whole-strip: tile boundaries do not matter to a
    per-pixel color threshold."""
    similarity_threshold = round(255 * similarity)
    blend_threshold = round(255 * (similarity + blend))

    def alpha_curve(distance: int) -> int:
        if distance <= similarity_threshold:
            return 0
        if blend_threshold <= similarity_threshold or distance >= blend_threshold:
            return 255
        return round(
            255
            * (distance - similarity_threshold)
            / (blend_threshold - similarity_threshold)
        )

    with Image.open(io.BytesIO(sheet)) as opened:
        # T12: a derived sheet must keep the source's declared frame count, or
        # inspect_sprite_png measures the re-saved static PNG as a single
        # frame (Pillow's own n_frames is always 1 for a static strip).
        source_frame_count = opened.info.get("studio_frame_count")
        rgba = opened.convert("RGBA")
        red, green, blue, alpha = rgba.split()
        key_layer = Image.new("RGB", rgba.size, key)
        key_red, key_green, key_blue = key_layer.split()
        distance = ImageChops.lighter(
            ImageChops.lighter(
                ImageChops.difference(red, key_red),
                ImageChops.difference(green, key_green),
            ),
            ImageChops.difference(blue, key_blue),
        )
        mask = distance.point(alpha_curve)
        rgba.putalpha(ImageChops.multiply(alpha, mask))
        buffer = io.BytesIO()
        png_info = None
        if source_frame_count is not None:
            png_info = PngInfo()
            png_info.add_text("studio_frame_count", str(source_frame_count))
        rgba.save(buffer, format="PNG", pnginfo=png_info)
        return buffer.getvalue()


def _mirror_sheet(sheet: bytes, frame_width: int) -> bytes:
    """Mirror every tile in place. Mirroring the whole strip would also reverse the
    playback order, which is a different edit from facing the other way."""
    with Image.open(io.BytesIO(sheet)) as opened:
        source = opened.convert("RGBA")
        mirrored = Image.new("RGBA", source.size, (0, 0, 0, 0))
        for left in range(0, source.width, frame_width):
            tile = source.crop((left, 0, left + frame_width, source.height))
            mirrored.paste(ImageOps.mirror(tile), (left, 0))
        buffer = io.BytesIO()
        info_values = dict(opened.info)
        if "studio_pivot_x" in info_values:
            info_values["studio_pivot_x"] = str(frame_width - int(info_values["studio_pivot_x"]))
        info = _sheet_png_info(info_values, source.width // frame_width)
        mirrored.save(buffer, format="PNG", pnginfo=info)
        return buffer.getvalue()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


async def _render_video_sprite_sheet(content: bytes, frame_count: int) -> bytes:
    def render() -> bytes:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                *alpha_video_decoder_args(content),
                "-i",
                "pipe:0",
                "-vf",
                f"tile={frame_count}x1",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            input=content,
            capture_output=True,
            check=True,
            timeout=60,
        )
        if not completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("video export did not produce a PNG sprite sheet")
        return completed.stdout

    try:
        return await asyncio.to_thread(render)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseAssetInvalid(
            "release_asset_decode_failed",
            "selected motion video could not be packed as a PNG sprite sheet",
        ) from exc


def _archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = ZipInfo(name, _ZIP_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return buffer.getvalue()


def _safe_slot(value: str) -> str:
    if not value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("unsafe motion path")
    if "/" in value or "\\" in value:
        raise ValueError("unsafe motion path")
    return value


def _sheet_png_info(values: dict, frame_count: int) -> PngInfo:
    info = PngInfo()
    info.add_text("studio_frame_count", str(frame_count))
    for key in ("studio_pivot_x", "studio_pivot_y", "studio_normalization", "studio_matte_version"):
        if key in values:
            info.add_text(key, str(values[key]))
    return info


def resolve_candidate_pivot(
    motions: tuple[MotionState, ...], candidate: MotionCandidate,
) -> tuple[int, int] | None:
    """Validate one immutable take and its references, independent of selections.

    Selected calibrations may register this take's anchor, but only calibrations
    pointing into its reference chain are relevant to this download.
    """
    available = {item.candidate_id: item for motion in motions for item in motion.candidates}
    relevant = {}
    pending = [candidate]
    while pending:
        item = pending.pop()
        if item.candidate_id in relevant:
            continue
        relevant[item.candidate_id] = item
        spec = item.recipe.normalization if item.recipe is not None else None
        if spec is not None and spec.reference_candidate_id is not None:
            reference = available.get(spec.reference_candidate_id)
            if reference is None or reference.sha256 != spec.reference_sha256:
                raise ValueError("calibration reference is not the exact immutable candidate; recalibrate before export")
            pending.append(reference)
    # Only incoming selected edges into the requested take/reference chain can
    # contribute an anchor. Unrelated stale selections cannot invalidate a take.
    reference_ids = set(relevant)
    for motion in motions:
        selected = motion.selected_candidate
        spec = selected.recipe.normalization if selected is not None and selected.recipe is not None else None
        if spec is not None and spec.reference_candidate_id in reference_ids:
            relevant[selected.candidate_id] = selected
    return _resolve_registered_candidate_pivots(relevant, "immutable").get(candidate.candidate_id)


def resolve_registered_pivots(motions: tuple[MotionState, ...]) -> dict[str, tuple[int, int]]:
    """Resolve calibration anchors only against the exact selected reference bytes."""
    selected = {m.selected_candidate.candidate_id: m.selected_candidate
                for m in motions if m.selected_candidate is not None}
    return _resolve_registered_candidate_pivots(selected, "selected")


def _resolve_registered_candidate_pivots(
    selected: dict[str, MotionCandidate], reference_kind: str,
) -> dict[str, tuple[int, int]]:
    pivots = {}
    for candidate in selected.values():
        if candidate.metadata and candidate.metadata.pivot_x is not None and candidate.metadata.pivot_y is not None:
            pivots[candidate.candidate_id] = (candidate.metadata.pivot_x, candidate.metadata.pivot_y)
    for candidate in selected.values():
        spec = candidate.recipe.normalization if candidate.recipe is not None else None
        if spec is None or spec.reference_candidate_id is None:
            continue
        reference = selected.get(spec.reference_candidate_id)
        if reference is None or reference.sha256 != spec.reference_sha256:
            raise ValueError(f"calibration reference is not the exact {reference_kind} candidate; recalibrate before export")
        pivot = (round(spec.target_anchor_x), round(spec.target_anchor_y))
        previous = pivots.get(reference.candidate_id)
        if previous is not None and previous != pivot:
            raise ValueError("selected calibrations disagree on the reference pivot; recalibrate before export")
        pivots[reference.candidate_id] = pivot
    return pivots


def register_sheet_pivot(sheet: bytes, pivot_x: int, pivot_y: int) -> bytes:
    """Write export-only registration metadata without altering source or pixel values."""
    with Image.open(io.BytesIO(sheet)) as opened:
        info = PngInfo()
        for key, value in opened.info.items():
            if isinstance(value, str) and key.startswith("studio_"):
                info.add_text(key, value)
        info.add_text("studio_pivot_x", str(pivot_x))
        info.add_text("studio_pivot_y", str(pivot_y))
        buffer = io.BytesIO()
        opened.save(buffer, format="PNG", pnginfo=info)
        return buffer.getvalue()
