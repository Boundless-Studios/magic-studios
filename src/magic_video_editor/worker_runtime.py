"""Discover local worker tools and persist restart-safe enrollment settings.

No model downloads, shell execution, or changes to ComfyUI launch flags. Operators
may provide OGWP_COMFY_START_COMMAND for any other installation/layout.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from urllib.parse import urlsplit


def configure_runtime(environment: dict[str, str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    home = Path.home()
    # A service does not inherit the enrolling terminal's interactive PATH.
    explicit = environment.get("MVS_FFMPEG_BIN")
    found = shutil.which("ffmpeg", path=environment.get("PATH", os.defpath))
    candidates = [explicit] if explicit else [
        str(Path(found).parent) if found else None,
        str(home / ".local/opt/ffmpeg-ubuntu/bin"),
    ]
    for directory in candidates:
        if directory and all(shutil.which(name, path=directory) for name in ("ffmpeg", "ffprobe")):
            settings["MVS_FFMPEG_BIN"] = directory
            environment["PATH"] = directory + os.pathsep + environment.get("PATH", os.defpath)
            break
    if environment.get("MVS_FFMPEG_BIN") and not settings:
        raise ValueError("MVS_FFMPEG_BIN must contain executable ffmpeg and ffprobe")
    url = urlsplit(environment.get("OGWP_COMFY_URL", "http://127.0.0.1:8188"))
    # Never start a local runtime for a remote URL, an unrelated port, or an
    # unrecognized recipe. This known launcher is loopback:8189 and Sage-capable.
    is_wsl = "microsoft" in Path("/proc/version").read_text().lower() if Path("/proc/version").exists() else False
    launcher = Path("/mnt/c/AI/start-windows-comfy.ps1") if is_wsl else Path("C:/AI/start-windows-comfy.ps1")
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe") if is_wsl else Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if (not environment.get("OGWP_COMFY_START_COMMAND")
            and environment.get("MVS_WORKER_PROFILE") == "h3-16gb-lightx8"
            and url.hostname in {"127.0.0.1", "localhost"} and url.port == 8189
            and launcher.is_file() and powershell.is_file()):
        settings["OGWP_COMFY_START_COMMAND"] = shlex.join([
            str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:/AI/start-windows-comfy.ps1", "-Attention", "sage"])
        environment.update(settings)
    return settings


def _largest_card(environment: dict[str, str]) -> tuple[str, int] | None:
    """The local ComfyUI's largest CUDA device as (name, MiB), or None if unreadable."""
    import httpx
    url = environment.get("OGWP_COMFY_URL", "http://127.0.0.1:8188").rstrip("/") + "/system_stats"
    try:
        devices = httpx.get(url, timeout=10).raise_for_status().json().get("devices", [])
    except (httpx.HTTPError, OSError, ValueError):
        return None
    cuda = [d for d in devices if d.get("type") == "cuda" and d.get("vram_total")]
    if not cuda:
        return None
    device = max(cuda, key=lambda d: d["vram_total"])
    return str(device.get("name", "GPU")), int(device["vram_total"]) // (1024 * 1024)


def without_undersized_profile(environment: dict[str, str], settings: dict[str, str],
                               card: tuple[str, int] | None) -> dict[str, str] | None:
    """Settings to attach without a profile the card is too small for, or None.

    Only a card below the profile's declared minimum is downgraded: the machine
    still attaches for what it can run without a recipe (CPU movie processing),
    reporting its card so the studio can say what it cannot run and why. Missing
    models, nodes or flags are fixable, so those still stop the attach before the
    pairing code is spent. Nothing is inferred: no other profile is chosen.
    """
    from outbound_gpu_worker_pool.machine_profiles import describe_profile
    profile = environment.get("MVS_WORKER_PROFILE")
    if not profile or card is None:
        return None
    try:
        needed = describe_profile(profile)["min_vram_mb"]
    except (KeyError, ValueError):
        return None
    name, vram = card
    if vram >= needed:
        return None
    print(f"{name} has {vram:,} MB of VRAM; the {profile} configuration needs {needed:,} MB. "
          "Attaching this machine without it, for the work it can still run. The Machines "
          "page lists what it can and cannot run, and why.", file=sys.stderr)
    kept = {k: v for k, v in settings.items() if k != "OGWP_COMFY_START_COMMAND"}
    return {**kept, "OGWP_WORKER_VRAM_MB": str(vram), "OGWP_WORKER_GPU_MODEL": name}


def main() -> None:
    from outbound_gpu_worker_pool.machine_profiles import prepare_environment
    environment = dict(os.environ)
    settings = configure_runtime(environment)
    try:
        settings.update(asyncio.run(prepare_environment(environment)))
    except ValueError:
        downgraded = without_undersized_profile(environment, settings, _largest_card(environment))
        if downgraded is None:
            raise
        settings = downgraded
        environment.pop("MVS_WORKER_PROFILE", None)
    ffmpeg_ready = all(shutil.which(name, path=environment.get("PATH", os.defpath))
                       for name in ("ffmpeg", "ffprobe"))
    print("CPU movie processing: " + ("FFmpeg ready" if ffmpeg_ready else
          "unavailable; install FFmpeg/ffprobe or set MVS_FFMPEG_BIN"), file=sys.stderr)
    if environment.get("MVS_WORKER_PROFILE"):
        print("ComfyUI restart recovery: " + ("configured" if
              environment.get("OGWP_COMFY_START_COMMAND") else
              "external/manual; configure OGWP_COMFY_START_COMMAND"), file=sys.stderr)
    print(json.dumps(settings))


if __name__ == "__main__":
    main()
