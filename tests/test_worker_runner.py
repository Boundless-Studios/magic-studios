"""The script runner's verified-profile plugin."""

from magic_video_editor.worker_runner import _profile_plugin


def test_the_profile_plugin_restarts_comfyui_with_the_configured_start_command():
    environment = {"OGWP_COMFY_URL": "http://127.0.0.1:8188",
                   "OGWP_COMFY_START_COMMAND": "python main.py --listen 127.0.0.1 --port 8188",
                   "OGWP_COMFY_STARTUP_TIMEOUT_SECONDS": "90"}
    plugin = _profile_plugin("h3-24gb-standard", {}, environment)
    assert plugin._start_command == ("python", "main.py", "--listen", "127.0.0.1", "--port", "8188")
    assert plugin._startup_timeout_seconds == 90.0


def test_without_a_start_command_the_profile_plugin_expects_a_running_comfyui():
    plugin = _profile_plugin("h3-24gb-standard", {}, {"OGWP_COMFY_URL": "http://127.0.0.1:8188"})
    assert plugin._start_command is None
