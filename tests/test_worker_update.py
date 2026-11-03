import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from magic_video_editor import worker_update

UPDATER = Path(worker_update.__file__)
FILENAME = 'outbound_gpu_worker_pool-0.1.0-py3-none-any.whl'
CREDENTIAL = 'mvsw_machine-credential-fixture'
FEATURES = {'old': '{}', 'new': '{"magic_feature_last_frame":"1"}', 'needs-extra': '{}', 'newer': '{}'}
SCRIPTS = {
    'worker.py': '# runner {build}\n',
    'cpu-worker.py': '# cpu {build}\n',
    # Each build's runtime writes its own setting, so a stale configuration shows.
    'worker-runtime.py': 'def configure_runtime(environment):\n    return {{"MVS_RUNTIME_BUILD": "{build}"}}\n# {build}\n',
}


def test_running_service_is_never_replaced(monkeypatch):
    if worker_update.os.name == 'nt':
        pytest.skip('systemd path')
    monkeypatch.setattr(worker_update.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(worker_update.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('download before stopping'))
    with pytest.raises(SystemExit, match='Stop magic-video-worker gracefully'):
        worker_update.main([])


def test_rollback_also_refuses_a_running_service(monkeypatch):
    if worker_update.os.name == 'nt':
        pytest.skip('systemd path')
    monkeypatch.setattr(worker_update.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(worker_update, 'restore_pool', lambda *a: pytest.fail('restored while running'))
    with pytest.raises(SystemExit, match='Stop magic-video-worker gracefully'):
        worker_update.main(['--rollback'])


@pytest.mark.parametrize('argv', [['--rollbak'], ['--rollback', 'extra'], ['--force'], ['rollback']])
def test_unknown_arguments_never_fall_through_to_an_update(monkeypatch, argv):
    monkeypatch.setattr(worker_update.subprocess, 'run', lambda *a, **k: pytest.fail('checked the service'))
    monkeypatch.setattr(worker_update, 'rollback', lambda *a: pytest.fail('rolled back'))
    monkeypatch.setattr(worker_update, 'apply_update', lambda *a: pytest.fail('updated'))
    with pytest.raises(SystemExit, match='Usage: python worker-update.py'):
        worker_update.main(argv)


def wheel(build):
    """A minimal pool wheel whose profile check reports `build`'s features or fails."""
    check = (f"    return {{'OGWP_WORKER_RUNTIME_VERSIONS': {FEATURES[build]!r}}}\n" if build in FEATURES
             else "    raise ValueError('ComfyUI is not running')\n")
    metadata = 'Metadata-Version: 2.1\nName: outbound-gpu-worker-pool\nVersion: 0.1.0\n'
    metadata += 'Provides-Extra: agent\nProvides-Extra: comfy\n'
    if build == 'needs-extra':
        # `uv pip check` ignores extras, so only the extras check can see this.
        metadata += 'Requires-Dist: definitely-not-installed>=1; extra == "agent"\n'
    files = {
        'outbound_gpu_worker_pool/__init__.py': f'BUILD = {build!r}\n',
        'outbound_gpu_worker_pool/agent_main.py': '',
        # A module only later builds have: a rollback must not leave it behind.
        **({'outbound_gpu_worker_pool/only_new.py': 'NEW = True\n'} if build != 'old' else {}),
        'outbound_gpu_worker_pool/machine_profiles.py': 'async def prepare_environment(environment):\n' + check,
        'outbound_gpu_worker_pool-0.1.0.dist-info/METADATA': metadata,
        'outbound_gpu_worker_pool-0.1.0.dist-info/WHEEL':
            'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    record = []
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b'=').decode()
        record.append(f'{name},sha256={digest},{len(text.encode())}')
    record.append('outbound_gpu_worker_pool-0.1.0.dist-info/RECORD,,')
    buffer = io.BytesIO()
    files['outbound_gpu_worker_pool-0.1.0.dist-info/RECORD'] = '\n'.join(record) + '\n'
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in files.items():
            # A fixed timestamp keeps a build's digest identical across requests.
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


@pytest.fixture
def machine(tmp_path):
    """A real uv venv, the stdlib-only updater beside it and a studio serving one build."""
    uv = shutil.which('uv')
    if uv is None:
        if os.environ.get('CI'):
            pytest.fail('CI must provide uv; these tests are the only real-install check')
        pytest.skip('uv is required to install the test wheels')
    subprocess.run([uv, 'venv', '-q', '--python', sys.executable, str(tmp_path / 'venv')], check=True)
    python = tmp_path / 'venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    shutil.copy(UPDATER, tmp_path / 'worker_update.py')
    root = tmp_path / 'worker'
    root.mkdir()
    served = {'build': 'old'}
    requests = []

    class Studio(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.removeprefix('/studio/')
            self.send_response(200 if name in SCRIPTS else 404)
            self.end_headers()
            if name in SCRIPTS:
                self.wfile.write(SCRIPTS[name].format(build=served['build']).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(self.path)
            data = wheel(served['build'])
            digest = hashlib.sha256(data).hexdigest()
            if body != {'credential': CREDENTIAL}:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('X-Worker-Package-Filename', FILENAME)
            self.send_header('X-Worker-Package-SHA256', digest)
            self.end_headers()
            if self.path.endswith('/manifest'):
                self.wfile.write(json.dumps({'filename': FILENAME, 'sha256': digest, 'commit': served['build']}).encode())
            else:
                self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Studio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def run(*code):
        driver = ('import json, os, pathlib, sys\nsys.path.insert(0, sys.argv[1])\nimport worker_update as w\n'
                  'root = pathlib.Path(sys.argv[2])\n' + '\n'.join(code))
        # Workers write bytecode; a stale .pyc is exactly what a rollback must not leave behind.
        environment = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
        return subprocess.run([str(python), '-c', driver, str(tmp_path), str(root), url],
                              capture_output=True, text=True, check=False, env=environment)

    # Reproducible wheels pin every timestamp, so builds of one size are told apart
    # by content alone: a cached .pyc must not hide which build is installed.
    pin = ('import outbound_gpu_worker_pool.machine_profiles as m\n'
           'for f in pathlib.Path(m.__file__).parent.glob("*.py"): os.utime(f, (1_000_000_000, 1_000_000_000))')

    def update(credential=CREDENTIAL, *setup, write_config='lambda c: (root / "worker.env").write_text(json.dumps(c))'):
        # Like main(), start from the configuration on disk.
        config = {'OGWP_WORKER_TOKEN': credential, 'MVS_WORKER_PROFILE': 'standard'}
        on_disk = ('config = json.loads((root / "worker.env").read_text()) if (root / "worker.env").exists() else {}\n'
                   f'config.update({config!r})')
        result = run(*setup, on_disk, f'm, r, changed = w.apply_update(root, sys.argv[3], config, {write_config})',
                     'print(json.dumps({"commit": m["commit"], "versions": r["runtime_versions"], "changed": changed}))')
        run(pin)
        return result

    def installed():
        result = run('import outbound_gpu_worker_pool as p; print(p.BUILD)')
        return result.stdout.strip() or result.stderr

    def record():
        path = root / 'pool-package.json'
        return json.loads(path.read_text()).get('commit') if path.exists() else None

    def scripts():
        return (root / 'worker.py').read_text() if (root / 'worker.py').exists() else None

    def config():
        path = root / 'worker.env'
        return json.loads(path.read_text()).get('MVS_RUNTIME_BUILD') if path.exists() else None

    yield SimpleNamespace(root=root, served=served, requests=requests, run=run, update=update,
                          installed=installed, record=record, scripts=scripts, config=config)
    server.shutdown()
    thread.join()
    server.server_close()


def outcome(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_updates_record_skip_and_roll_back_the_pool_package(machine):
    # A worker attached before builds were recorded has no record: install once.
    assert outcome(machine.update()) == {'commit': 'old', 'versions': {}, 'changed': True}
    assert machine.installed() == 'old' and machine.record() == 'old'
    assert machine.scripts() == '# runner old\n'

    # The same build again downloads nothing and still runs the profile check.
    machine.requests.clear()
    assert outcome(machine.update())['changed'] is False
    assert machine.requests == ['/studio/workers/package/manifest']

    machine.served['build'] = 'new'
    assert outcome(machine.update()) == {
        'commit': 'new', 'versions': {'magic_feature_last_frame': '1'}, 'changed': True,
    }
    assert machine.installed() == 'new' and machine.record() == 'new'
    assert machine.scripts() == '# runner new\n'

    # A build that fails its check is removed and the previous one restored.
    machine.served['build'] = 'broken'
    failed = machine.update()
    assert failed.returncode != 0
    assert 'rolled back' in failed.stderr and 'ComfyUI is not running' in failed.stderr
    assert machine.installed() == 'new' and machine.record() == 'new'
    assert machine.scripts() == '# runner new\n'

    # An operator can return to the build before the last successful update, even
    # though both builds' sources share a size and an mtime.
    restored = machine.run('w.rollback(root)')
    assert restored.returncode == 0, restored.stderr
    assert machine.installed() == 'old' and machine.record() == 'old'


def test_refused_credential_changes_nothing(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    refused = machine.update(credential='mvsw_revoked')
    assert refused.returncode != 0
    assert 'refused this machine credential' in refused.stderr
    assert machine.installed() == 'old' and machine.scripts() == '# runner old\n'
    assert machine.requests[-1] == '/studio/workers/package/manifest'


def test_an_install_that_leaves_other_files_is_rolled_back(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    # The installer reports success without replacing anything.
    failed = machine.update(CREDENTIAL, 'w.install_wheel = lambda uv, wheel: None')
    assert failed.returncode != 0
    assert 'do not match the downloaded wheel' in failed.stderr
    assert machine.installed() == 'old' and machine.record() == 'old'


def test_a_build_missing_an_extras_dependency_changes_nothing(machine):
    assert outcome(machine.update())['changed'] is True
    backups = sorted((machine.root / 'rollback').iterdir())
    machine.served['build'] = 'needs-extra'
    refused = machine.update()
    assert refused.returncode != 0
    assert 'needs dependencies this worker does not have' in refused.stderr
    assert machine.installed() == 'old' and machine.record() == 'old'
    assert machine.scripts() == '# runner old\n'
    assert sorted((machine.root / 'rollback').iterdir()) == backups


def test_a_failure_after_the_package_changed_restores_package_and_scripts(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    failed = machine.update(write_config='lambda c: (_ for _ in ()).throw(OSError("disk full"))')
    assert failed.returncode != 0
    assert 'Worker refresh failed and was rolled back (disk full)' in failed.stderr
    # The test's configuration writer always fails, so writing it back is reported, not hidden.
    assert 'previous configuration could not be written back' in failed.stderr
    assert machine.installed() == 'old' and machine.record() == 'old'
    assert machine.scripts() == '# runner old\n'
    assert (machine.root / 'worker_runtime.py').read_text().endswith('# old\n')


def test_a_failed_record_write_restores_the_configuration_and_the_record(machine):
    assert outcome(machine.update())['changed'] is True
    assert machine.config() == 'old'
    machine.served['build'] = 'new'
    # The first write of the worker's pool-package.json fails, however the updater writes it.
    fail_once = (
        'import builtins\n'
        'state = {"failed": False}\n'
        'def _fail(target):\n'
        '    if pathlib.Path(target) == root / "pool-package.json" and not state["failed"]:\n'
        '        state["failed"] = True\n'
        '        raise OSError("disk full")\n'
        'original_replace, original_write = w.os.replace, pathlib.Path.write_text\n'
        'w.os.replace = lambda a, b: (_fail(b), original_replace(a, b))[1]\n'
        'pathlib.Path.write_text = lambda self, *a, **k: (_fail(self), original_write(self, *a, **k))[1]')
    failed = machine.update(CREDENTIAL, fail_once)
    assert failed.returncode != 0
    assert 'rolled back (disk full)' in failed.stderr
    assert machine.installed() == 'old' and machine.record() == 'old'
    assert machine.config() == 'old' and machine.scripts() == '# runner old\n'
    assert not list(machine.root.glob('*.tmp'))


def test_an_installed_package_that_cannot_be_listed_is_never_replaced(machine):
    assert outcome(machine.update())['changed'] is True
    # A missing RECORD: the package works, but a backup could not restore it.
    found = machine.run('import importlib.metadata as m; print(m.distribution("outbound-gpu-worker-pool")._path)')
    record = Path(found.stdout.strip()) / 'RECORD'
    record.unlink()
    backups = sorted((machine.root / 'rollback').iterdir())
    machine.served['build'] = 'new'
    refused = machine.update()
    assert refused.returncode != 0
    assert 'no file list (RECORD); nothing was changed' in refused.stderr
    assert machine.installed() == 'old' and machine.record() == 'old' and machine.config() == 'old'
    assert sorted((machine.root / 'rollback').iterdir()) == backups


def _record_path(machine):
    found = machine.run('import importlib.metadata as m; print(m.distribution("outbound-gpu-worker-pool")._path)')
    return Path(found.stdout.strip()) / 'RECORD'


def _has_new_module(machine):
    return machine.run('import outbound_gpu_worker_pool.only_new').returncode == 0


def test_a_rollback_removes_the_newer_build_even_when_its_record_is_gone(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    assert outcome(machine.update())['changed'] is True
    assert _has_new_module(machine)
    _record_path(machine).unlink()  # damaged after the update
    restored = machine.run('w.rollback(root)')
    assert restored.returncode == 0, restored.stderr
    assert machine.installed() == 'old' and not _has_new_module(machine)


def test_a_rollback_with_nothing_to_list_is_refused_before_changing_anything(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    assert outcome(machine.update())['changed'] is True
    _record_path(machine).unlink()
    [backup] = list((machine.root / 'rollback').iterdir())
    (backup / 'replaced_by.json').unlink(missing_ok=True)
    refused = machine.run('w.rollback(root)')
    assert refused.returncode != 0
    assert 'cannot be listed' in refused.stderr
    assert machine.installed() == 'new' and _has_new_module(machine)
    assert backup.exists()


@pytest.mark.parametrize('interrupt', ['raise KeyboardInterrupt', 'os.kill(os.getpid(), signal.SIGTERM); time.sleep(5)'])
def test_an_interrupted_install_restores_the_previous_package(machine, interrupt):
    assert outcome(machine.update())['changed'] is True
    backups = sorted((machine.root / 'rollback').iterdir())
    machine.served['build'] = 'new'
    interrupted = machine.update(CREDENTIAL, (
        'import signal, time\n'
        'install = w.install_wheel\n'
        'def interrupted_install(uv, wheel):\n'
        '    install(uv, wheel)\n'
        f'    {interrupt}\n'
        'w.install_wheel = interrupted_install'))
    assert interrupted.returncode != 0
    assert machine.installed() == 'old' and not _has_new_module(machine)
    assert machine.record() == 'old' and machine.scripts() == '# runner old\n'
    # The restored build is installed again, so the interrupted update's backup is gone.
    assert sorted((machine.root / 'rollback').iterdir()) == backups


def test_an_interrupt_while_committing_restores_package_scripts_and_record(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    interrupted = machine.update(write_config='lambda c: (_ for _ in ()).throw(KeyboardInterrupt())')
    assert interrupted.returncode != 0
    assert 'KeyboardInterrupt' in interrupted.stderr
    assert machine.installed() == 'old' and not _has_new_module(machine)
    assert machine.record() == 'old' and machine.scripts() == '# runner old\n'


def test_only_the_backup_rollback_can_use_is_kept(machine):
    for build in ('old', 'new', 'newer'):
        machine.served['build'] = build
        assert outcome(machine.update())['changed'] is True
    [backup] = list((machine.root / 'rollback').iterdir())
    restored = machine.run('w.rollback(root)')
    assert restored.returncode == 0, restored.stderr
    assert machine.installed() == 'new'
    assert not (machine.root / 'rollback').exists() or not list((machine.root / 'rollback').iterdir())


def test_an_interrupt_between_the_package_upgrade_and_the_scripts_restores_everything(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    interrupted = machine.update(CREDENTIAL, (
        'upgrade = w.upgrade_pool\n'
        'def upgraded_then_interrupted(*args):\n'
        '    result = upgrade(*args)\n'
        '    raise KeyboardInterrupt\n'
        'w.upgrade_pool = upgraded_then_interrupted'))
    assert interrupted.returncode != 0
    assert machine.installed() == 'old' and not _has_new_module(machine)
    assert machine.record() == 'old' and machine.scripts() == '# runner old\n' and machine.config() == 'old'


def test_a_script_replaced_just_before_an_interrupt_is_restored(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    # The signal is handled right after os.replace() returns, before anything else runs.
    interrupted = machine.update(CREDENTIAL, (
        'replace, state = w.os.replace, {"done": False}\n'
        'def replace_then_interrupt(source, target):\n'
        '    replace(source, target)\n'
        '    if pathlib.Path(target) == root / "worker.py" and not state["done"]:\n'
        '        state["done"] = True\n'
        '        raise KeyboardInterrupt\n'
        'w.os.replace = replace_then_interrupt'))
    assert interrupted.returncode != 0
    assert machine.scripts() == '# runner old\n'
    assert machine.installed() == 'old' and machine.record() == 'old'


@pytest.mark.parametrize('truncated', [' \n', ',\n'])  # parse as [] and as ['.']
def test_a_legacy_backup_over_a_truncated_record_is_refused(machine, truncated):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    assert outcome(machine.update())['changed'] is True
    _record_path(machine).write_text(truncated)
    [backup] = list((machine.root / 'rollback').iterdir())
    (backup / 'replaced_by.json').unlink()  # as written by an updater before this one
    refused = machine.run('w.rollback(root)')
    assert refused.returncode != 0 and 'cannot be listed' in refused.stderr
    assert _has_new_module(machine) and backup.exists()


def test_an_update_that_installs_nothing_still_prunes_old_backups(machine):
    assert outcome(machine.update())['changed'] is True
    stale = [machine.root / 'rollback' / f'pool-{"a" * 12}-{n}' for n in (1, 2)]
    for path in stale:
        path.mkdir(parents=True)
    newest = max((machine.root / 'rollback').iterdir(), key=lambda p: int(p.name.rsplit('-', 1)[-1]))
    assert outcome(machine.update())['changed'] is False
    assert list((machine.root / 'rollback').iterdir()) == [newest]


def test_an_incomplete_newer_backup_never_displaces_the_usable_one(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    assert outcome(machine.update())['changed'] is True
    [usable] = list((machine.root / 'rollback').iterdir())
    # Left by an older updater stopped mid-copy: files, but no manifest.json.
    incomplete = machine.root / 'rollback' / f'pool-{"b" * 12}-{time.time_ns()}'
    (incomplete / 'files').mkdir(parents=True)
    assert outcome(machine.update())['changed'] is False
    assert list((machine.root / 'rollback').iterdir()) == [usable]
    restored = machine.run('w.rollback(root)')
    assert restored.returncode == 0, restored.stderr
    assert machine.installed() == 'old'


def test_a_rollback_skips_an_incomplete_backup(machine):
    assert outcome(machine.update())['changed'] is True
    machine.served['build'] = 'new'
    assert outcome(machine.update())['changed'] is True
    (machine.root / 'rollback' / f'pool-{"c" * 12}-{time.time_ns()}').mkdir()
    restored = machine.run('w.rollback(root)')
    assert restored.returncode == 0, restored.stderr
    assert machine.installed() == 'old'
