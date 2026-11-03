import importlib.util
import json
from pathlib import Path


def test_build_uses_the_application_pin_and_records_artifact_digest(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/build_worker_package.py"
    assert script.exists(), "the image must build its protected worker artifact"
    spec = importlib.util.spec_from_file_location("build_worker_package", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "pyproject.toml"
    requirement = "outbound-gpu-worker-pool[coordinator] @ git+https://github.com/example/private@abc123"
    project.write_text('[project]\ndependencies = [' + json.dumps(requirement) + ']\n')
    destination = tmp_path / "packages"
    calls = []

    def build(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("--wheel-dir") + 1])
        (output / "outbound_gpu_worker_pool-0.1.0-py3-none-any.whl").write_bytes(b"wheel")

    monkeypatch.setattr(module.subprocess, "run", build)
    module.build_worker_package(project, destination)
    assert calls[0][-1] == requirement
    manifest = json.loads((destination / "manifest.json").read_text())
    import hashlib
    assert manifest["sha256"] == hashlib.sha256(b"wheel").hexdigest()
    # Every pool build is version 0.1.0; the commit tells an operator which one.
    assert manifest["commit"] == "abc123"
    assert (destination / manifest["filename"]).read_bytes() == b"wheel"
