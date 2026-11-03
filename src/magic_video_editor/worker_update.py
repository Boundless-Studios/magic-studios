"""Refresh an enrolled worker without spending a pairing code.

Stop the worker gracefully first. Windows operators must stop their scheduled
worker only after its current jobs finish. This updater never stops or starts
processes, so it can never restart a busy runtime.

It refreshes the runner scripts and brings the pinned pool package to the build
the studio ships. Every build is version 0.1.0, so builds are told apart by the
wheel digest recorded in pool-package.json. A different build is downloaded with
the machine's own credential, installed without changing other dependencies,
compared file by file with the wheel and checked in a fresh process (the profile
check that decides what the worker advertises). Any failure restores the previous
package and leaves the worker stopped. `--rollback` restores the latest backup.

Usage: python worker-update.py [--rollback]
"""
import ast
import base64
import csv
import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

POOL = 'outbound-gpu-worker-pool'
RECORD = 'pool-package.json'
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
WHEEL_NAME = re.compile(r'outbound_gpu_worker_pool-[A-Za-z0-9_.+]+-py3-none-any\.whl')
FEATURE_KEYS = ('magic_feature_last_frame',)
# The extras attach installs; their dependencies must already be present.
EXTRAS = ('agent', 'comfy')
# Runs in a fresh interpreter, so the newly installed package is the one checked.
VERIFY = '''
import asyncio, json, os
import outbound_gpu_worker_pool.agent_main
from outbound_gpu_worker_pool.machine_profiles import prepare_environment
prepared = asyncio.run(prepare_environment(dict(os.environ)))
print(json.dumps({"runtime_versions": json.loads(prepared.get("OGWP_WORKER_RUNTIME_VERSIONS", "{}"))}))
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The machine credential must never be forwarded to a different host or path.
        return None


def _require_stopped():
    if os.name == 'nt':
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            "if ((Get-ScheduledTask -TaskName MagicVideoStudioWorker -ErrorAction SilentlyContinue).State -eq 'Running') { exit 1 }"], check=False)
        if result.returncode:
            raise SystemExit('Wait for jobs to finish and stop the scheduled worker before updating.')
        return
    result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'magic-video-worker'], check=False)
    # is-active returns zero for a running service.
    if result.returncode == 0:
        raise SystemExit('Stop magic-video-worker gracefully before updating; enrollment is preserved.')


def installed_record(root):
    """What the last install or update recorded, or {} when nothing did."""
    try:
        return json.loads((root / RECORD).read_text())
    except (OSError, ValueError):
        return {}


def _post(url, credential):
    request = urllib.request.Request(url, data=json.dumps({'credential': credential}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            return {key.lower(): value for key, value in response.headers.items()}, response.read(MAX_PACKAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise SystemExit('The studio refused this machine credential; nothing was changed. '
                             'Start the worker once so it can renew, or re-attach the machine.') from None
        raise SystemExit(f'The worker package is unavailable (HTTP {exc.code}); nothing was changed.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SystemExit('Could not reach the studio; nothing was changed.') from None


def fetch_manifest(url, credential):
    _, body = _post(url + '/studio/workers/package/manifest', credential)
    manifest = json.loads(body)
    if not WHEEL_NAME.fullmatch(manifest.get('filename', '')) or not re.fullmatch(r'[0-9a-f]{64}', manifest.get('sha256', '')):
        raise SystemExit('The studio returned invalid worker package metadata; nothing was changed.')
    return manifest


def download_wheel(url, credential, manifest, folder):
    headers, data = _post(url + '/studio/workers/package/update', credential)
    if (len(data) > MAX_PACKAGE_BYTES or headers.get('x-worker-package-sha256') != manifest['sha256']
            or headers.get('x-worker-package-filename') != manifest['filename']
            or hashlib.sha256(data).hexdigest() != manifest['sha256']):
        raise SystemExit('Worker package verification failed; nothing was changed.')
    wheel = Path(folder) / manifest['filename']
    wheel.write_bytes(data)
    return wheel


def _installed():
    from importlib.metadata import distribution
    return distribution(POOL)


def backup_pool(destination):
    """Copy every installed file of the pool package (its RECORD) for a rollback."""
    from importlib.metadata import PackageNotFoundError
    try:
        dist = _installed()
    except PackageNotFoundError:
        # Nothing installed: a rollback only removes the new files.
        base, files = Path(sysconfig.get_paths()['purelib']), []
    else:
        try:
            listed = dist.files
        except Exception as exc:  # noqa: BLE001 - unreadable metadata is not "nothing installed"
            raise SystemExit(f'The installed pool package cannot be listed ({exc}); nothing was changed.') from exc
        if listed is None:
            # Without a RECORD there is nothing to restore from, so a failed update
            # would leave no package at all.
            raise SystemExit('The installed pool package has no file list (RECORD); nothing was changed. '
                             'Re-attach the machine to repair it.')
        base, files = Path(dist.locate_file('')), [str(entry) for entry in listed]
    for relative in files:
        source = base / relative
        if source.is_file():
            target = destination / 'files' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / 'manifest.json').write_text(json.dumps({'base': str(base), 'files': files}))


def wheel_files(wheel):
    """Every path the wheel installs, from its own RECORD."""
    with zipfile.ZipFile(wheel) as archive:
        name = next(entry for entry in archive.namelist() if entry.endswith('.dist-info/RECORD'))
        return [row[0] for row in csv.reader(io.StringIO(archive.read(name).decode())) if row]


def restore_pool(backup):
    """Remove the current package's files and put the backed-up ones back.

    The files to remove come from the installed RECORD and from the list of the
    build installed over this backup (`replaced_by.json`). With neither, files
    unique to the newer build would survive beside the restored ones, so the
    restore is refused before anything changes.
    """
    saved = json.loads((backup / 'manifest.json').read_text())
    base = Path(saved['base'])
    replaced_by = backup / 'replaced_by.json'
    newer = json.loads(replaced_by.read_text()) if replaced_by.is_file() else []
    try:
        listed = _installed().files
        current = [str(entry) for entry in listed] if listed is not None else None
    except Exception:  # noqa: BLE001 - a half-installed package may have no metadata
        current = None
    # A list naming no installed file is no better than none: a truncated RECORD
    # can parse as [] or as a bare row such as ['.'].
    if not any((base / relative).is_file() for relative in current or ()) and not newer:
        raise SystemExit('The installed pool package cannot be listed and the backup does not say what '
                         'replaced it; nothing was changed. Re-attach the machine to repair it.')
    current = sorted({*(current or ()), *newer})
    for relative in current:
        path = base / relative
        if path.is_file():
            path.unlink()
    for relative in saved['files']:
        source = backup / 'files' / relative
        if source.is_file():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # A restored source can carry the same size and mtime as the build it replaces
    # (reproducible wheels pin timestamps), so a cached .pyc would still look
    # valid and Python would keep running the removed build.
    for relative in {*current, *saved['files']}:
        if relative.endswith('.py'):
            shutil.rmtree((base / relative).parent / '__pycache__', ignore_errors=True)


def installed_matches(wheel):
    """Every file the wheel ships is installed with the content its RECORD promises."""
    base = Path(_installed().locate_file(''))
    with zipfile.ZipFile(wheel) as archive:
        name = next(entry for entry in archive.namelist() if entry.endswith('.dist-info/RECORD'))
        rows = list(csv.reader(io.StringIO(archive.read(name).decode())))
    for path, digest, _ in rows:
        if not digest or '.dist-info/' in path or '.data/' in path:
            continue
        algorithm, _, expected = digest.partition('=')
        try:
            data = (base / path).read_bytes()
        except OSError:
            return False
        actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b'=').decode()
        if actual != expected:
            return False
    return True


def _uv():
    found = shutil.which('uv') or Path.home() / '.local' / 'bin' / ('uv.exe' if os.name == 'nt' else 'uv')
    if not Path(found).is_file():
        raise SystemExit('uv was not found (attach installs it in ~/.local/bin); nothing was changed.')
    return str(found)


def extras_satisfied(uv, wheel):
    """Whether the wheel's own and its worker extras' dependencies are all installed.

    `uv pip check` ignores extras, so resolve `wheel[agent,comfy]` offline
    against only what is installed. Nothing is changed.
    """
    result = subprocess.run([uv, 'pip', 'install', '--dry-run', '--no-index', '--python', sys.executable,
                             '--reinstall-package', POOL, f'{wheel}[{",".join(EXTRAS)}]'],
                            capture_output=True, text=True, check=False)
    return result.returncode == 0


def install_wheel(uv, wheel):
    subprocess.run([uv, 'pip', 'install', '--python', sys.executable, '--reinstall-package', POOL,
                    '--no-deps', str(wheel)], check=True)


def verify(environment):
    """Import the installed agent and run the profile check in a fresh process."""
    result = subprocess.run([sys.executable, '-c', VERIFY], env={**os.environ, **environment},
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-2000:] or 'verification failed')
    return json.loads(result.stdout.strip().splitlines()[-1])


class Interrupted(BaseException):
    """SIGTERM arrived while the worker's files were being changed."""


@contextmanager
def interruptible():
    """While files change, SIGTERM raises like Ctrl-C does, so the handlers restore."""
    def raise_interrupted(signum, frame):
        raise Interrupted(f'signal {signum}')
    previous = signal.signal(signal.SIGTERM, raise_interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def uninterrupted():
    """Hold off Ctrl-C and SIGTERM while a restore runs: half a restore is worse than none."""
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def upgrade_pool(url, credential, environment, root, folder, created=None):
    """Bring the pool package to the studio's build; on failure, restore and stop.

    Returns the manifest, the profile check's result and the backup of the
    previous package (None when nothing changed). The caller records the new
    build only once the rest of the refresh has succeeded. A complete backup is
    also appended to `created` the moment it exists.
    """
    manifest = fetch_manifest(url, credential)
    if installed_record(root).get('sha256') == manifest['sha256']:
        try:
            return manifest, verify(environment), None
        except RuntimeError as exc:
            raise SystemExit(f'The installed worker failed its check ({exc}); nothing was changed.') from exc
    uv = _uv()
    wheel = download_wheel(url, credential, manifest, folder)
    if not extras_satisfied(uv, wheel):
        raise SystemExit('The new pool package needs dependencies this worker does not have; nothing was '
                         'changed. Re-attach the machine to install them.')
    previous = installed_record(root).get('sha256') or 'unrecorded'
    backup = root / 'rollback' / f'pool-{previous[:12]}-{time.time_ns()}'
    backup.mkdir(parents=True)
    try:
        backup_pool(backup)
    except BaseException:
        shutil.rmtree(backup)
        raise
    if created is not None:
        # The caller's handler restores from it if an interruption lands after this
        # function's own handler has stopped covering the install.
        created.append(backup)
    (backup / RECORD).write_text(json.dumps(installed_record(root)))
    # What is about to be installed, so a restore can remove it even if the
    # interrupted install leaves no readable RECORD behind.
    (backup / 'replaced_by.json').write_text(json.dumps(wheel_files(wheel)))
    try:
        with interruptible():
            install_wheel(uv, wheel)
            if not installed_matches(wheel):
                raise RuntimeError('the installed files do not match the downloaded wheel')
            result = verify(environment)
    except BaseException as exc:  # noqa: BLE001 - every failure, Ctrl-C included, restores the previous package
        with uninterrupted():
            restore_pool(backup)
            # The restored build is installed again, so --rollback must not offer it.
            shutil.rmtree(backup)
        if not isinstance(exc, Exception):
            raise
        raise SystemExit(f'Pool package update failed and was rolled back ({exc}). '
                         'The worker is left stopped; start it again to keep the previous version.') from exc
    return manifest, result, backup


def _replace_file(path, data):
    """Write `data` to `path` atomically: a reader sees the old or the new bytes."""
    staging = path.with_name(path.name + '.tmp')
    staging.write_bytes(data)
    os.replace(staging, path)


def _write_record(root, manifest):
    _replace_file(root / RECORD, json.dumps(manifest).encode())


def apply_update(root, url, config, write_config):
    """Stage and check everything, upgrade the package, then replace the scripts,
    write the configuration and record the build. Any failure after the package
    changed restores the previous package and scripts."""
    with tempfile.TemporaryDirectory(dir=root) as folder:
        staged = Path(folder)
        scripts = staged / 'scripts'
        scripts.mkdir()
        for remote, local in [('worker.py', 'worker.py'), ('cpu-worker.py', 'cpu_worker.py'),
                              ('worker-runtime.py', 'worker_runtime.py')]:
            data = urllib.request.urlopen(url + '/studio/' + remote, timeout=30).read()
            ast.parse(data)
            (scripts / local).write_bytes(data)
        # Validate using staged helper before changing any installed files.
        import importlib.util
        spec = importlib.util.spec_from_file_location('updated_runtime', scripts / 'worker_runtime.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        environment = dict(os.environ)
        environment.update(config)
        original_config = dict(config)
        config = {**config, **runtime.configure_runtime(environment)}
        environment.update(config)
        packages = staged / 'packages'
        packages.mkdir()
        originals = staged / 'originals'
        originals.mkdir()
        replaced = []
        created = []
        record_path = root / RECORD
        previous_record = record_path.read_bytes() if record_path.is_file() else None
        config_written = False
        try:
            # One guard from the package upgrade to the record: an interruption
            # between phases is restored like one inside them.
            with interruptible():
                manifest, result, backup = upgrade_pool(url, config.get('OGWP_WORKER_TOKEN', ''), environment,
                                                        root, packages, created)
                # Only the staged scripts: loading the runtime may have left a __pycache__ here.
                for file in (path for path in scripts.iterdir() if path.is_file()):
                    if (root / file.name).is_file():
                        shutil.copy2(root / file.name, originals / file.name)
                    # Tracked before the replace: a signal handled right after it
                    # must still find this script in the restore set.
                    replaced.append(file.name)
                    os.replace(file, root / file.name)
                config_written = True
                write_config(config)
                _write_record(root, manifest)
        except BaseException as exc:  # noqa: BLE001 - leave the previous worker exactly as it was, Ctrl-C included
            unrestored = ''
            with uninterrupted():
                for name in replaced:
                    if (originals / name).is_file():
                        os.replace(originals / name, root / name)
                    else:
                        (root / name).unlink(missing_ok=True)
                # upgrade_pool restores (and removes) its own backup when the install
                # fails; one that still exists means the package changed and stayed.
                for made in created:
                    if made.exists():
                        restore_pool(made)
                        shutil.rmtree(made)
                if previous_record is None:
                    record_path.unlink(missing_ok=True)
                else:
                    _replace_file(record_path, previous_record)
                if config_written:
                    try:
                        write_config(original_config)
                    except Exception as restore_error:  # noqa: BLE001 - report it rather than hide the rollback
                        unrestored = (f' The previous configuration could not be written back ({restore_error}); '
                                      'restore it before starting the worker.')
            if not isinstance(exc, Exception):
                raise
            raise SystemExit(f'Worker refresh failed and was rolled back ({exc}).{unrestored} The worker is left '
                             'stopped; start it again to keep the previous version.') from exc
    # Also when the package was already current: backups left by earlier updaters
    # are pruned to the one --rollback can use.
    prune_backups(root, keep=backup)
    return manifest, result, backup is not None


def _backups(root):
    """Pool backups, oldest first (by the creation time in their names)."""
    return sorted((root / 'rollback').glob('pool-*'), key=lambda path: path.name.rsplit('-', 1)[-1].zfill(20))


def usable_backup(path):
    """Whether a backup finished: backup_pool writes manifest.json last, so an
    updater stopped mid-copy leaves none (or an unreadable one)."""
    try:
        saved = json.loads((path / 'manifest.json').read_text())
    except (OSError, ValueError):
        return False
    return isinstance(saved, dict) and isinstance(saved.get('base'), str) and isinstance(saved.get('files'), list)


def prune_backups(root, keep=None):
    """After a successful update only one backup can be rolled back to: `keep`, or
    the newest usable one when this update installed nothing. Drop the rest,
    including incomplete backups an interrupted older updater left behind."""
    backups = _backups(root)
    usable = [path for path in backups if usable_backup(path)]
    keep = keep if keep is not None else (usable[-1] if usable else None)
    for old in backups:
        if old != keep:
            shutil.rmtree(old, ignore_errors=True)


def rollback(root):
    backups = [path for path in _backups(root) if usable_backup(path)]
    if not backups:
        raise SystemExit('No complete pool package backup to restore.')
    with uninterrupted():
        restore_pool(backups[-1])
        previous = backups[-1] / RECORD
        if previous.is_file() and json.loads(previous.read_text()):
            shutil.copy2(previous, root / RECORD)
        else:
            (root / RECORD).unlink(missing_ok=True)
        shutil.rmtree(backups[-1])
    print(f'Restored the pool package from {backups[-1].name}. Start the worker service/task again.')


USAGE = 'Usage: python worker-update.py [--rollback]'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ['--rollback']):
        # A mistyped recovery flag must never fall through to an upgrade.
        raise SystemExit(USAGE)
    root = Path.home() / '.magic-video-worker'
    _require_stopped()
    if argv == ['--rollback']:
        rollback(root)
        return
    sys.path.insert(0, str(root))
    import worker as previous
    config_path = previous.default_config_path()
    config = previous._read_config(config_path)
    url = config.get('MVS_STUDIO_URL', 'https://magicstudios.ai').rstrip('/')
    if not url.startswith('https://'):
        raise SystemExit('Worker updates require an HTTPS Studio URL')
    manifest, result, changed = apply_update(root, url, config,
                                             lambda updated: previous._write_config(config_path, updated))
    versions = result.get('runtime_versions', {})
    features = [key for key in FEATURE_KEYS if versions.get(key) == '1']
    print(f"Pool package {'updated to' if changed else 'already at'} {manifest.get('commit') or 'build'}"
          f" ({manifest['sha256'][:12]}). Features: {', '.join(features) or 'none'}.")
    print('Worker refreshed; credentials and profile preserved. Start the worker service/task again.')


if __name__ == '__main__':
    main()
