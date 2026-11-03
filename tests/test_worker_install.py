"""Execute the shared installer against an HTTP server and a fake uv process."""

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "src/magic_video_editor/worker_install.py"
WHEEL = "outbound_gpu_worker_pool-0.1.0-py3-none-any.whl"
DATA = b"wheel payload"
CODE = "mvsp_" + "s" * 32


@pytest.fixture
def installer_env(tmp_path):
    requests = []
    options = {"status": 200, "filename": WHEEL, "digest": hashlib.sha256(DATA).hexdigest()}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(options["status"])
            self.send_header("X-Worker-Package-Filename", options["filename"])
            self.send_header("X-Worker-Package-SHA256", options["digest"])
            self.send_header("X-Worker-Package-Commit", "abc123")
            self.end_headers()
            self.wfile.write(DATA)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    binary = tmp_path / "bin"
    binary.mkdir()
    fake_uv = binary / "uv"
    fake_uv.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
pathlib.Path(os.environ["CALL_RECORD"]).write_text(json.dumps({
    "args": sys.argv[1:], "code": os.environ.get("MVS_CODE"),
    "wheel": pathlib.Path(sys.argv[-1].split("[")[0]).read_text(),
}))
print("dependency installation diagnostic")
sys.exit(int(os.environ.get("UV_TEST_EXIT", "0")))
''')
    fake_uv.chmod(0o700)
    env = {**os.environ, "MVS_CODE": CODE, "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
           "CALL_RECORD": str(tmp_path / "uv.json")}
    url = f"http://127.0.0.1:{server.server_port}"
    yield tmp_path, url, env, requests, options
    server.shutdown()
    thread.join()
    server.server_close()


def run_installer(setup):
    directory, url, env, _, _ = setup
    return subprocess.run(
        [sys.executable, str(INSTALLER), url, str(directory / "worker")],
        env=env, text=True, capture_output=True,
    )


def test_installs_local_verified_wheel_without_git_or_leaking_code(installer_env):
    directory, _, _, requests, _ = installer_env
    result = run_installer(installer_env)
    assert result.returncode == 0, result.stderr
    record = json.loads((directory / "uv.json").read_text())
    assert record["args"][:4] == ["pip", "install", "--python", sys.executable]
    assert record["args"][4:6] == ["--reinstall-package", "outbound-gpu-worker-pool"]
    assert record["args"][-1].endswith(WHEEL + "[agent,comfy]")
    assert record["wheel"] == DATA.decode()
    assert record["code"] is None
    assert requests == [("/studio/workers/package", {"token": CODE})]
    assert CODE not in result.stdout + result.stderr
    log = directory / "worker/install.log"
    assert "dependency installation diagnostic" in log.read_text()
    assert CODE not in log.read_text()
    assert log.stat().st_mode & 0o077 == 0
    assert not list((directory / "worker").rglob("*.whl"))
    # worker-update.py tells builds apart by this record, since all are 0.1.0.
    assert json.loads((directory / "worker/pool-package.json").read_text()) == {
        "filename": WHEEL, "sha256": hashlib.sha256(DATA).hexdigest(), "commit": "abc123",
    }


@pytest.mark.parametrize("failure", ["checksum", "filename", "refused", "unavailable"])
def test_failed_download_never_installs_or_redeems(installer_env, failure):
    directory, _, _, requests, options = installer_env
    if failure == "checksum":
        options["digest"] = "0" * 64
    elif failure == "filename":
        options["filename"] = "../outside.whl"
    else:
        options["status"] = 400 if failure == "refused" else 503
    result = run_installer(installer_env)
    assert result.returncode != 0
    assert "Attaching failed:" in result.stderr
    assert not (directory / "uv.json").exists()
    assert len(requests) == 1
    assert CODE not in result.stdout + result.stderr


def test_install_failure_keeps_diagnostics_and_explains_retry(installer_env):
    directory, _, env, _, _ = installer_env
    env["UV_TEST_EXIT"] = "1"
    result = run_installer(installer_env)
    assert result.returncode != 0
    assert "dependency installation diagnostic" in (directory / "worker/install.log").read_text()
    assert "install.log" in result.stderr
    assert "has not spent" in result.stderr
    assert not (directory / "worker/pool-package.json").exists()


@pytest.mark.parametrize("name", ["attach.sh", "attach.ps1"])
def test_attach_uses_shared_installer_before_redeeming(name):
    source = (INSTALLER.parent / name).read_text()
    assert "git+https://github.com" not in source
    assert "worker-install.py" in source
    assert source.index("worker-install.py") < source.index("/studio/workers/redeem")
    assert "Nothing was changed" not in source
