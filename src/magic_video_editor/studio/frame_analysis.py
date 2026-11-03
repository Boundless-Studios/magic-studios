"""Bounded native-frame inspection and versioned pose-cycle suggestions."""

import base64
import io
import json
from fractions import Fraction
import math
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from magic_video_editor.plugins.ffmpeg import alpha_video_decoder_args
from magic_video_editor.studio.exports import split_sheet_into_tiles
from magic_video_editor.studio.models import MediaKind, MotionCandidate, NormalizationSpec

ANALYSIS_VERSION = "pose-cycle-v5"
MAX_ANALYSIS_FRAMES = 512


def analyze_candidate_frames(source: bytes, candidate: MotionCandidate) -> dict:
    metadata = candidate.metadata
    if metadata is None or not 1 <= metadata.frame_count <= MAX_ANALYSIS_FRAMES:
        raise ValueError("frame analysis requires between 1 and 512 measured frames")
    decoded_duration_ms = None
    frame_rate = metadata.frame_rate
    time_base = metadata.time_base
    if metadata.media_kind is MediaKind.SHEET:
        frames = split_sheet_into_tiles(source, metadata.frame_count)
    else:
        with tempfile.TemporaryDirectory(prefix="studio-native-frames-") as folder:
            path = Path(folder) / "source"
            path.write_bytes(source)
            subprocess.run(
                ["ffmpeg", "-v", "error", *alpha_video_decoder_args(source), "-i", str(path),
                 "-map", "0:v:0", "-vsync", "0", "-frames:v", str(MAX_ANALYSIS_FRAMES + 1),
                 str(Path(folder) / "%05d.png")],
                check=True, capture_output=True, timeout=120,
            )
            frames = [p.read_bytes() for p in sorted(Path(folder).glob("*.png"))]
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=avg_frame_rate,time_base", "-of", "json", str(path)],
                capture_output=True, check=True, timeout=30,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            frame_rate = str(Fraction(stream["avg_frame_rate"]))
            time_base = stream.get("time_base")
            fps = float(Fraction(frame_rate))
            if not math.isfinite(fps) or fps <= 0:
                raise ValueError("video has no usable frame rate")
            decoded_duration_ms = len(frames) * 1000 / fps
        if len(frames) != metadata.frame_count:
            raise ValueError("decoded frame count differs from candidate metadata; remeasure before editing")
    duration_ms = decoded_duration_ms or (candidate.frame_duration_ms * len(frames)
                   if candidate.frame_duration_ms else candidate.duration_ms)
    if not duration_ms or duration_ms <= 0:
        raise ValueError("frame analysis requires measured duration")
    records = []
    for index, content in enumerate(frames):
        with Image.open(io.BytesIO(content)) as opened:
            image = opened.convert("RGBA")
        pixels = np.asarray(image)
        alpha = pixels[:, :, 3]
        # Opaque green-source takes are inspectable before alpha derivation.
        rgb = pixels[:, :, :3].astype(np.int16)
        green = (rgb[:, :, 1] > rgb[:, :, 0] + 35) & (rgb[:, :, 1] > rgb[:, :, 2] + 25)
        has_transparency = bool(np.any(alpha < 250))
        border_green = np.concatenate((green[0], green[-1], green[:, 0], green[:, -1])).mean() > .5
        foreground = alpha > 8 if has_transparency else (~green if border_green else None)
        coordinates = None if foreground is None else np.argwhere(foreground)
        bounds = None if coordinates is None or len(coordinates) == 0 else [
            int(coordinates[:, 1].min()), int(coordinates[:, 0].min()),
            int(coordinates[:, 1].max()) + 1, int(coordinates[:, 0].max()) + 1,
        ]
        central = None if foreground is None else np.argwhere(
            foreground[:, int(image.width * .35):int(image.width * .65)])
        central_bounds = None if central is None or len(central) == 0 else [
            int(central[:, 0].min()), int(central[:, 0].max()) + 1,
        ]
        edge = None if foreground is None else bool(
            foreground[:2].any() or foreground[-2:].any()
            or foreground[:, :2].any() or foreground[:, -2:].any())
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        pose = []
        for y in range(16):
            for x in range(16):
                r, g, b, a = image.getpixel((int((x + .5) * image.width / 16), int((y + .5) * image.height / 16)))
                pose.extend(c / 255 * a / 255 * (2 if y >= 8 else 1) for c in (r, g, b))
        output = io.BytesIO()
        image.save(output, format="PNG")
        records.append(dict(index=index, timestamp_ms=index * duration_ms / len(frames),
                            image="data:image/png;base64," + base64.b64encode(output.getvalue()).decode(),
                            pose=pose, edge_contact=edge, foreground_bbox=bounds, central_vertical_bounds=central_bounds))
    return dict(analysis_version=ANALYSIS_VERSION, candidate_id=candidate.candidate_id,
                source_sha256=candidate.sha256, frame_count=len(frames),
                source_chroma_alpha=bool(candidate.recipe and candidate.recipe.op == "derive_alpha"),
                width=metadata.width // len(frames) if metadata.media_kind is MediaKind.SHEET else metadata.width,
                height=metadata.height, duration_ms=duration_ms, fps=1000 * len(frames) / duration_ms,
                frame_rate=frame_rate, time_base=time_base,
                index_space="stored_source_frames", frames=records)


