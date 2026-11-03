from pathlib import Path
import os

import pytest
from magic_video_editor.worker_runtime import configure_runtime


def test_explicit_ffmpeg_directory_is_available_to_service(tmp_path):
    for name in ('ffmpeg', 'ffprobe'):
        file = tmp_path / name
        file.write_text('#!/bin/sh\nexit 0\n')
        file.chmod(0o755)
    environment = {'MVS_FFMPEG_BIN': str(tmp_path), 'PATH': '/usr/bin'}
    assert configure_runtime(environment)['MVS_FFMPEG_BIN'] == str(tmp_path)
    assert environment['PATH'].split(os.pathsep)[0] == str(tmp_path)


def test_invalid_explicit_ffmpeg_directory_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    with pytest.raises(ValueError, match='MVS_FFMPEG_BIN'):
        configure_runtime({'MVS_FFMPEG_BIN': str(tmp_path / 'missing')})


@pytest.mark.parametrize('url,profile', [
    ('http://remote.example:8189', 'h3-16gb-lightx8'),
    ('http://127.0.0.1:8188', 'h3-16gb-lightx8'),
    ('http://127.0.0.1:8189', 'h3-24gb-standard'),
])
def test_no_local_launcher_for_remote_or_other_recipe(url, profile):
    environment = {'OGWP_COMFY_URL': url, 'MVS_WORKER_PROFILE': profile}
    assert 'OGWP_COMFY_START_COMMAND' not in configure_runtime(environment)


def test_explicit_launcher_is_not_replaced():
    environment = {'OGWP_COMFY_START_COMMAND': 'custom-runtime --flags'}
    configure_runtime(environment)
    assert environment['OGWP_COMFY_START_COMMAND'] == 'custom-runtime --flags'


def test_verified_wsl_launcher_preserves_sage(monkeypatch):
    read_text = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda self, *a, **k:
                        'Linux Microsoft WSL2' if str(self) == '/proc/version' else read_text(self, *a, **k))
    monkeypatch.setattr(Path, 'exists', lambda self: True)
    monkeypatch.setattr(Path, 'is_file', lambda self: True)
    environment = {'MVS_WORKER_PROFILE': 'h3-16gb-lightx8',
                   'OGWP_COMFY_URL': 'http://127.0.0.1:8189', 'PATH': ''}
    command = configure_runtime(environment)['OGWP_COMFY_START_COMMAND']
    import shlex
    args = shlex.split(command)
    assert args[0].endswith('/powershell.exe')
    assert args[-2:] == ['-Attention', 'sage']
    assert 'C:/AI/start-windows-comfy.ps1' in args


def test_a_card_too_small_for_its_profile_attaches_without_it(capsys):
    from magic_video_editor.worker_runtime import without_undersized_profile

    environment = {"MVS_WORKER_PROFILE": "h3-24gb-standard"}
    settings = {"MVS_FFMPEG_BIN": "/usr/bin", "OGWP_COMFY_START_COMMAND": "start"}
    downgraded = without_undersized_profile(environment, settings, ("RTX 4070", 12282))

    assert downgraded == {"MVS_FFMPEG_BIN": "/usr/bin", "OGWP_WORKER_VRAM_MB": "12282",
                          "OGWP_WORKER_GPU_MODEL": "RTX 4070"}
    assert "MVS_WORKER_PROFILE" not in downgraded
    assert "RTX 4070 has 12,282 MB of VRAM; the h3-24gb-standard configuration needs 24,000 MB" in (
        capsys.readouterr().err)


@pytest.mark.parametrize("card", [None, ("RTX 4090", 24564)])
def test_other_profile_failures_still_stop_the_attach(card):
    from magic_video_editor.worker_runtime import without_undersized_profile

    # Unreadable card, or one big enough: the failure was models/nodes/flags, which the
    # person can fix before spending the pairing code.
    assert without_undersized_profile({"MVS_WORKER_PROFILE": "h3-24gb-standard"}, {}, card) is None
    assert without_undersized_profile({}, {}, ("RTX 3060", 12288)) is None