def _attack_distances(frames: list[dict], coarse: np.ndarray) -> np.ndarray:
    """Compare complete silhouettes, including the weapon, at 64-pixel area resolution.

    The 16x16 point samples used by walk can miss a narrow axe entirely. Premultiplied
    color and area-averaged alpha make the attack seam sensitive to its true position.
    Synthetic callers without images retain the coarse descriptor for compatibility.
    """
    if any(not isinstance(frame.get("image"), str) for frame in frames):
        return np.abs(coarse[:, None] - coarse[None, :]).mean(axis=2)
    resolution = 64 if len(frames) <= 160 else 32
    signatures = []
    for frame in frames:
        content = base64.b64decode(frame["image"].split(",", 1)[1])
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        pixels = np.asarray(image, dtype=np.uint8)
        alpha = pixels[:, :, 3].astype(np.float32) / 255
        if np.all(alpha > .98):
            rgb = pixels[:, :, :3].astype(np.int16)
            green = (rgb[:, :, 1] > rgb[:, :, 0] + 35) & (rgb[:, :, 1] > rgb[:, :, 2] + 25)
            if np.concatenate((green[0], green[-1], green[:, 0], green[:, -1])).mean() > .5:
                pixels = pixels.copy()
                pixels[green, 3] = 0
                image = Image.fromarray(pixels, "RGBA")
        reduced = np.asarray(image.resize((resolution, resolution), Image.Resampling.BOX), dtype=np.float32) / 255
        mask = reduced[:, :, 3:4]
        signatures.append(np.concatenate((mask * 2, reduced[:, :, :3] * mask * .5), axis=2).ravel())
    signatures = np.stack(signatures)
    distances = np.empty((len(frames), len(frames)), dtype=np.float32)
    for index in range(len(frames)):
        distances[index] = np.abs(signatures - signatures[index]).mean(axis=1)
    return distances


def _suggest_idle_cycle(analysis: dict, count: int) -> dict:
    """Find a subtle reciprocal pose cycle across the full measured source.

    Matching endpoints alone would accept a frozen hold or half a breath. Require
    visible excursion, out-and-back travel and matching seam motion as well. These
    pose proxies propose source indices; they never establish visual acceptance.
    """
    frames = analysis["frames"]
    poses = np.asarray([frame["pose"] for frame in frames], dtype=np.float64)
    fps = float(Fraction(analysis["frame_rate"])) if analysis.get("frame_rate") else analysis["fps"]
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("idle loop requires a usable measured source frame rate")
    if not 1 <= len(frames) <= MAX_ANALYSIS_FRAMES or not np.isfinite(poses).all():
        raise ValueError("idle loop requires bounded finite measured poses")
    result = dict(analysis_version=ANALYSIS_VERSION, mode="idle", candidate_id=analysis["candidate_id"],
                  source_sha256=analysis["source_sha256"], status="incomplete", start=None, end=None,
                  ordered_frames=[], frame_duration_ms=None, loop_policy="loop",
                  edge_clipped_frames=[], warnings=[], visual_review_required=True,
                  source_frame_rate=analysis.get("frame_rate"), source_time_base=analysis.get("time_base"))
    n = len(frames)
    rounded = lambda value: math.floor(value + .5)
    minimum = max(count, rounded(fps * .75))
    distances = np.empty((n, n))
    for index in range(n):
        distances[index] = np.abs(poses - poses[index]).mean(axis=1)
    travel = np.concatenate(([0], np.cumsum(np.diag(distances, 1))))
    best, score = None, math.inf
    for start in range(n - minimum):
        for end in range(start + minimum, n):
            excursion = float(distances[start, start + 1:end + 1].max())
            seam = distances[start, end]
            if excursion < .003 or seam > max(.0005, excursion * .12):
                continue
            if travel[end] - travel[start] < excursion * 1.8:
                continue
            first_delta = poses[start + 1] - poses[start]
            last_delta = poses[end] - poses[end - 1]
            velocity_seam = float(np.abs(first_delta - last_delta).mean())
            if velocity_seam > max(.0002, excursion * .04):
                continue
            value = seam + velocity_seam
            if value < score:
                best, score = (start, end), value
    if best is None:
        result["warnings"].append("No complete active idle cycle found; inspect breathing and recovery or generate another take.")
        return result
    start, end = best
    clipped = [index for index in range(start, end + 1) if frames[index].get("edge_contact") is True]
    unknown = any(frames[index].get("edge_contact") is None for index in range(start, end + 1))
    result.update(start=start, end=end, ordered_frames=[start + index * (end - start) // count for index in range(count)],
                  frame_duration_ms=max(1, rounded(1000 * (end - start) / fps / count)),
                  source_interval_ms=1000 * (end - start) / fps,
                  edge_clipped_frames=clipped, status="incomplete" if clipped or unknown else "complete")
    if clipped:
        result["warnings"].append("Foreground touches the source edge; padding cannot restore clipped pixels.")
    if unknown:
        result["warnings"].append("Edge visibility could not be measured; derive alpha and inspect again.")
    result["warnings"].append("Pose matching is heuristic; verify subtle breathing, identity, grounding and timed wrap visually.")
    return result


def suggest_cycle(analysis: dict, mode: str, count: int) -> dict:
    """Port of walkLoop.ts/attackLoop.ts; a suggestion is never visual acceptance."""
    if mode not in ("idle", "walk", "attack") or count not in (8, 12, 16):
        raise ValueError("choose idle, walk or attack and 8, 12, or 16 output frames")
    if mode == "idle":
        return _suggest_idle_cycle(analysis, count)
    poses = np.asarray([frame["pose"] for frame in analysis["frames"]], dtype=np.float64)
    n, fps = len(poses), analysis["fps"]
    if mode == "attack":
        distances = _attack_distances(analysis["frames"], poses)
    else:
        distances = np.empty((n, n))
        for i in range(n):
            distances[i] = np.abs(poses - poses[i]).mean(axis=1)
    rounded = lambda value: math.floor(value + .5)  # JavaScript Math.round parity
    minimum = max(count, rounded(fps * (.65 if mode == "walk" else .75)))
    best, score = None, math.inf
    earliest = next((i for i in range(1, n) if distances[0, i] >= (.025 if mode == "attack" else .04)), -1)
    latest_start = n - 1
    anchor_tolerance = max(4, analysis.get("height", 512) / 64)
    travel = np.concatenate(([0], np.cumsum(np.diag(distances, 1))))
    longer_window = False
    # Preserve the compact search and its ranking before considering longer attacks.
    # Both passes use the same measured-pose guards and only stored source frames.
    for longer_window in ((False, True) if mode == "attack" else (False,)):
        for start in range(latest_start + 1):
            if mode == "attack" and earliest < 0:
                break
            end_limit = (min(n - 1, start + rounded(fps * 2) + 1) if mode == "walk"
                         else n if longer_window else min(n, start + rounded(fps * 1.8) + 1))
            first_end = max(start + minimum, start + rounded(fps * 1.8) + 1) if longer_window else start + minimum
            for end in range(first_end, end_limit):
                seam = distances[start, end]
                if mode == "walk":
                    motion = distances[start, (start + end) // 2]
                    if motion < .015 or motion < seam * 1.5:
                        continue
                    value = seam + .5 * distances[start + 1, end + 1]
                else:
                    first_center = analysis["frames"][start].get("central_vertical_bounds")
                    last_center = analysis["frames"][end].get("central_vertical_bounds")
                    if first_center is not None and last_center is not None:
                        if abs(last_center[1] - first_center[1]) > anchor_tolerance:
                            continue
                        first_height = first_center[1] - first_center[0]
                        last_height = last_center[1] - last_center[0]
                        if abs(last_height - first_height) > anchor_tolerance * 1.5:
                            continue
                    excursion = float(distances[start, start + 1:end + 1].max())
                    if excursion < .025 or seam > max(.012, excursion * .3):
                        continue
                    if travel[end] - travel[start] < excursion * 2.1:
                        continue
                    peak = int(distances[start, start + 1:end + 1].argmax()) + start + 1
                    if peak < start + rounded(fps * .15) or end - peak < rounded(fps * .12):
                        continue
                    # A visible seam dominates; among near-identical seams, prefer one
                    # compact strike/recovery over a long idle hold before the next hit.
                    value = seam + .0002 * (end - start)
                if value < score:
                    best, score = (start, end), value
        if best is not None:
            break
    result = dict(analysis_version=ANALYSIS_VERSION, mode=mode, candidate_id=analysis["candidate_id"],
                  source_sha256=analysis["source_sha256"], status="incomplete", start=None, end=None,
                  ordered_frames=[], frame_duration_ms=None, loop_policy="once" if mode == "attack" else "loop",
                  edge_clipped_frames=[], warnings=[], visual_review_required=True)
    if best is None:
        result["warnings"].append("No complete pose cycle found; inspect the source or generate another take.")
        return result
    if longer_window:
        result["warnings"].append("No compact attack cycle found; using a longer source window exceeding 1.8 seconds. Review holds, cadence, recovery, and number of strikes visually.")
    start, end = best
    clipped = [i for i in range(start, end + 1) if analysis["frames"][i]["edge_contact"] is True]
    unknown = any(analysis["frames"][i]["edge_contact"] is None for i in range(start, end + 1))
    result.update(start=start, end=end, ordered_frames=[start + i * (end - start) // count for i in range(count)],
                  frame_duration_ms=max(1, rounded(1000 * (end - start) / fps / count)),
                  edge_clipped_frames=clipped, status="incomplete" if clipped or unknown else "complete")
    first_bounds = analysis["frames"][start].get("foreground_bbox")
    last_bounds = analysis["frames"][end].get("foreground_bbox")
    if first_bounds is not None and last_bounds is not None:
        delta = last_bounds[3] - first_bounds[3]
        result["foreground_bottom_delta_px"] = delta
        if abs(delta) > max(4, analysis.get("height", 512) * .015):
            result["warnings"].append("Foreground bottom changes across the seam; inspect feet separately because weapon overhang can affect this measurement.")
    if clipped:
        result["warnings"].append("Foreground touches the source edge; padding cannot restore clipped pixels.")
    if unknown:
        result["warnings"].append("Edge visibility could not be measured; derive alpha and inspect again.")
    result["warnings"].append("Central silhouette alignment is a proxy, not a measured foot anchor; weapon overhang requires visual review.")
    result["warnings"].append("Pose matching is heuristic; verify motion, number of strikes, grounding, and cadence visually.")
    return result


def calibrate_body(source: dict, reference: dict, source_frame: int, reference_frame: int = 0) -> dict:
    """Match central body height and belt/feet anchor on explicitly chosen ready poses."""
    def measure(analysis: dict, index: int) -> dict:
        if not 0 <= index < len(analysis["frames"]):
            raise ValueError("calibration frame is outside the source")
        record = analysis["frames"][index]
        content = base64.b64decode(record["image"].split(",", 1)[1])
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        pixels = np.asarray(image)
        if not np.any(pixels[:, :, 3] < 250):
            raise ValueError("body calibration requires transparent frames; derive alpha first")
        left, right = math.floor(image.width * .24), math.ceil(image.width * .76)
        mask = pixels[:, left:right, 3] >= 96
        ys, xs = np.nonzero(mask)
        if len(ys) == 0 or ys.max() <= ys.min():
            raise ValueError("body silhouette could not be measured")
        top, feet = int(ys.min()), int(ys.max())
        first = math.floor(top + (feet - top) * .53 + .5)
        last = math.floor(top + (feet - top) * .68 + .5)
        belt_xs = np.sort(np.nonzero(mask[first:last + 1])[1] + left)
        hip = int(belt_xs[len(belt_xs) // 2]) if len(belt_xs) else image.width / 2
        sx, sy = analysis["width"] / image.width, analysis["height"] / image.height
        return dict(height=(feet - top + 1) * sy, hip_x=hip * sx, feet_y=feet * sy, top=top * sy)
    attack, walk = measure(source, source_frame), measure(reference, reference_frame)
    normalization = dict(scale=math.floor(walk["height"] / attack["height"] * 1000 + .5) / 1000,
                         source_anchor_x=attack["hip_x"], source_anchor_y=attack["feet_y"],
                         target_anchor_x=walk["hip_x"], target_anchor_y=walk["feet_y"], padding=8,
                         cleanup_green_edges=bool(source.get("source_chroma_alpha")),
                         reference_candidate_id=reference["candidate_id"], reference_sha256=reference["source_sha256"])
    method = "central-body-alpha-v1"
    pivot = reference.get("registered_pivot")
    if pivot is not None:
        px, py = pivot["x"], pivot["y"]
        if (isinstance(px, bool) or isinstance(py, bool) or not math.isfinite(px) or not math.isfinite(py)
                or not 0 <= px <= reference["width"] or not 0 <= py <= reference["height"]):
            raise ValueError("registered reference pivot is outside its frame")
        scale = normalization["scale"]
        if scale <= 0:
            raise ValueError("rounded calibration scale must be positive")
        # Reparameterize the same affine map around the declared registration:
        # P - s * (S + (P - T) / s) == T - s * S.
        source_x = attack["hip_x"] + (px - walk["hip_x"]) / scale
        source_y = attack["feet_y"] + (py - walk["feet_y"]) / scale
        if not 0 <= source_x <= source["width"] or not 0 <= source_y <= source["height"]:
            raise ValueError("registered-pivot calibration places the virtual source anchor outside the source frame")
        normalization.update(source_anchor_x=source_x, source_anchor_y=source_y,
                             target_anchor_x=px, target_anchor_y=py)
        # Enforce the existing finite scale/anchor bounds without rounding or clamping.
        NormalizationSpec(**normalization)
        method = "central-body-alpha-registered-pivot-v2"
    return dict(normalization=normalization, source_measurement=attack, reference_measurement=walk,
                source_candidate_id=source["candidate_id"], reference_candidate_id=reference["candidate_id"],
                source_sha256=source["source_sha256"], reference_sha256=reference["source_sha256"],
                source_frame=source_frame, reference_frame=reference_frame,
                method=method, visual_review_required=True,
                warnings=["Compare resting poses visually: weapons crossing the central body can bias this measurement."])
